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
