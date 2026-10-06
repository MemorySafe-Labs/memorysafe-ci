from __future__ import annotations

import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memorysafe_chatgpt.storage import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_store_merge_find_forget_and_health(self) -> None:
        self.assertFalse(self.store.automatic_mode_enabled())
        self.store.set_automatic_mode(True)
        self.assertTrue(self.store.automatic_mode_enabled())

        first = self.store.remember(
            "I prefer concise answers.", "preference", 0.9, 0.95
        )
        self.assertEqual(first["decision"], "PROTECT")
        self.assertTrue(first["protected"])

        duplicate = self.store.remember(
            "I prefer concise answers.", "preference", 0.8, 0.9
        )
        self.assertEqual(duplicate["decision"], "MERGE")
        self.assertEqual(duplicate["memory_id"], first["memory_id"])

        matches = self.store.find("concise answers", 5)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["memory_id"], first["memory_id"])

        health = self.store.health()
        self.assertEqual(health["active_memories"], 1)
        self.assertEqual(health["comparison"]["raw_remember_requests"], 2)
        self.assertEqual(health["comparison"]["duplicate_copies_avoided"], 1)
        self.assertTrue(health["automatic_mode"])
        self.assertEqual(health["automatic_captures"], 0)
        self.assertEqual(health["automatic_candidates_evaluated"], 0)
        self.assertIn("no candidate has reached", health["automatic_status"])
        self.assertIsNone(health["automatic_last_event_at"])
        benchmark = health["token_benchmark"]
        # Pinning the old hand-entered figures is what let them go stale; assert the
        # invariants the model must satisfy instead of a number that moves whenever a
        # tool description is edited.
        self.assertEqual(benchmark["kind"], "measured_input_payload_model")
        self.assertGreater(benchmark["tool_schema_tokens"], 0)
        self.assertEqual(
            benchmark["fixed_overhead_tokens"],
            benchmark["tool_schema_tokens"] + benchmark["instruction_tokens"],
        )
        self.assertEqual(
            benchmark["break_even_facts"],
            math.ceil(benchmark["fixed_overhead_tokens"] / benchmark["average_fact_tokens"])
            + benchmark["recalled_per_turn"],
        )
        at_break_even = benchmark["break_even_facts"] * benchmark["average_fact_tokens"]
        governed = benchmark["fixed_overhead_tokens"] + (
            benchmark["recalled_per_turn"] * benchmark["average_fact_tokens"]
        )
        self.assertGreaterEqual(at_break_even, governed)
        self.assertLess(
            (benchmark["break_even_facts"] - 1) * benchmark["average_fact_tokens"],
            governed,
        )
        self.assertFalse(health["token_benchmark"]["chatgpt_live_usage_available"])
        compare = health["token_benchmark"]["live_compare"]
        self.assertEqual(compare["facts_stored"], 1)
        self.assertEqual(
            compare["without_memorysafe_tokens"],
            compare["facts_stored"] * compare["average_fact_tokens"],
        )
        self.assertEqual(
            compare["with_memorysafe_tokens"],
            compare["fixed_overhead_tokens"]
            + compare["facts_recalled_per_turn"] * compare["average_fact_tokens"],
        )
        self.assertIn(compare["paying_off"], (True, False))
        if compare["paying_off"]:
            self.assertGreater(compare["tokens_saved_per_turn"], 0)
        else:
            self.assertEqual(compare["tokens_saved_per_turn"], 0)
            self.assertGreaterEqual(compare["tokens_extra_per_turn"], 0)
        self.assertEqual(health["live_token_metrics"]["counted_memories"], 1)
        self.assertGreater(health["live_token_metrics"]["active_memory_tokens"], 0)
        self.assertEqual(
            health["live_token_metrics"]["active_memory_tokens"],
            health["live_token_metrics"]["top_context_tokens"],
        )

        self.store.remember(
            "My preferred editor is VS Code.",
            "preference",
            0.7,
            0.9,
            source="automatic",
        )
        self.store.record_automatic_skip("Sensitive candidate skipped.")
        health = self.store.health()
        self.assertEqual(health["automatic_captures"], 1)
        self.assertEqual(health["automatic_skips"], 1)
        self.assertEqual(health["automatic_candidates_evaluated"], 2)
        self.assertIn("evaluated 2 candidates", health["automatic_status"])
        self.assertEqual(health["automatic_last_decision"], "SKIP_AUTO")
        self.assertEqual(health["automatic_last_reason"], "Sensitive candidate skipped.")
        self.assertIsNotNone(health["automatic_last_event_at"])

        # 0.4.11: a protected memory is forgotten only with confirm=True.
        forgotten = self.store.forget(first["memory_id"], confirm=True)
        self.assertTrue(forgotten["forgotten"])
        remaining_ids = {item["memory_id"] for item in self.store.find("concise", 5)}
        self.assertNotIn(first["memory_id"], remaining_ids)

    def test_forgotten_memory_is_not_reported_as_a_duplicate_avoided(self) -> None:
        saved = self.store.remember("A unique memory that will be forgotten.", "other")
        self.store.forget(saved["memory_id"])

        comparison = self.store.health()["comparison"]

        self.assertEqual(comparison["raw_remember_requests"], 1)
        self.assertEqual(comparison["duplicate_copies_avoided"], 0)
        self.assertEqual(comparison["content_bytes_avoided"], 0)

    def test_irrelevant_memories_are_not_recalled(self) -> None:
        """Importance must not smuggle an unrelated memory into a recall.

        The relevance term and the importance term used to be added before the cut, so a
        typical importance of 0.85 contributed 0.085 against a 0.08 threshold: every
        stored memory came back for every query. Recall stopped being selective and the
        caller paid context for the whole store on each turn.
        """

        store = self.store
        store.remember("Primo-adoptants deadline is 4 November 2026", "project")
        store.remember("Likes oat milk", "other")

        hits = [item["content"] for item in store.find("when is the primo deadline", 5)]

        self.assertTrue(any("Primo-adoptants" in text for text in hits))
        self.assertFalse(any("oat milk" in text for text in hits))

    def test_recall_is_recorded_and_separated_from_governance(self) -> None:
        store = self.store
        store.remember("Morgan is Head of Operations, no equity", "decision")
        store.find("Morgan", 5)

        health = store.health()
        self.assertNotIn("RECALL", health["decision_counts"])
        self.assertEqual(health["recall"]["times_memory_was_consulted"], 1)
        self.assertEqual(health["recall"]["memories_ever_recalled"], 1)

    def test_stopwords_do_not_create_matches(self) -> None:
        """A shared "is" and "of" is not a reason to return a memory.

        Before content words were isolated, "what is the capital of Portugal" scored
        0.244 against a 0.12 bar on a memory about who holds equity — every
        unanswerable question came back with confident, irrelevant context.
        """

        store = self.store
        store.remember("Morgan Reyes is Head of Operations and holds no equity", "decision")
        store.remember("Primo-adoptants closes 4 November 2026", "project")

        self.assertEqual(store.find("how do I roast a chicken", 5, record=False), [])
        self.assertEqual(store.find("what time does the pharmacy close", 5, record=False), [])
        self.assertTrue(store.find("when does primo close", 5, record=False))

    def test_light_stemming_matches_plural_and_tense(self) -> None:
        store = self.store
        store.remember("One provisional patent filed April 2026", "decision")
        hits = [h["content"] for h in store.find("how many patents do I have", 5, record=False)]
        self.assertTrue(any("provisional patent" in h for h in hits))

    def test_question_shape_breaks_ties(self) -> None:
        """A "when" question should prefer the memory that contains a date.

        Every near-miss in the recall benchmark was a tie: two memories matched the
        same single word and ranking fell to length and importance, which is arbitrary.
        The question word says what the answer should look like.
        """

        store = self.store
        store.remember("Jordan Ellis is a strategic advisor and NXL alum", "personal")
        store.remember("NXL-Toronto admission decision expected 18 September 2026", "project")

        hits = store.find("when will NXL tell me", 5, record=False)
        self.assertTrue(hits)
        self.assertIn("18 September", hits[0]["content"])

    def test_stemmer_matches_incorporation_and_incorporated(self) -> None:
        # "ation" alone left incorporation as "incorpor" and incorporated as
        # "incorporat", so the two never met.
        from memorysafe_chatgpt.storage import _stem

        self.assertEqual(_stem("incorporation"), _stem("incorporated"))
        self.assertEqual(_stem("notarization"), _stem("notarized"))

    def test_protection_never_lapses_with_age(self) -> None:
        """Nothing may revoke protection because a memory got old.

        Only evidence should demote a memory, never the calendar. This asserts the
        property directly rather than trusting that no decay logic gets added later.
        """

        store = self.store
        saved = store.remember("Morgan holds no equity as of August 2026", "decision")
        self.assertTrue(saved["protected"])

        with store._session() as connection:
            connection.execute(
                "UPDATE memories SET created_at = ?, updated_at = ? WHERE id = ?",
                ("2019-01-01T00:00:00+00:00", "2019-01-01T00:00:00+00:00", saved["memory_id"]),
            )

        store.health()
        store.find("equity", 5, record=False)
        explained = store.explain(saved["memory_id"])
        self.assertTrue(explained["protected"])
        self.assertEqual(explained["state"], "active")

    def test_every_protection_and_revocation_is_explainable(self) -> None:
        store = self.store
        saved = store.remember("One provisional patent filed April 2026", "decision")
        explained = store.explain(saved["memory_id"])
        self.assertTrue(explained["protected"])
        self.assertTrue(explained["protected_because"])
        self.assertTrue(explained["protected_since"])

        store.forget(saved["memory_id"], confirm=True)
        after = store.explain(saved["memory_id"])
        self.assertEqual(after["state"], "deleted")
        forgets = [e for e in after["history"] if e["decision"] == "FORGET"]
        self.assertEqual(len(forgets), 1)
        self.assertTrue(forgets[0]["reason"])

    def test_explain_survives_a_missing_memory(self) -> None:
        # A command a user reaches for when confused must not itself raise.
        detail = self.store.explain("MS-does-not-exist")
        self.assertFalse(detail["found"])

    def test_protection_reasons_distinguish_why(self) -> None:
        """Different triggers must give different reasons.

        Every protected memory used to carry one identical sentence, which recorded
        that a decision happened without saying why this memory got it. A reason that
        is the same for every case explains nothing.
        """

        store = self.store
        reasons = {
            category: store.remember(f"a durable {category} fact", category)["reason"]
            for category in ("decision", "safety", "preference", "project", "personal")
        }
        self.assertEqual(len(set(reasons.values())), len(reasons), reasons)
        self.assertIn("decision", reasons["decision"].lower())
        self.assertIn("safety", reasons["safety"].lower())

    def test_both_assistants_resolve_to_one_store(self) -> None:
        """Claude and the Codex connector must land on the same file by default.

        They install into different roots, and the store used to be resolved relative
        to each of them. The result was two memories that never met, with each
        assistant appearing to have forgotten what the other was told.
        """

        import os
        from memorysafe_chatgpt.server import _default_database_path, canonical_data_dir

        saved = os.environ.pop("MEMORYSAFE_DB_PATH", None)
        try:
            as_claude = _default_database_path()
            os.environ["MEMORYSAFE_INSTALL_ROOT"] = "/tmp/some-other-install-root"
            as_codex = _default_database_path()
        finally:
            os.environ.pop("MEMORYSAFE_INSTALL_ROOT", None)
            if saved is not None:
                os.environ["MEMORYSAFE_DB_PATH"] = saved

        self.assertEqual(as_claude, as_codex)
        self.assertNotIn("MemorySafe Beta", str(canonical_data_dir()))

    def test_linux_store_is_under_xdg_data_home(self) -> None:
        import os
        from unittest.mock import patch
        from memorysafe_chatgpt import server as connector

        with patch.object(connector.sys, "platform", "linux"):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("XDG_DATA_HOME", None)
                os.environ.pop("MEMORYSAFE_DB_PATH", None)
                path = connector.canonical_data_dir()
        self.assertEqual(path, Path.home() / ".local" / "share" / "MemorySafe" / "data")

    def test_windows_localappdata_present_never_evaluates_home_fallback(self) -> None:
        """os.environ.get("LOCALAPPDATA", Path.home() / ...) evaluates Path.home()
        unconditionally, since Python evaluates a call's arguments before the call
        runs -- even when LOCALAPPDATA is set and the fallback is discarded. On
        Windows, Path.home() can raise RuntimeError when neither USERPROFILE nor
        HOMEDRIVE/HOMEPATH is set, which would crash canonical_data_dir() -- the
        production MCP server's own resolution of where the user's memories live --
        even though LOCALAPPDATA was present and the fallback was never going to be
        used. Four other call sites had this bug fixed in 4ec89fd; this one was
        missed twice because both fix lists came from a triage report rather than
        an exhaustive grep.
        """

        import os
        from unittest.mock import patch
        from memorysafe_chatgpt import server as connector

        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "Local"
            local.mkdir()
            with patch.object(connector.sys, "platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": str(local)}, clear=True
            ), patch.object(
                connector.Path, "home", side_effect=RuntimeError("no home directory")
            ):
                path = connector.canonical_data_dir()
        self.assertEqual(path, (local / "MemorySafe" / "data"))

    def test_setup_skips_assistants_that_are_not_installed(self) -> None:
        """A Codex-only Mac must not be given Claude instructions.

        The beta shipped three files and asked the tester which one they needed. The
        machine can answer that itself, and an absent assistant is the normal case,
        not an error.
        """

        import importlib.util, io, contextlib
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "setup_assistants",
            Path(__file__).resolve().parents[1] / "scripts" / "setup_assistants.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        module.detect = lambda: {"claude_desktop": False, "claude_code": False, "codex": True}
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.main()
        output = buffer.getvalue()

        self.assertEqual(code, 0)
        self.assertIn("Codex", output)
        self.assertNotIn("ChatGPT / Codex", output)
        self.assertIn("Claude Desktop    not installed", output)
        self.assertIn("Claude Code       not installed", output)

        module.detect = lambda: {"claude_desktop": False, "claude_code": False, "codex": False}
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module.main()
        self.assertEqual(code, 1)
        self.assertIn("No supported assistant found", buffer.getvalue())

    def test_repeat_notifications_are_suppressed(self) -> None:
        """A crash loop restarts every thirty seconds; warning each time is its own bug."""

        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "ms_notify", Path(__file__).resolve().parents[1] / "scripts" / "notify.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.STATE = Path(self.temp_dir.name) / "state"

        sent = []
        module.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: sent.append(1))})()

        self.assertTrue(module.notify("t", "m", "key-under-test"))
        self.assertFalse(module.notify("t", "m", "key-under-test"))
        self.assertEqual(len(sent), 1)

    @unittest.skip("needs crash-loop notification in run_setup_service.sh and run_tunnel_service.sh, not written yet")
    def test_service_scripts_watch_for_crash_loops(self) -> None:
        """launchd restarts a dead service forever and tells nobody.

        That is how this install ran broken for eleven days: both services were dying
        on launch, and a dying service was indistinguishable from a working one.
        """

        from pathlib import Path

        scripts = Path(__file__).resolve().parents[1] / "scripts"
        for name in ("run_setup_service.sh", "run_tunnel_service.sh"):
            body = (scripts / name).read_text()
            self.assertIn("notify.py", body, name)
            self.assertIn(".starts", body, name)

    def test_search_reaches_memories_below_the_top_thousand(self) -> None:
        """A memory outside the top 1000 by rank must still be findable.

        Candidates were selected with ORDER BY protected, importance, updated_at
        LIMIT 1000 and only those were scored. Past a thousand memories, anything
        ranked below the cut was stored, counted, described as protected, and
        impossible to recall — the failure a memory product cannot have.
        """

        store = self.store
        store.remember("The Fasken workshop runs 28 August to 1 September 2026", "other")
        for index in range(1200):
            store.remember(f"routine project note number {index}", "project")

        hits = store.find("when does the fasken workshop run", 5, record=False)
        self.assertTrue(hits, "the buried memory was unreachable")
        self.assertIn("Fasken", hits[0]["content"])

    def test_accented_and_non_latin_memories_work(self) -> None:
        """Portuguese must not shred at every accent, and other scripts must store.

        Normalisation matched [a-z0-9] only: "submissão" became "submiss o", "coração"
        became "cora o", and Japanese, Russian, Arabic and Greek folded to nothing and
        were refused as containing no letters. For a product built in Brazil that is
        not an edge case.
        """

        store = self.store
        store.remember("A submissão do Gama Fund é 28 de setembro", "project")
        store.remember("Reunião com Mathieu Rinaldi no BDC Capital", "project")
        store.remember("Привет, это память", "other")

        # Found with the accents, and without them — people do not type their own.
        for query in ("submissão do gama", "submissao do gama"):
            hits = store.find(query, 3, record=False)
            self.assertTrue(hits, query)
            self.assertIn("submissão", hits[0]["content"])

        self.assertTrue(store.find("reuniao com mathieu", 3, record=False))
        self.assertTrue(store.find("память", 3, record=False))

    def test_spaceless_scripts_are_findable(self) -> None:
        # A whole Japanese sentence is one token, so equality never matches and the
        # memory was storable but unrecallable.
        store = self.store
        store.remember("日本語のメモリーをテストします", "other")
        self.assertTrue(store.find("日本語", 3, record=False))

    def test_injection_attempts_do_not_damage_the_store(self) -> None:
        store = self.store
        for attack in ("'; DROP TABLE memories; --", "1' OR '1'='1"):
            store.remember(attack, "other")
            store.find(attack, 3, record=False)
        self.assertGreaterEqual(store.health()["active_memories"], 2)

    def test_recall_log_is_pruned_without_losing_the_count(self) -> None:
        """The log is capped; the history is not.

        One row is written per search and nothing removed them, so ordinary use would
        leave hundreds of thousands of rows. Trimming them is fine — reporting a total
        that silently stops at the cap is not.
        """

        store = self.store
        for index in range(5):
            store.remember(f"memory number {index}", "project")
        searches = 2100
        for _ in range(searches):
            store.find("memory number 1", 5)

        # Through the store's own session, so this runs on every backend.
        with store._session() as connection:
            kept = connection.execute(
                "SELECT COUNT(*) FROM decision_events WHERE decision = 'RECALL'"
            ).fetchone()[0]
        self.assertLessEqual(kept, 2000, "recall log grew past its cap")
        self.assertEqual(
            store.health()["recall"]["times_memory_was_consulted"],
            searches,
            "pruning lost consultations from the reported total",
        )

    def test_write_time_does_not_grow_with_the_store(self) -> None:
        """Writes slowed as the store grew: 8 ms at a hundred, 21 ms at two thousand.

        Near-duplicate detection orders by updated_at, which had no index to satisfy it,
        so every write sorted the whole table.
        """

        from contextlib import closing

        with closing(sqlite3.connect(self.store.database_path)) as connection:
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        self.assertIn("memories_recent_by_category", indexes)

    def test_operations_leave_no_open_connection_behind(self) -> None:
        """`with sqlite3.Connection:` commits or rolls back -- it never closes.

        Verified against CPython: that context manager only manages the transaction.
        Every one of storage.py's 17 `with self._connect() as connection:` blocks
        used to exit with the connection still open. On POSIX that was invisible,
        because unlinking or replacing a file that some process still has open is
        legal. On Windows it is not: SQLite opens its database file without
        FILE_SHARE_DELETE, so a connection MemoryStore forgot to close blocked
        deleting, renaming or replacing memorysafe.sqlite3 the next time anything
        touched it -- doctor.py's support-bundle copy, `memorysafe migrate`, or a
        user just moving their store. That surfaced as 66 CI failures on
        windows-latest, all `PermissionError: [WinError 32] ... being used by
        another process` in tearDown.

        "The file can be deleted afterwards" is not a portable check: on POSIX it
        passes whether or not the connection was ever closed, so it would not have
        caught this bug here. The property that is actually broken, and that is
        observable identically on every platform, is simpler: no sqlite3.Connection
        object MemoryStore opened for the operation should still be usable once the
        operation returns. A closed connection raises ProgrammingError on any further
        use, so intercept every connection opened during one remember() call and
        assert each one now raises rather than answering a query.
        """

        opened: list[sqlite3.Connection] = []
        real_connect = sqlite3.connect

        def _tracking_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with mock.patch(
            "memorysafe_chatgpt.storage.sqlite3.connect", side_effect=_tracking_connect
        ):
            self.store.remember("Windows enforces what POSIX quietly allows.", "project")

        self.assertGreater(
            len(opened), 0, "the tracking patch never observed a connection open"
        )
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
