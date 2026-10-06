"""memorysafe uninstall: everything MemorySafe put on this computer, taken back off.

There was no uninstaller. Removing MemorySafe from the Windows test machine by hand on
19 Sep meant finding seven separate things, including a 267 MB private runtime that no
assistant knows about, so "remove the plugin" left almost all of it behind.

What these pin down:

- a plugin leaves through its own host's installer, never by editing that host's config;
- Claude Desktop is reported with its one manual step and never touched from here, the
  same boundary connect respects;
- memories are never deleted without --purge, and a --purge copies them out first;
- a failure is reported, never raised: a half-removed install must still say what is left.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import agents, uninstall
from memorysafe_chatgpt.cli import main as cli_main
from memorysafe_chatgpt.migrate import PLUGIN_ID


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _Computer(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.home = root / "home"
        self.home.mkdir()
        self.data_root = self.home / "AppData" / "Local" / "MemorySafe"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.env = {
            "PATH": str(self.bin),
            "APPDATA": str(self.home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "SYSTEMROOT": r"C:\Windows",
        }
        bin_dirs = patch.object(agents, "_POSIX_BIN_DIRS", ())
        bin_dirs.start()
        self.addCleanup(bin_dirs.stop)
        self.calls: list = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cli(self, name: str) -> str:
        path = self.bin / (name + (".exe" if agents.sys.platform == "win32" else ""))
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def _claude_code_plugin(self) -> None:
        _write(self.home / ".claude" / "plugins" / "installed_plugins.json", json.dumps({"plugins": {PLUGIN_ID: [{}]}}))

    def _codex_plugin(self) -> None:
        _write(self.home / ".codex" / "config.toml", f'[plugins."{PLUGIN_ID}"]\nenabled = true\n')

    def _runtime(self) -> None:
        _write(self.data_root / "runtime" / "abc" / "ready", "")
        _write(self.data_root / "python" / "cpython" / "python.exe", "x" * 1024)
        _write(self.data_root / "runtime-state" / "device.json", "{}")
        _write(self.data_root / "data" / "memorysafe.sqlite3", "sqlite")

    def _inventory(self, platform: str | None = None) -> list[dict]:
        return uninstall.inventory(self.home, self.env, platform or agents.sys.platform, self.data_root)

    def _by_kind(self, kind: str, platform: str | None = None) -> list[dict]:
        return [item for item in self._inventory(platform) if item["kind"] == kind]

    def _runner(self, returncode: int = 0):
        def run(command, **kwargs):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, returncode, b"", b"")
        return run

    def _apply(self, purge: bool = False, runner=None):
        return uninstall.apply(self.home, self.env, agents.sys.platform, self.data_root, purge, runner or self._runner())


class InventoryTests(_Computer):
    def test_a_computer_without_memorysafe_has_nothing_to_remove(self) -> None:
        self.assertEqual(self._inventory(), [])

    def test_a_plugin_leaves_through_its_own_hosts_installer(self) -> None:
        claude = self._cli("claude")
        self._claude_code_plugin()
        _write(self.home / ".claude" / "plugins" / "known_marketplaces.json", json.dumps({"memorysafe": {}}))
        item = self._by_kind("plugin")[0]
        self.assertEqual(
            [command[1:] for command in item["commands"]],
            [["plugin", "uninstall", PLUGIN_ID], ["plugin", "marketplace", "remove", "memorysafe"]],
        )
        self.assertEqual(Path(item["commands"][0][0]).resolve(), Path(claude).resolve())

    def test_a_marketplace_that_was_never_added_is_not_removed(self) -> None:
        self._cli("claude")
        self._claude_code_plugin()
        self.assertEqual([command[1:] for command in self._by_kind("plugin")[0]["commands"]],
                         [["plugin", "uninstall", PLUGIN_ID]])

    def test_without_the_hosts_command_the_step_is_the_users(self) -> None:
        self._claude_code_plugin()
        item = self._by_kind("plugin")[0]
        self.assertEqual(item["commands"], [])
        self.assertIn("claude plugin uninstall memorysafe@memorysafe", item["manual"])

    def test_claude_desktop_is_reported_never_removed_from_here(self) -> None:
        """It asks the user to confirm what its extensions do; connect respects the same line."""
        desktop = self.home / "AppData" / "Roaming" / "Claude"
        (desktop / "Claude Extensions" / "local.mcpb.memorysafe-beta.memorysafe").mkdir(parents=True)
        item = self._by_kind("extension", "win32")[0]
        self.assertEqual((item["commands"], item["paths"]), ([], []))
        self.assertIn("Settings > Extensions", item["manual"])

    def test_the_private_runtime_and_state_are_found(self) -> None:
        self._runtime()
        files = self._by_kind("files")
        details = " ".join(item["detail"] for item in files)
        self.assertIn("Python, uv and packages", details)
        self.assertIn("device id, logs", details)
        self.assertGreater(sum(item["bytes"] for item in files), 1000)

    def test_host_caches_and_logs_are_found(self) -> None:
        (self.home / ".claude" / "plugins" / "cache" / "memorysafe").mkdir(parents=True)
        (self.home / ".codex" / "plugins" / "cache" / "memorysafe").mkdir(parents=True)
        logs = self.home / "AppData" / "Local" / "claude-cli-nodejs" / "Cache-x" / "mcp-logs-plugin-memorysafe-memorysafe"
        logs.mkdir(parents=True)
        item = [entry for entry in self._by_kind("files", "win32") if entry["label"] == "Host caches"][0]
        self.assertEqual(len(item["paths"]), 3)

    def test_the_0_3_x_windows_leftovers_are_found(self) -> None:
        start_menu = self.home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        (start_menu / "MemorySafe").mkdir(parents=True)
        _write(start_menu / "Startup" / "MemorySafe Dashboard Service.lnk", "shortcut")
        item = [entry for entry in self._by_kind("files", "win32") if entry["label"] == "Start Menu"][0]
        self.assertEqual(len(item["paths"]), 2)

    def test_every_hand_written_registration_is_found(self) -> None:
        project = self.home / "work"
        project.mkdir()
        _write(self.home / ".claude.json", json.dumps({
            "mcpServers": {"memorysafe": {"command": "x"}},
            "projects": {str(project): {"mcpServers": {"memorysafe": {"command": "x"}, "other": {}}}},
        }))
        _write(project / ".mcp.json", json.dumps({"mcpServers": {"memorysafe": {"command": "x"}}}))
        _write(self.home / ".codex" / "config.toml", "[mcp_servers.memorysafe]\ncommand = 'x'\n")
        found = {item["registration"] for item in self._by_kind("registration")}
        self.assertEqual(found, {"claude_user", "claude_projects", "codex", f"file:{project / '.mcp.json'}"})


class ApplyTests(_Computer):
    def test_memories_survive_an_uninstall_without_purge(self) -> None:
        self._runtime()
        result = self._apply()
        self.assertFalse((self.data_root / "runtime").exists())
        self.assertFalse((self.data_root / "runtime-state").exists())
        self.assertTrue((self.data_root / "data" / "memorysafe.sqlite3").is_file())
        self.assertIsNone(result["memories_backup"])

    def test_purge_copies_the_memories_out_before_deleting_them(self) -> None:
        self._runtime()
        result = self._apply(purge=True)
        self.assertFalse((self.data_root / "data").exists())
        backup = Path(result["memories_backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.parent, self.home)
        self.assertEqual(backup.read_text(encoding="utf-8"), "sqlite")

    def test_the_plugins_installers_are_run_and_claude_desktop_is_left_to_the_user(self) -> None:
        self._cli("claude")
        self._claude_code_plugin()
        # This computer's own layout, like the rest of the file. Patching sys.platform to
        # borrow another one also sends shutil.which down its Windows branch, where off
        # Windows there is no _winapi to call -- which is what find_cli hit on Linux CI.
        desktop = agents.claude_desktop_dirs(self.home, self.env, agents.sys.platform)[0]
        (desktop / "Claude Extensions" / "local.mcpb.memorysafe-beta.memorysafe").mkdir(parents=True)
        result = self._apply()
        self.assertEqual([command[1:3] for command in self.calls], [["plugin", "uninstall"]])
        self.assertTrue(any("Settings > Extensions" in step for step in result["manual"]))

    def test_hand_written_registrations_are_removed_and_backed_up(self) -> None:
        project = self.home / "work"
        project.mkdir()
        claude_json = _write(self.home / ".claude.json", json.dumps({
            "mcpServers": {"memorysafe": {"command": "x"}, "other": {"command": "y"}},
            "projects": {str(project): {"mcpServers": {"memorysafe": {"command": "x"}}}},
        }))
        mcp_json = _write(project / ".mcp.json", json.dumps({"mcpServers": {"memorysafe": {}, "keep": {}}}))
        codex = _write(self.home / ".codex" / "config.toml", "[mcp_servers.memorysafe]\ncommand = 'x'\n\n[other]\nk = 1\n")
        self._apply()
        after = json.loads(claude_json.read_text(encoding="utf-8"))
        self.assertNotIn("memorysafe", after["mcpServers"])
        self.assertIn("other", after["mcpServers"])  # the user's own servers stay
        self.assertEqual(after["projects"][str(project)]["mcpServers"], {})
        self.assertEqual(json.loads(mcp_json.read_text(encoding="utf-8"))["mcpServers"], {"keep": {}})
        self.assertNotIn("memorysafe", codex.read_text(encoding="utf-8"))
        self.assertIn("[other]", codex.read_text(encoding="utf-8"))
        self.assertTrue(claude_json.with_name(claude_json.name + ".memorysafe-backup").is_file())

    def test_an_installer_that_fails_is_reported_not_raised(self) -> None:
        self._cli("claude")
        self._claude_code_plugin()
        self._runtime()
        result = self._apply(runner=self._runner(returncode=1))
        self.assertTrue(any("Claude Code" in failure for failure in result["failed"]))
        # and the rest of the uninstall still happened
        self.assertFalse((self.data_root / "runtime").exists())

    def test_an_empty_data_root_is_not_left_behind(self) -> None:
        self._runtime()
        self._apply(purge=True)
        self.assertFalse(self.data_root.exists())


class BusyFileTests(_Computer):
    """Windows says no for a moment, then yes.

    Found by uninstalling for real on 20 September. The first --apply left all 268 MB
    because MemorySafe was running; after every process was stopped, three more runs
    each freed over 100 MB and stopped on a different file, none read-only and none
    held by any process. A retry loop cleared the rest.
    """

    def test_a_lock_that_clears_is_waited_out(self) -> None:
        attempts = []

        def flaky(target):
            attempts.append(target)
            if len(attempts) < 3:
                raise PermissionError(13, "Access is denied", str(target))

        self._runtime()
        with patch.object(uninstall.shutil, "rmtree", side_effect=flaky), patch.object(
            uninstall.time, "sleep"
        ) as slept:
            uninstall.remove_path(self.data_root / "runtime")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(slept.call_count, 2, "it waits between tries rather than spinning")

    def test_a_lock_that_never_clears_is_still_reported(self) -> None:
        self._runtime()
        with patch.object(
            uninstall.shutil, "rmtree", side_effect=PermissionError(13, "Access is denied")
        ), patch.object(uninstall.time, "sleep"):
            result = self._apply()
        self.assertTrue(result["failed"])
        self.assertTrue(result["still_in_use"], "the remedy differs from any other failure")

    def test_a_failure_that_is_not_a_lock_is_not_retried(self) -> None:
        """Retrying what is genuinely not ours only makes the uninstall slower."""
        self._runtime()
        with patch.object(
            uninstall.shutil, "rmtree", side_effect=OSError(21, "Is a directory")
        ), patch.object(uninstall.time, "sleep") as slept:
            result = self._apply()
        # Counting rmtree calls would count the several paths an uninstall removes.
        # Never sleeping is what says no path was tried twice.
        self.assertEqual(slept.call_count, 0)
        self.assertTrue(result["failed"])
        self.assertFalse(result["still_in_use"])

    def test_a_clean_run_reports_nothing_in_use(self) -> None:
        self._runtime()
        self.assertFalse(self._apply()["still_in_use"])

    def test_the_report_says_what_to_do_about_a_lock(self) -> None:
        """WinError 5 is not an instruction. Closing the assistants is."""
        self._runtime()
        with patch.object(
            uninstall.shutil, "rmtree", side_effect=PermissionError(13, "Access is denied")
        ), patch.object(uninstall.time, "sleep"):
            printed = StringIO()
            with redirect_stdout(printed):
                cli_main_uninstall(self)
        text = printed.getvalue()
        self.assertIn("still running", text)
        self.assertIn("Close Claude Desktop", text)


def cli_main_uninstall(case: _Computer) -> None:
    """Print a real --apply result the way the command does."""
    from memorysafe_chatgpt.cli import _print_uninstall

    items = uninstall.inventory(case.home, case.env, agents.sys.platform, case.data_root)
    result = uninstall.apply(
        case.home, case.env, agents.sys.platform, case.data_root, False, case._runner()
    )
    _print_uninstall(items, result, True, False)


class CommandTests(_Computer):
    def _run(self, *arguments: str) -> str:
        output = StringIO()
        argv = ["memorysafe", "--install-root", str(self.data_root), "uninstall", *arguments]
        with patch("sys.argv", argv), patch("pathlib.Path.home", return_value=self.home), \
                patch.dict(agents.os.environ, self.env, clear=True), redirect_stdout(output):
            cli_main()
        return output.getvalue()

    def test_the_dry_run_removes_nothing_and_says_what_it_would(self) -> None:
        self._runtime()
        with patch.object(uninstall.subprocess, "run") as run:
            text = self._run()
        run.assert_not_called()
        self.assertTrue((self.data_root / "runtime").exists())
        self.assertIn("dry run", text)
        self.assertIn("would remove", text)
        self.assertIn("--purge to delete", text)
        self.assertIn("memorysafe uninstall --apply", text)

    def test_the_dry_run_keeps_memories_out_of_the_total(self) -> None:
        self._runtime()
        text = self._run()
        self.assertIn("kept; add --purge to delete", text)

    def test_nothing_installed_says_so(self) -> None:
        self.assertIn("Nothing of MemorySafe's is on this computer", self._run())

    def test_apply_removes_and_reports(self) -> None:
        self._runtime()
        text = self._run("--apply")
        self.assertFalse((self.data_root / "runtime").exists())
        self.assertIn("Private runtime", text)


class DocumentTests(unittest.TestCase):
    def test_both_install_documents_say_how_to_remove_it(self) -> None:
        """An uninstaller nobody is told about is the same as no uninstaller."""
        root = Path(__file__).resolve().parents[1]
        for name in ("plugin/README-template.md", "plugin/INSTALL.md"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("Removing MemorySafe", text, name)
            for shim in (
                "~/.local/share/MemorySafe/bin/memorysafe uninstall",
                "~/Library/Application\\ Support/MemorySafe/bin/memorysafe uninstall",
                "%LOCALAPPDATA%\\MemorySafe\\bin\\memorysafe.cmd uninstall",
            ):
                self.assertIn(shim, text, name)
            self.assertIn("--purge", text, name)


if __name__ == "__main__":
    unittest.main()
