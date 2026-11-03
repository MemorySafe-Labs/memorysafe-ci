"""The UserPromptSubmit hook shipped in both plugins.

It now runs for everyone who installs the plugin, including people who never turned
automatic mode on, so it must stay silent for them -- and it must never write to the
store or fail a prompt.
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
FACT = "I prefer concise answers from now on."


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


capture_hook = _load_script("memorysafe_capture_hook_test", SCRIPTS / "capture_hook.py")


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

    def test_reading_the_setting_never_writes_the_store(self) -> None:
        self._store(automatic=True)
        before = (self.database.read_bytes(), self.database.stat().st_mtime_ns)
        self._hook(FACT)
        self.assertEqual((self.database.read_bytes(), self.database.stat().st_mtime_ns), before)

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
            )
            self.assertEqual((result.returncode, result.stdout), (0, ""), script)

    def test_both_plugins_register_it_through_the_wrapper(self) -> None:
        hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text())
        entry = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(entry, {"type": "command", "command": '/bin/sh "${CLAUDE_PLUGIN_ROOT}/scripts/capture_hook"'})

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
