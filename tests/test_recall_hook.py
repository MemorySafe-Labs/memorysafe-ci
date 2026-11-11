"""The SessionStart / UserPromptSubmit recall hook shipped in both plugins.

A fresh session must receive the stored preferences without the assistant having to
choose to look (4 Nov 2026: three fresh sessions on a store holding four preferences never
called MemorySafe). The hook must stay silent on an absent or empty store, never write the
store, never fail a session, and stay within a small token budget.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from memorysafe_chatgpt.storage import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugin" / "scripts"
PREFERENCE = "Use Tailwind for styling and Black for Python formatting"
TASK = "Add a settings page to the app with a dark-mode toggle."


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


recall_hook = _load_script("memorysafe_recall_hook_test", SCRIPTS / "recall_hook.py")


class RecallHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "data" / "memorysafe.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _hook(self, payload: dict) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment["MEMORYSAFE_DB_PATH"] = str(self.database)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "recall_hook.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
            check=False,
        )

    def _store(self) -> MemoryStore:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        return MemoryStore(self.database)

    def _session_start(self) -> subprocess.CompletedProcess:
        return self._hook({"hook_event_name": "SessionStart", "source": "startup"})

    def test_a_fresh_session_receives_the_stored_preference(self) -> None:
        store = self._store()
        store.remember(PREFERENCE, "preference")
        store.remember("Ship the settings page by Friday", "task")
        result = self._session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn(PREFERENCE, output["additionalContext"])
        self.assertIn("[preference]", output["additionalContext"])
        # A task is not carried into every session; memorysafe_find is the way to it.
        self.assertNotIn("Ship the settings page", output["additionalContext"])
        self.assertIn("memorysafe_find", output["additionalContext"])
        self.assertIn("2 active memories", output["additionalContext"])

    def test_a_forgotten_preference_is_not_carried(self) -> None:
        store = self._store()
        kept = store.remember(PREFERENCE, "preference")
        gone = store.remember("Use tabs, not spaces", "preference")
        store.forget(gone["memory_id"], confirm=True)
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(PREFERENCE, context)
        self.assertNotIn("tabs, not spaces", context)
        self.assertEqual(kept["memory_id"][:3], "MS-")

    def test_silent_on_an_empty_store_and_without_one(self) -> None:
        result = self._session_start()
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse(self.database.parent.exists())
        self._store()
        result = self._session_start()
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_the_budget_holds_with_many_long_memories(self) -> None:
        store = self._store()
        for n in range(30):
            store.remember(f"Preference number {n}: " + ("x" * 300), "preference")
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        lines = [line for line in context.splitlines() if line.startswith("- [")]
        self.assertLessEqual(len(lines), recall_hook.MAX_MEMORIES)
        self.assertLessEqual(sum(len(line) for line in lines), recall_hook.MAX_CHARS_TOTAL)
        self.assertTrue(all(len(line) <= len("- [preference] ") + recall_hook.MAX_CHARS_EACH for line in lines))

    def test_preferences_are_carried_before_decisions(self) -> None:
        """On a well-used store the twelve lines were the newest decisions and none of
        the preferences: decisions score higher and nearly everything is
        protected. A preference applies to every session; a decision is found by its task."""
        store = self._store()
        store.remember("Write commit messages in the imperative mood", "preference")
        for n in range(14):
            store.remember(f"Decided on {n + 1} March 2027 that module {n} ships behind a flag", "decision")
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        lines = [line for line in context.splitlines() if line.startswith("- [")]
        self.assertTrue(lines[0].startswith("- [preference] Write commit messages"), lines[0])

    def test_among_one_kind_the_ones_that_came_up_are_carried_first(self) -> None:
        store = self._store()
        for n in range(20):
            store.remember(f"Preference {n}: keep the {['alpha','bravo','charlie','delta','echo','foxtrot','golf','hotel','india','juliet','kilo','lima','mike','november','oscar','papa','quebec','romeo','sierra','tango'][n]} module under strict typing", "preference")
        for _ in range(4):
            store.find("tango module strict typing", 1)
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        lines = [line for line in context.splitlines() if line.startswith("- [")]
        self.assertIn("tango", lines[0])

    def test_two_lines_saying_the_same_thing_are_carried_once(self) -> None:
        store = self._store()
        store.remember("Always answer in plain language, the answer first", "preference")
        store.remember("Always answer in plain language with the answer first, please", "decision")
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context.count("plain language"), 1)

    def test_in_hosted_mode_the_file_left_on_this_computer_is_not_carried(self) -> None:
        """The local file is the copy the person left behind when they chose their hosted
        account; the account may since have changed or erased what it holds."""
        self._store().remember(PREFERENCE, "preference")
        state = self.database.parent.parent / "runtime-state"
        state.mkdir(parents=True)
        (state / "storage-mode.json").write_text(json.dumps({"mode": "hosted"}), encoding="utf-8")
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn(PREFERENCE, context)
        self.assertIn("hosted MemorySafe account", context)
        self.assertIn("memorysafe_find", context)
        task = json.loads(self._hook({"prompt": TASK}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("on this computer", task)

    def test_a_secret_or_a_contact_saved_on_request_is_never_carried(self) -> None:
        """An explicit remember has no filter, so the store can hold a card number or someone's
        phone number. Carried, it would be put into every session and sent to the provider by
        a conversation that never asked for it. It stays findable."""
        store = self._store()
        store.remember(PREFERENCE, "preference")
        store.remember("My card number is 4111 1111 1111 1111 for the hosting bill", "decision")
        store.remember("Always sign releases with sk_live_ABCDEFGHIJKLMNOPQRSTUV0123", "preference")
        store.remember("Jordan can be reached at someone@example.com or (514) 555-0142", "personal")
        store.remember("I am allergic to penicillin", "safety")
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        for kept in (PREFERENCE, "allergic to penicillin"):
            self.assertIn(kept, context)
        for secret in ("4111", "sk_live", "someone@example.com", "555-0142"):
            self.assertNotIn(secret, context)
        self.assertEqual(len(store.find("card number hosting bill", 5, record=False)), 1)

    def test_a_health_note_is_carried_only_as_a_safety_note(self) -> None:
        """A safety note is in every session on purpose. The same subject in a personal fact
        or a decision would ride into every conversation and to the provider unasked."""
        store = self._store()
        store.remember(PREFERENCE, "preference")
        store.remember("I am allergic to penicillin", "safety")
        store.remember("I was diagnosed with ADHD last year", "personal")
        store.remember("We decided I take 20 mg of atorvastatin each night", "decision")
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        for kept in (PREFERENCE, "allergic to penicillin"):
            self.assertIn(kept, context)
        for private in ("ADHD", "atorvastatin"):
            self.assertNotIn(private, context)
        self.assertEqual(len(store.find("diagnosed ADHD", 5, record=False)), 1)

    def test_a_fact_that_adds_to_or_negates_another_is_not_a_duplicate(self) -> None:
        """The check compared shared words with the shorter memory, so "allergic to penicillin
        and peanuts" was dropped after "allergic to penicillin", and "do not use semicolons"
        after "do use semicolons"."""
        from memorysafe_chatgpt.storage import MemoryStore

        store = MemoryStore(self.database)
        store.remember("Allergic to penicillin", "safety")
        store.remember("Allergic to penicillin and peanuts", "safety")
        store.remember("Do use semicolons in JavaScript for the web app", "preference")
        store.remember("Do not use semicolons in JavaScript for the scripts folder", "preference")
        _active, carried = recall_hook.carried_memories(self.database)
        contents = [content for _category, content in carried]
        for kept in ("Allergic to penicillin", "Allergic to penicillin and peanuts",
                     "Do use semicolons in JavaScript for the web app",
                     "Do not use semicolons in JavaScript for the scripts folder"):
            self.assertIn(kept, contents)

    def test_a_preference_used_often_is_carried_however_long_ago_it_was_saved(self) -> None:
        """The window was the newest rows by updated_at, shared by every kind. A recall moves
        last_used_at and recall_count, never updated_at, so a store with more protected rows
        than the window lost the preference saved long ago and recalled every week."""
        import sqlite3
        from contextlib import closing
        from unittest.mock import patch

        from memorysafe_chatgpt.storage import MemoryStore

        store = MemoryStore(self.database)
        old = store.remember("Always answer in British English", "preference")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE memories SET updated_at = '2025-01-01T00:00:00+00:00', created_at = '2025-01-01T00:00:00+00:00', "
                "recall_count = 400, last_used_at = '2026-11-01T00:00:00+00:00' WHERE id = ?",
                (old["memory_id"],),
            )
            rows = [(f"MS-NEWER-{n}", f"Decision number {n} about the harbour project", "decision",
                     "2026-10-01T00:00:00+00:00") for n in range(40)]
            for memory_id, content, category, moment in rows:
                connection.execute(
                    "INSERT INTO memories (id, content, normalized_content, category, importance, confidence, protected, "
                    "state, created_at, updated_at, source) VALUES (?, ?, ?, ?, 0.85, 0.95, 1, 'active', ?, ?, 'manual')",
                    (memory_id, content, content.lower(), category, moment, moment),
                )
            connection.commit()
        with patch.object(recall_hook, "MAX_CONSIDERED", 10):
            _active, carried = recall_hook.carried_memories(self.database)
        self.assertIn(("preference", "Always answer in British English"), carried)

    def test_a_reader_that_cannot_see_pending_changes_stays_silent(self) -> None:
        """The immutable fallback ignores the write-ahead log, so mid-write it showed a memory
        forgotten a moment ago as still active."""
        self._store().remember(PREFERENCE, "preference")
        real = recall_hook.sqlite3.connect

        def picky(target, *args, **kwargs):
            if "immutable" not in str(target):
                raise recall_hook.sqlite3.OperationalError("unable to open database file")
            return real(target, *args, **kwargs)

        wal = self.database.with_name(self.database.name + "-wal")
        with unittest.mock.patch.object(recall_hook.sqlite3, "connect", picky):
            self.assertTrue(recall_hook.carried_memories(self.database)[1])
            wal.write_bytes(b"pending")
            self.assertEqual(recall_hook.carried_memories(self.database), (0, []))
        wal.unlink()

    def test_a_task_prompt_gets_the_find_hint_a_question_does_not(self) -> None:
        self._store().remember(PREFERENCE, "preference")
        task = json.loads(self._hook({"prompt": TASK}).stdout)["hookSpecificOutput"]
        self.assertEqual(task["hookEventName"], "UserPromptSubmit")
        self.assertIn("memorysafe_find", task["additionalContext"])
        self.assertEqual(self._hook({"prompt": "What time is it in Toronto?"}).stdout, "")

    def test_a_task_prompt_on_an_empty_store_is_silent(self) -> None:
        self._store()
        self.assertEqual(self._hook({"prompt": TASK}).stdout, "")

    def test_reading_never_writes_the_store(self) -> None:
        self._store().remember(PREFERENCE, "preference")
        before = (self.database.read_bytes(), self.database.stat().st_mtime_ns)
        self._session_start()
        self._hook({"prompt": TASK})
        self.assertEqual((self.database.read_bytes(), self.database.stat().st_mtime_ns), before)

    def test_read_only_falls_back_when_mode_ro_is_refused(self) -> None:
        """Apple's system Python refuses 'file:...?mode=ro' on some paths; the hook then
        reads with immutable=1 instead of going silent (4 Nov 2026)."""
        self._store().remember(PREFERENCE, "preference")
        real = recall_hook.sqlite3.connect

        def picky(target, *args, **kwargs):
            if target.endswith("?mode=ro"):
                raise recall_hook.sqlite3.OperationalError("unable to open database file")
            return real(target, *args, **kwargs)

        from unittest.mock import patch
        with patch.object(recall_hook.sqlite3, "connect", picky):
            active, memories = recall_hook.carried_memories(self.database)
        self.assertEqual(active, 1)
        self.assertIn(PREFERENCE, memories[0][1])

    def test_bad_input_is_silent(self) -> None:
        for payload in ("not json", "[]", "42"):
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "recall_hook.py")],
                input=payload, capture_output=True, text=True, check=False,
                env={**os.environ, "MEMORYSAFE_DB_PATH": str(self.database)}, timeout=30,
            )
            self.assertEqual((result.returncode, result.stdout), (0, ""), payload)

    @unittest.skipIf(os.name == "nt", "invokes /bin/sh directly; the wrapper is a POSIX shell script")
    def test_the_wrapper_exits_zero_whatever_happens(self) -> None:
        """Exit 2 from a SessionStart hook would stop the session from starting."""
        lonely = Path(self.temporary.name) / "lonely"
        lonely.mkdir()
        shutil.copy2(SCRIPTS / "recall_hook", lonely / "recall_hook")
        for script in (SCRIPTS / "recall_hook", lonely / "recall_hook"):
            result = subprocess.run(
                ["/bin/sh", str(script)], input="not json", capture_output=True, text=True, check=False,
                env={**os.environ, "MEMORYSAFE_INSTALL_ROOT": self.temporary.name}, timeout=30,
            )
            self.assertEqual((result.returncode, result.stdout), (0, ""), script)

    def test_both_events_are_registered_through_the_wrapper(self) -> None:
        hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text())
        command = {"type": "command", "command": '/bin/sh "${CLAUDE_PLUGIN_ROOT}/scripts/recall_hook"'}
        self.assertEqual(hooks["hooks"]["SessionStart"][0]["hooks"], [command])
        self.assertIn(command, hooks["hooks"]["UserPromptSubmit"][0]["hooks"])


if __name__ == "__main__":
    unittest.main()
