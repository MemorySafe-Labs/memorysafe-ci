"""The first run on Windows, as a new user meets it.

From a clean install of the public 0.5.0 extension on Windows 11 (Microsoft Store
Claude Desktop) on 14 Nov 2026: console windows on every start, the extension that
served the dashboard reported as not connected, a cost as the first number shown, a
"CAPTURING" badge beside a switch reading OFF, a score that fell when the first memory
was saved, and a Claude Code in a terminal that could not see the desktop app's store.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import agents, claude_launcher, device_link, server

from memorysafe_chatgpt.storage import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = (ROOT / "src" / "memorysafe_chatgpt" / "dashboard.py").read_text(encoding="utf-8")


class TheHostIsRecordedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "state"

    def test_claude_desktop_records_the_extension_it_started(self) -> None:
        with patch.dict(os.environ, {"MEMORYSAFE_HOST": agents.DESKTOP_HOST, "MEMORYSAFE_STATE_DIR": str(self.state_dir)}):
            claude_launcher._record_the_host()
        record = (self.state_dir / agents.DESKTOP_EXTENSION_RECORD)
        self.assertTrue(record.exists())
        import json

        # Read as JSON: on Windows the file escapes every backslash in the path.
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["plugin_dir"], str(ROOT.resolve()))

    def test_another_host_records_nothing(self) -> None:
        environment = {"MEMORYSAFE_STATE_DIR": str(self.state_dir)}
        with patch.dict(os.environ, environment):
            os.environ.pop("MEMORYSAFE_HOST", None)
            claude_launcher._record_the_host()
        self.assertFalse((self.state_dir / agents.DESKTOP_EXTENSION_RECORD).exists())

    def test_the_extension_manifest_names_its_host(self) -> None:
        import json

        manifest = json.loads((ROOT / "plugin" / "manifest.json").read_text(encoding="utf-8"))
        config = manifest["server"]["mcp_config"]
        self.assertEqual(config["env"]["MEMORYSAFE_HOST"], agents.DESKTOP_HOST)
        self.assertEqual(config["platform_overrides"]["win32"]["env"]["MEMORYSAFE_HOST"], agents.DESKTOP_HOST)

    def test_the_plugins_for_claude_code_and_codex_do_not_claim_to_be_the_extension(self) -> None:
        self.assertNotIn("MEMORYSAFE_HOST", (ROOT / "plugin" / "mcp.json").read_text(encoding="utf-8"))


class AnEmptyStoreHasNoScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MemoryStore(Path(self.temporary.name) / "memorysafe.sqlite3")

    def test_no_score_until_a_memory_is_saved(self) -> None:
        self.assertIsNone(self.store.health()["memory_health"])

    def test_the_first_good_memory_gives_a_score(self) -> None:
        self.store.remember("My favourite coffee is espresso.", category="preference", importance=0.85, confidence=0.95)
        self.assertIsInstance(self.store.health()["memory_health"], int)

    def test_the_health_tool_says_empty_and_agrees_with_capture_status(self) -> None:
        with patch.object(server, "_store", return_value=self.store), patch.object(server, "_hosted_mode", return_value=False):
            result = server._dashboard_data()
        self.assertEqual(result.status, "empty")
        self.assertIsNone(result.memory_health)
        self.assertIn("empty", result.capture_status)


class TheDashboardSaysWhatItMeansTests(unittest.TestCase):
    def test_no_badge_reads_capturing(self) -> None:
        """"active" means something was saved recently, by hand too, and sat in green
        beside an Automatic capture switch reading OFF."""
        self.assertNotIn('"CAPTURING"', DASHBOARD_HTML)
        self.assertIn("SAVED TODAY", DASHBOARD_HTML)
        self.assertIn("NOTHING SAVED YET", DASHBOARD_HTML)

    def test_a_store_that_does_not_pay_off_yet_leads_with_what_it_holds(self) -> None:
        self.assertNotIn("EXTRA TOKENS / TURN", DASHBOARD_HTML)
        self.assertIn("MEMORIES KEPT", DASHBOARD_HTML)

    def test_nothing_is_said_about_a_sign_in_that_never_was(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result = device_link.sign_out(Path(folder))
        self.assertFalse(result["was_linked"])
        self.assertIn("was_linked !== false", DASHBOARD_HTML)


class NoConsoleWindowTests(unittest.TestCase):
    def test_a_browser_opens_without_cmd_on_windows(self) -> None:
        """cmd /c start, from a server with no console, flashed a console window."""
        opened = []
        with patch.object(server.sys, "platform", "win32"), \
             patch.object(server.os, "startfile", opened.append, create=True), \
             patch.object(server.subprocess, "Popen", side_effect=AssertionError("no process")), \
             patch.dict(os.environ, {"MEMORYSAFE_OPEN_DASHBOARD": "1"}):
            self.assertEqual(server._open_dashboard_window("http://127.0.0.1:8765/dashboard"), "browser")
        self.assertEqual(opened, ["http://127.0.0.1:8765/dashboard"])

    def test_the_runtime_build_starts_nothing_with_a_window(self) -> None:
        source = (ROOT / "plugin" / "scripts" / "install_runtime.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("subprocess.run("), source.count("**_NO_WINDOW"))


class OneStoreForTheDesktopAppAndATerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (ROOT / "plugin" / "scripts" / "start.cmd").read_text(encoding="utf-8")

    def test_the_desktop_apps_store_is_looked_for_before_the_default(self) -> None:
        lookup = self.script.index('if "%MEMORYSAFE_INSTALL_ROOT%"=="" call :desktop_store')
        default = self.script.index('if "%MEMORYSAFE_INSTALL_ROOT%"=="" set "MEMORYSAFE_INSTALL_ROOT=%LOCALAPPDATA%\\MemorySafe"')
        self.assertLess(lookup, default)

    def test_a_store_already_in_the_usual_place_always_wins(self) -> None:
        routine = self.script[self.script.index("\n:desktop_store"):self.script.index("\n:stage_plugin")]
        keep = routine.index('if exist "%LOCALAPPDATA%\\MemorySafe\\data\\memorysafe.sqlite3" exit /b 0')
        self.assertLess(keep, routine.index("for /d"))
        self.assertIn("LocalCache\\Local\\MemorySafe", routine)


if __name__ == "__main__":
    unittest.main()
