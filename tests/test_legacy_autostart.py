"""Old start-at-login items from a 0.3.x install, which bring the old dashboard back first."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import doctor, legacy_autostart, migrate


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class LegacyAutostartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.startup = (
            self.home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        )
        self.agents = self.home / "Library" / "LaunchAgents"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_the_cmd_the_0_3_x_installer_wrote_is_found(self) -> None:
        item = _write(self.startup / "MemorySafe Dashboard Service.cmd", "call run_setup_service.cmd")
        self.assertEqual(legacy_autostart.find(self.home, {}, "win32"), [item])

    def test_the_other_extensions_count_too(self) -> None:
        names = ["MemorySafe Dashboard Service.lnk", "MemorySafe Dashboard Service.bat", "MemorySafe Dashboard Service.vbs"]
        for name in names:
            _write(self.startup / name)
        self.assertEqual([path.name for path in legacy_autostart.find(self.home, {}, "win32")], sorted(names))

    def test_an_item_that_was_already_retired_is_not_reported_again(self) -> None:
        _write(self.startup / ("MemorySafe Dashboard Service.cmd" + legacy_autostart.RETIRED_SUFFIX))
        self.assertEqual(legacy_autostart.find(self.home, {}, "win32"), [])

    def test_other_programs_startup_items_are_never_touched(self) -> None:
        _write(self.startup / "Dropbox.lnk")
        _write(self.startup / "MemorySafe Notes.cmd")
        self.assertEqual(legacy_autostart.find(self.home, {}, "win32"), [])

    def test_the_appdata_in_the_environment_wins_over_the_home_folder(self) -> None:
        roaming = self.home / "elsewhere"
        item = _write(roaming / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "MemorySafe Dashboard Service.cmd")
        self.assertEqual(legacy_autostart.find(self.home, {"APPDATA": str(roaming)}, "win32"), [item])

    def test_the_mac_setup_agent_is_found_but_the_tunnel_agent_is_not(self) -> None:
        agent = _write(self.agents / "ca.memorysafe.beta.setup.plist")
        _write(self.agents / "ca.memorysafe.beta.tunnel.plist")
        self.assertEqual(legacy_autostart.find(self.home, {}, "darwin"), [agent])

    def test_linux_has_nothing_to_report(self) -> None:
        _write(self.agents / "ca.memorysafe.beta.setup.plist")
        self.assertEqual(legacy_autostart.find(self.home, {}, "linux"), [])

    def test_the_remedy_names_the_file_and_leaves_the_tunnel_alone_on_a_mac(self) -> None:
        agent = _write(self.agents / "ca.memorysafe.beta.setup.plist")
        text = legacy_autostart.remedy([agent], "darwin")
        self.assertIn("launchctl bootout", text)
        self.assertIn(str(agent), text)
        self.assertIn("ca.memorysafe.beta.tunnel", text)
        item = _write(self.startup / "MemorySafe Dashboard Service.cmd")
        self.assertIn(str(item), legacy_autostart.remedy([item], "win32"))
        self.assertEqual(legacy_autostart.remedy([], "win32"), "")

    def test_a_migrate_dry_run_lists_them(self) -> None:
        item = _write(self.startup / "MemorySafe Dashboard Service.cmd")
        with patch.object(migrate.sys, "platform", "win32"), patch.dict(
            migrate.os.environ, {"APPDATA": str(self.home / "AppData" / "Roaming")}
        ):
            found = migrate.find_legacy(self.home, self.home / "data-root")
        self.assertEqual(found["old_autostart"], [str(item)])

    def test_the_doctor_names_the_item_that_is_keeping_the_old_dashboard_on_the_port(self) -> None:
        item = _write(self.startup / "MemorySafe Dashboard Service.cmd")
        running = {"version": "0.3.7", "pid": None}
        with patch.object(doctor, "_dashboard_report", return_value=running), patch.object(
            doctor.sys, "platform", "win32"
        ), patch.object(doctor.Path, "home", return_value=self.home), patch.dict(
            doctor.os.environ, {"APPDATA": str(self.home / "AppData" / "Roaming")}
        ):
            result = doctor._dashboard_version_check()
        self.assertEqual(result["status"], "warning")
        self.assertIn(str(item), result["summary"])
        self.assertEqual(result["details"]["old_autostart"], [item.name])


if __name__ == "__main__":
    unittest.main()
