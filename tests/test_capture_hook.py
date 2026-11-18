"""The UserPromptSubmit hook shipped in both plugins.

It runs for everyone who installs the plugin, including people who never turned
automatic mode on, so it must stay silent for them and never fail a prompt. Since
0.4.30 (4 Nov 2026) it saves a plainly stated preference itself when automatic mode is
on, through the store's own policy and governance; everything else is still a hint to
the model, and nothing is written for anyone with automatic mode off.
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
from unittest.mock import patch

from memorysafe_chatgpt.storage import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugin" / "scripts"
FACT = "I'm working on the Acme launch for Globex this quarter."
PREFERENCE = "Always use Black for Python formatting."
SENSITIVE = "Always use my password hunter2 for the staging box."


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


capture_hook = _load_script("memorysafe_capture_hook_test", SCRIPTS / "capture_hook.py")


def recall_hook_module():
    """The session-start hook, which carries the same reader."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("recall_hook_for_capture_tests", SCRIPTS / "recall_hook.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "data" / "memorysafe.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _hook(self, prompt: str) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment["MEMORYSAFE_DB_PATH"] = str(self.database)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "capture_hook.py")],
            input=json.dumps({"prompt": prompt}),
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
            check=False,
        )

    def _store(self, *, automatic: bool) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        MemoryStore(self.database).set_automatic_mode(automatic)

    def test_silent_when_automatic_mode_is_off(self) -> None:
        self._store(automatic=False)
        result = self._hook(FACT)
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_hints_on_a_stated_fact_when_automatic_mode_is_on(self) -> None:
        self._store(automatic=True)
        result = self._hook(FACT)
        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertIn("memorysafe_auto_capture", output["additionalContext"])

    def test_silent_on_a_question_even_when_on(self) -> None:
        self._store(automatic=True)
        self.assertEqual(self._hook("What time is it in Toronto?").stdout, "")

    def test_silent_without_a_store_and_never_creates_one(self) -> None:
        result = self._hook(FACT)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertFalse(self.database.parent.exists())

    def test_a_plain_fact_never_writes_the_store(self) -> None:
        self._store(automatic=True)
        before = (self.database.read_bytes(), self.database.stat().st_mtime_ns)
        self._hook(FACT)
        self.assertEqual((self.database.read_bytes(), self.database.stat().st_mtime_ns), before)

    def test_a_plain_preference_is_saved_without_the_model(self) -> None:
        """4 Nov 2026: two pilot rounds in which the model ignored the hint and saved
        nothing. A preference stated this plainly is saved by the hook itself."""
        self._store(automatic=True)
        result = self._hook(PREFERENCE)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("MemorySafe saved automatically", context)
        self.assertIn("Always use Black for Python formatting", context)
        found = MemoryStore(self.database).find("Black", limit=5)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["category"], "preference")
        # Saying it again merges rather than duplicating.
        self._hook(PREFERENCE)
        self.assertEqual(len(MemoryStore(self.database).find("Black", limit=5)), 1)

    def test_a_plain_preference_with_automatic_mode_off_writes_nothing(self) -> None:
        self._store(automatic=False)
        before = (self.database.read_bytes(), self.database.stat().st_mtime_ns)
        self.assertEqual(self._hook(PREFERENCE).stdout, "")
        self.assertEqual((self.database.read_bytes(), self.database.stat().st_mtime_ns), before)

    def test_the_store_policy_still_refuses_what_it_refuses(self) -> None:
        """The hook's write goes through capture_policy: a password is not saved, and the
        model is left with the usual hint."""
        self._store(automatic=True)
        result = self._hook(SENSITIVE)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("saved automatically", context)
        self.assertEqual(MemoryStore(self.database).find("hunter2", limit=5), [])

    def test_clear_preferences_picks_only_plain_statements(self) -> None:
        text = ("Always use Black for Python formatting. Can you also add tests? "
                "I prefer pytest over unittest. The launch moved to Friday. "
                "From now on, no new dependencies without asking.")
        self.assertEqual(capture_hook.clear_preferences(text), [
            "Always use Black for Python formatting.",
            "I prefer pytest over unittest.",
            "From now on, no new dependencies without asking.",
        ])
        self.assertEqual(capture_hook.clear_preferences("Always use Black?"), [])
        self.assertEqual(capture_hook.clear_preferences("Always " + "x" * 300), [])
        self.assertEqual(capture_hook.clear_preferences("Add a settings page."), [])

    def test_an_instruction_for_this_one_piece_of_work_is_not_a_preference(self) -> None:
        """Each of these matched the preference pattern, was saved as a protected preference
        and was then carried into every later session as a standing order (10 Nov 2026)."""
        for remark in (
            "Never mind, I fixed it.",
            "Don't add tests yet.",
            "Do not create a PR until I say so.",
            "Use the staging database for this run, not prod.",
            "Going forward with the plan, please implement step 2.",
            "Never mind the linter for now.",
            "Don't use the cache today.",
        ):
            self.assertEqual(capture_hook.clear_preferences(remark), [], remark)
        for standing in (
            "Always use Black for Python formatting.",
            "Never deploy on Fridays.",
            "From now on, no new dependencies.",
            "Going forward, write tests first.",
            "I prefer pytest over unittest.",
            "Don't use tabs, ever. Always spaces.",
        ):
            self.assertTrue(capture_hook.clear_preferences(standing), standing)

    def test_a_passing_remark_is_not_written_even_with_automatic_mode_on(self) -> None:
        store = MemoryStore(self.database)
        store.set_automatic_mode(True)
        self._hook("Don't add tests yet.")
        self._hook("Never mind, I fixed it.")
        self.assertEqual(store.find("", 5, record=False), [])

    def test_in_hosted_mode_the_hook_saves_nothing_here_and_says_nothing(self) -> None:
        """Saving to the file on this computer would put the preference where no assistant
        reads it, and "saved, do not save it again" would keep it from the account."""
        store = MemoryStore(self.database)
        store.set_automatic_mode(True)
        state = self.database.parent.parent / "runtime-state"
        state.mkdir(parents=True)
        (state / "storage-mode.json").write_text(json.dumps({"mode": "hosted"}), encoding="utf-8")
        result = self._hook(PREFERENCE)
        self.assertEqual((result.returncode, result.stdout), (0, ""))
        self.assertEqual(store.find("", 5, record=False), [])

    def test_read_only_falls_back_when_mode_ro_is_refused(self) -> None:
        """Apple's system Python refuses 'file:...?mode=ro' on some paths; the hook then
        reads with immutable=1 instead of going silent (4 Nov 2026)."""
        self._store(automatic=True)
        real = capture_hook.sqlite3.connect
        calls = []

        def picky(target, *args, **kwargs):
            calls.append(target)
            if target.endswith("?mode=ro"):
                raise capture_hook.sqlite3.OperationalError("unable to open database file")
            return real(target, *args, **kwargs)

        with patch.object(capture_hook.sqlite3, "connect", picky):
            self.assertTrue(capture_hook.automatic_mode_enabled(self.database))
        self.assertTrue(any(c.endswith("&immutable=1") for c in calls))

    def test_automatic_mode_is_not_read_from_a_copy_that_misses_pending_changes(self) -> None:
        """The immutable fallback ignores the write-ahead log: it could read automatic mode as
        on just after it was switched off, and the hook would save what the person said."""
        store = MemoryStore(self.database)
        store.set_automatic_mode(True)
        real = capture_hook.sqlite3.connect

        def picky(target, *args, **kwargs):
            if "immutable" not in str(target):
                raise capture_hook.sqlite3.OperationalError("unable to open database file")
            return real(target, *args, **kwargs)

        wal = self.database.with_name(self.database.name + "-wal")
        with patch.object(capture_hook.sqlite3, "connect", picky):
            self.assertTrue(capture_hook.automatic_mode_enabled(self.database))
            wal.write_bytes(b"pending")
            self.assertFalse(capture_hook.automatic_mode_enabled(self.database))
        wal.unlink()

    def test_the_fallback_reads_a_store_whose_log_is_already_in_the_main_file(self) -> None:
        """A checkpoint copies the log into the main file but leaves the log its size, so
        while an assistant has the store open the log is never empty. Refusing whenever it
        was not empty meant the fallback never ran, and on Apple's system Python capture and
        recall went silent for good."""
        import sqlite3 as real_sqlite
        from contextlib import closing

        store = MemoryStore(self.database)
        store.set_automatic_mode(True)
        # Held open as an assistant would, and closed inside the test: tearDown removes the
        # folder before cleanups run, and Windows deletes no file that is open.
        held = real_sqlite.connect(self.database)
        try:
            self._fallback_with_the_log_copied(held)
        finally:
            held.close()

    def _fallback_with_the_log_copied(self, held) -> None:
        import sqlite3 as real_sqlite
        from contextlib import closing

        held.execute("PRAGMA wal_autocheckpoint = 0")
        held.execute("SELECT count(*) FROM settings").fetchone()  # opens the store, as an assistant has
        with closing(real_sqlite.connect(self.database)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute("INSERT INTO settings(key, value, updated_at) VALUES ('probe', '1', 'now')")
            writer.commit()
        wal = self.database.with_name(self.database.name + "-wal")
        self.assertGreater(wal.stat().st_size, 0)
        self.assertTrue(capture_hook.unseen_changes(self.database))
        held.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self.assertGreater(wal.stat().st_size, 0)  # the log keeps its size...
        self.assertFalse(capture_hook.unseen_changes(self.database))  # ...but holds nothing new

        real = capture_hook.sqlite3.connect

        def picky(target, *args, **kwargs):
            if "immutable" not in str(target):
                raise capture_hook.sqlite3.OperationalError("unable to open database file")
            return real(target, *args, **kwargs)

        with patch.object(capture_hook.sqlite3, "connect", picky):
            self.assertTrue(capture_hook.automatic_mode_enabled(self.database))
        self.assertEqual(recall_hook_module().unseen_changes(self.database), False)

    @unittest.skipIf(os.name == "nt", "invokes /bin/sh directly via subprocess, which does not exist on "
                                       "Windows; scripts/capture_hook is a POSIX shell script with no "
                                       "Windows counterpart -- hooks.json registers it unconditionally as "
                                       "'/bin/sh \"...\"' on every platform -- so there is no Windows-side "
                                       "behaviour of this wrapper for a test to cover")
    def test_the_wrapper_exits_zero_whatever_happens(self) -> None:
        """Exit 2 from a UserPromptSubmit hook blocks the user's prompt."""
        lonely = Path(self.temporary.name) / "lonely"
        lonely.mkdir()
        shutil.copy2(SCRIPTS / "capture_hook", lonely / "capture_hook")
        for script in (SCRIPTS / "capture_hook", lonely / "capture_hook"):
            result = subprocess.run(
                ["/bin/sh", str(script)],
                input="not json",
                capture_output=True,
                text=True,
                env={**os.environ, "MEMORYSAFE_INSTALL_ROOT": self.temporary.name},
                timeout=30,
                check=False,
            )
            self.assertEqual((result.returncode, result.stdout), (0, ""), script)

    def test_both_plugins_register_it_through_the_wrapper(self) -> None:
        hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text())
        entries = hooks["hooks"]["UserPromptSubmit"][0]["hooks"]
        self.assertIn({"type": "command", "command": '/bin/sh "${CLAUDE_PLUGIN_ROOT}/scripts/capture_hook"'}, entries)

    def test_windows_falls_back_to_localappdata_not_xdg(self) -> None:
        """The branch was darwin/else-XDG, so Windows landed in the XDG arm and looked
        under ~/.local/share/MemorySafe. It is masked whenever the launcher sets
        MEMORYSAFE_INSTALL_ROOT, so the hook read an absent store in silence.
        """
        with patch.object(capture_hook.sys, "platform", "win32"), \
             patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\t\\AppData\\Local"}, clear=True):
            path = capture_hook.database_path()
        self.assertEqual(
            path, Path("C:\\Users\\t\\AppData\\Local") / "MemorySafe" / "data" / "memorysafe.sqlite3"
        )


if __name__ == "__main__":
    unittest.main()
