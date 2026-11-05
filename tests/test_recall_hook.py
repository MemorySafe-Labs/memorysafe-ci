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
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from memorysafe_chatgpt.storage import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugin" / "scripts"
PREFERENCE = "Use Tailwind for styling and Black for Python formatting"
TASK = "Add a settings page to the app with a dark-mode toggle."
SUPPORT_LINK = "The support link shown to users is always /v2/internal/support; never an email address."
DISPLAY_TIME = "All displayed times use display_time() from app/time.py in the America/Toronto zone."
FEATURE_FLAGS = 'All feature flags are read through flags.enabled("name") from app/flags.py; never os.environ.'
CSS_PLACE = "All CSS lives in public/style.css and nowhere else."
MONEY = "All money is stored as integer cents in fields ending in _cents; never floats."
# A fictional project brief in the shape of the pilot's round 3: plain lasting rules, one each.
BRIEF = (
    "The settings file is app.settings.toml at the repo root; never create config.* files.",
    "All user-facing strings go through t() from app/i18n.py.",
    "Tests are plain pytest functions in tests/, one file per module named test_<module>.py.",
    "HTTP routes are registered only in app/routes.py, all under the prefix /v2/internal.",
    "All logging goes through log_event(name, **fields) from app/log.py.",
    MONEY,
    CSS_PLACE,
    FEATURE_FLAGS,
    "Every change adds one line to CHANGELOG.md under the Unreleased heading.",
    "All lines stay under 100 characters.",
    DISPLAY_TIME,
    "All identifiers are ULIDs from new_id() in app/ids.py; never uuid4.",
    SUPPORT_LINK,
)
_WORDS = ("cache", "schema", "fixtures", "templates", "assets", "secrets", "queues", "reports")


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
        self.assertIn(f"call memorysafe_find for the other {30 - len(lines)}", context)
        self.assertLessEqual(sum(len(line) for line in lines), recall_hook.MAX_CHARS_TOTAL)
        self.assertTrue(all(len(line) <= len("- [preference] ") + recall_hook.MAX_CHARS_EACH for line in lines))

    def test_a_task_prompt_gets_the_rules_once_per_session_then_the_find_hint(self) -> None:
        """0.4.32 put the preferences on every task prompt; 0.4.34 puts them on the first
        task prompt of a session only (a pilot round measured the repeat as ~15% more
        spend), later task prompts get the one-line find hint, and a session start,
        resume or compaction makes the next task prompt carry the rules again."""
        store = self._store()
        store.remember(PREFERENCE, "preference")
        store.remember("Ship the settings page by Friday", "task")
        first = json.loads(self._hook({"prompt": TASK, "session_id": "s1"}).stdout)["hookSpecificOutput"]
        self.assertEqual(first["hookEventName"], "UserPromptSubmit")
        self.assertIn("Stored preferences to apply in this task:", first["additionalContext"])
        self.assertIn("- " + PREFERENCE, first["additionalContext"])
        self.assertNotIn("Ship the settings page", first["additionalContext"])
        self.assertNotIn("memorysafe_find", first["additionalContext"])
        self.assertNotIn("check each one holds", first["additionalContext"])
        second = json.loads(self._hook({"prompt": TASK, "session_id": "s1"}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Stored preferences", second)
        self.assertIn("memorysafe_find", second)
        other = json.loads(self._hook({"prompt": TASK, "session_id": "s2"}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Stored preferences", other)
        self._hook({"hook_event_name": "SessionStart", "source": "compact", "session_id": "s1"})
        again = json.loads(self._hook({"prompt": TASK, "session_id": "s1"}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Stored preferences", again)
        # Without a session id there is nothing to remember by: the rules every time.
        for _ in range(2):
            self.assertIn("Stored preferences",
                          json.loads(self._hook({"prompt": TASK}).stdout)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self._hook({"prompt": "What time is it in Toronto?", "session_id": "s1"}).stdout, "")
        markers = self.database.parent.parent / "hooks" / "rules-shown"
        self.assertEqual(sorted(p.name for p in markers.iterdir()), ["s1", "s2"])

    def test_task_like_is_judged_on_any_sentence_not_only_the_first(self) -> None:
        """0.4.33: the pilot's session-1 prompt states two facts and then asks for a change;
        the rules must fire for it. Greetings, reply instructions and questions get nothing."""
        self._store().remember(PREFERENCE, "preference")
        for prompt in (
            "The settings file is app.settings.toml at the repo root. Add a footer under the list in render_list.py.",
            "Please add a dark-mode toggle to the settings page.",
            "I need you to fix the failing parser test.",
            "Hi. Let's refactor the auth module to use the new client.",
            " ".join(["word"] * 30) + ".",
        ):
            self.assertTrue(recall_hook.is_task_like(prompt), prompt)
            self.assertIn("Stored preferences to apply in this task:",
                          json.loads(self._hook({"prompt": prompt}).stdout)["hookSpecificOutput"]["additionalContext"], prompt)
        for prompt in ("Hi.", "Reply with only the word OK. Do not use any tools.", "What time is it in Toronto?",
                       "Which test framework does this repo use?"):
            self.assertFalse(recall_hook.is_task_like(prompt), prompt)
            self.assertEqual(self._hook({"prompt": prompt}).stdout, "", prompt)

    def test_task_rules_stay_within_their_budget(self) -> None:
        store = self._store()
        for n in range(20):
            store.remember(f"Preference number {n}: " + ("y" * 150), "preference")
        context = json.loads(self._hook({"prompt": TASK}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context), recall_hook.TASK_RULES_MAX_CHARS + 1)
        self.assertIn("- Preference number", context)
        self.assertNotIn("memorysafe_find", context)

    def test_a_brief_stated_once_is_carried_whole_at_session_start(self) -> None:
        """0.4.36: round 3 of the pilot (4 Nov 2026) stated a 25-fact brief once; the block
        showed 12, the model never searched for the other 13, and every fact the brief-once
        arm missed was one of those. A brief that fits in about a thousand tokens goes whole."""
        store = self._store()
        for fact in BRIEF:
            store.remember(fact, "preference")
        context = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        for fact in BRIEF:
            self.assertIn(fact, context)
        self.assertIn(f"{len(BRIEF)} active memories", context)
        self.assertNotIn("for the other", context)

    def test_task_rules_lead_with_the_memories_the_task_names(self) -> None:
        """0.4.36: round 3's task block held the first four facts whatever the task, and S2
        missed the support link and the local-time rule it asked for by name."""
        store = self._store()
        for fact in BRIEF:
            store.remember(fact, "preference")
        prompt = ('Add a feature-flagged "late payment notice" banner to the invoice page, with '
                  "the support link and the due date in local time; style it.")
        lines = json.loads(self._hook({"prompt": prompt}).stdout)["hookSpecificOutput"]["additionalContext"].splitlines()
        self.assertEqual(lines[0], "Stored preferences to apply in this task:")
        # Each shares two words with the task, more than any other fact.
        self.assertEqual(set(lines[1:3]), {"- " + SUPPORT_LINK, "- " + FEATURE_FLAGS})
        for fact in (SUPPORT_LINK, DISPLAY_TIME, FEATURE_FLAGS, CSS_PLACE):
            self.assertIn("- " + fact, lines)
        # Unrelated facts stay at session start, and the block says so in one line.
        self.assertNotIn("- " + MONEY, lines)
        self.assertEqual(lines[-1], recall_hook._TAIL_SHOWN_AT_START)
        self.assertLessEqual(len("\n".join(lines)), recall_hook.TASK_RULES_MAX_CHARS)

    def test_task_rules_reach_memories_the_session_start_block_left_out(self) -> None:
        """A store too big for the session-start block: the task still gets the one memory it
        names, found among RELEVANCE_POOL, and is told memorysafe_find reaches the rest."""
        store = self._store()
        for n in range(recall_hook.MAX_MEMORIES + 5):
            store.remember(f"Rule {n}: module {n} keeps its own {_WORDS[n % len(_WORDS)]} "
                           f"in folder {n} and never in folder {n + 1}", "preference")
        # Last in stored order (protected, importance, updated_at), past the session-start block.
        last = store.remember(SUPPORT_LINK, "preference")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "UPDATE memories SET protected = 0, importance = 0.1, "
                "updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (last["memory_id"],))
        start = json.loads(self._session_start().stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn(SUPPORT_LINK, start)
        prompt = "Add the support link to the footer of the invoice page."
        context = json.loads(self._hook({"prompt": prompt}).stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("- " + SUPPORT_LINK, context)
        self.assertEqual(context.splitlines()[-1], recall_hook._TAIL_FIND)

    def test_relevance_ignores_the_words_every_task_shares(self) -> None:
        terms = recall_hook._terms
        changelog = "Every change adds one line to CHANGELOG.md under the Unreleased heading."
        self.assertEqual(recall_hook.relevance(terms("Add a nightly job that stores exchange rates."), changelog), 0)
        self.assertGreater(recall_hook.relevance(terms("Add a feature-flagged banner."), FEATURE_FLAGS), 0)
        self.assertGreater(recall_hook.relevance(terms("Show the due date in local time."), DISPLAY_TIME), 0)
        self.assertGreater(recall_hook.relevance(terms("Style the banner."), CSS_PLACE), 0)
        self.assertEqual(recall_hook.relevance(terms("Add a settings page to the app."), MONEY), 0)

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
