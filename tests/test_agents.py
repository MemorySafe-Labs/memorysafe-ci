"""memorysafe connect: one install, every assistant on the computer, one store.

The store was always shared -- every launcher resolves the same data root -- but the
install was not. A tester who installed the Claude Desktop extension on Windows (19 Sep)
still had to find a second guide for Claude Code and a third for Codex, and nothing told
them which of their assistants were connected. These tests pin down what `connect` may
decide on its own and what it must leave to the user:

- an assistant is connected only through its own installer (`claude plugin ...`,
  `codex plugin ...`), never by writing its configuration by hand -- that is what
  0.3.x's setup_assistants.py did, and `migrate` exists to clean up after it;
- Claude Desktop is reported, never connected from here: it asks the user to confirm
  every extension, and that confirmation is the host's safety boundary;
- nothing is reported connected until the host's own records say so afterwards.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import agents
from memorysafe_chatgpt.cli import main as cli_main
from memorysafe_chatgpt.migrate import PLUGIN_ID


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.bin = Path(self.temporary.name) / "bin"
        self.bin.mkdir()
        # An empty PATH: no test may find a real claude or codex on the machine running it.
        self.env = {"PATH": str(self.bin), "APPDATA": str(self.home / "AppData" / "Roaming"),
                    "LOCALAPPDATA": str(self.home / "AppData" / "Local"), "SYSTEMROOT": r"C:\Windows"}
        # Nor in the fixed fallback folders, which PATH does not cover: a Mac with Homebrew's
        # codex in /opt/homebrew/bin made the empty computer look connectable.
        bin_dirs = patch.object(agents, "_POSIX_BIN_DIRS", ())
        bin_dirs.start()
        self.addCleanup(bin_dirs.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cli(self, name: str) -> str:
        # shutil.which needs a file that looks executable on this platform.
        path = self.bin / (name + (".exe" if agents.sys.platform == "win32" else ""))
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def _inventory(self, platform: str | None = None) -> dict[str, dict]:
        return {entry["id"]: entry for entry in agents.inventory(self.home, self.env, platform or agents.sys.platform)}

    def _install_claude_code_plugin(self, enabled: bool | None = None) -> None:
        _write(self.home / ".claude" / "plugins" / "installed_plugins.json", json.dumps({"plugins": {PLUGIN_ID: [{}]}}))
        if enabled is not None:
            _write(self.home / ".claude" / "settings.json", json.dumps({"enabledPlugins": {PLUGIN_ID: enabled}}))

    def _install_codex_plugin(self) -> None:
        _write(self.home / ".codex" / "config.toml", f'[plugins."{PLUGIN_ID}"]\nenabled = true\n')


class InventoryTests(_Home):
    def test_an_empty_computer_has_nothing_to_connect(self) -> None:
        for entry in self._inventory().values():
            self.assertFalse(entry["present"], entry)
            self.assertFalse(entry["can_connect"], entry)

    def test_claude_code_with_its_command_can_be_connected(self) -> None:
        cli = self._cli("claude")
        (self.home / ".claude").mkdir()
        entry = self._inventory()["claude_code"]
        self.assertTrue(entry["present"])
        self.assertFalse(entry["connected"])
        self.assertTrue(entry["can_connect"])
        self.assertEqual(Path(entry["command"]).resolve(), Path(cli).resolve())

    def test_the_off_path_native_install_is_still_found(self) -> None:
        """The native installer puts claude in ~/.local/bin and leaves PATH alone on Windows.

        On the 19 Sep test machine it printed "not in your PATH" and moved on, so a PATH
        lookup alone reports "cannot connect" on the most common install there is.
        """
        name = "claude.exe" if agents.sys.platform == "win32" else "claude"
        native = _write(self.home / ".local" / "bin" / name, "")
        native.chmod(0o755)
        entry = self._inventory()["claude_code"]
        self.assertTrue(entry["can_connect"])
        self.assertEqual(Path(entry["command"]).resolve(), native.resolve())

    def test_claude_code_without_its_command_gets_a_next_step_not_a_button(self) -> None:
        """~/.claude exists when Claude Code only ever ran inside the Claude desktop app.

        Its plugins load there, but nothing can add one without the claude command, so the
        user has to be told how to get it rather than offered a Connect that cannot work.
        """
        (self.home / ".claude").mkdir()
        entry = self._inventory()["claude_code"]
        self.assertTrue(entry["present"])
        self.assertFalse(entry["can_connect"])
        self.assertIn("claude.ai/install", entry["next_step"])

    def test_claude_code_without_its_command_is_covered_when_the_extension_is_connected(self) -> None:
        """The desktop app's Code tab already has MemorySafe, and no plugin can be added without the command."""
        (self.home / ".claude").mkdir()
        desktop = self.home / "Library" / "Application Support" / "Claude"
        (desktop / "Claude Extensions" / "local.mcpb.memorysafe-beta.memorysafe").mkdir(parents=True)
        _write(desktop / "Claude Extensions Settings" / "local.mcpb.memorysafe-beta.memorysafe.json",
               json.dumps({"isEnabled": True}))
        entry = self._inventory("darwin")["claude_code"]
        self.assertTrue(entry["covered"])
        self.assertFalse(entry["connected"])
        self.assertFalse(entry["can_connect"])
        self.assertIsNone(entry["next_step"])
        self.assertIn("extension", entry["note"])

    def test_claude_code_is_not_covered_without_the_extension(self) -> None:
        (self.home / ".claude").mkdir()
        entry = self._inventory()["claude_code"]
        self.assertFalse(entry["covered"])
        self.assertIn("claude.ai/install", entry["next_step"])

    def test_codex_from_the_desktop_app_gets_a_connect_button(self) -> None:
        """The Codex app has no codex on PATH, but its plugin app server ships the CLI."""
        (self.home / ".codex").mkdir()
        bundled = self.home / ".codex" / "plugins" / ".plugin-appserver" / "codex-cli" / "bin" / "codex"
        _write(bundled, "")
        bundled.chmod(0o755)
        entry = self._inventory("darwin")["codex"]
        self.assertTrue(entry["can_connect"])
        self.assertEqual(Path(entry["command"]).resolve(), bundled.resolve())

    def test_an_installed_plugin_is_connected(self) -> None:
        self._install_claude_code_plugin()
        entry = self._inventory()["claude_code"]
        self.assertEqual((entry["connected"], entry["how"], entry["can_connect"]), (True, "plugin", False))

    def test_a_hand_written_registration_still_counts_as_connected(self) -> None:
        # It is a working registration. Replacing it is migrate's job, not connect's.
        _write(self.home / ".claude.json", json.dumps({"mcpServers": {"memorysafe": {"command": "x"}}}))
        entry = self._inventory()["claude_code"]
        self.assertEqual((entry["connected"], entry["how"]), (True, "manual"))

    def test_codex_plugin_is_read_from_its_config(self) -> None:
        self._cli("codex")
        self._install_codex_plugin()
        entry = self._inventory()["codex"]
        self.assertEqual((entry["present"], entry["connected"], entry["how"]), (True, True, "plugin"))

    @unittest.skipUnless(agents.sys.platform == "win32", "the Store app's layout is Windows-only")
    def test_the_codex_desktop_apps_bundled_cli_is_found(self) -> None:
        """The Codex desktop app (Microsoft Store) puts no codex on PATH.

        On the 19 Sep test machine connect first reported "settings are here but the codex
        command is not" right after Codex was installed. The app ships the full CLI under a
        per-version hash in %LOCALAPPDATA%\\OpenAI\\Codex\\bin; the newest one is used.
        """
        (self.home / ".codex").mkdir()
        bundled = _write(self.home / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "247581e40ee272fb" / "codex.exe", "")
        entry = self._inventory()["codex"]
        self.assertTrue(entry["can_connect"])
        self.assertEqual(Path(entry["command"]).resolve(), bundled.resolve())

    def test_codex_settings_without_the_command_cannot_be_connected(self) -> None:
        (self.home / ".codex").mkdir()
        entry = self._inventory()["codex"]
        self.assertTrue(entry["present"])
        self.assertFalse(entry["can_connect"])
        self.assertTrue(entry["next_step"])


class CodeTabWordingTests(_Home):
    """What the Claude Code row offers, once the Claude Desktop extension is in.

    The desktop app exposes its extensions to its own Code tab, so a session there already
    reaches MemorySafe through the extension and connecting the plugin as well lists every
    tool twice (issue #7). The offer stays -- Claude Code in a terminal cannot see the
    extension -- but "Installed here. Not connected yet." was the wrong thing to say about
    it, and said nothing about the copy they already have.
    """

    def _desktop_extension(self) -> None:
        desktop = agents.claude_desktop_dirs(self.home, self.env, agents.sys.platform)[0]
        (desktop / "Claude Extensions" / "local.mcpb.memorysafe-beta.memorysafe").mkdir(parents=True)

    def test_the_row_says_the_plugin_is_for_a_terminal(self) -> None:
        self._cli("claude")
        self._desktop_extension()
        entry = self._inventory()["claude_code"]
        self.assertTrue(entry["can_connect"], "the offer stays: a terminal needs the plugin")
        self.assertIn("in a terminal", entry["note"])
        self.assertIn("extension", entry["note"])

    def test_without_the_extension_the_row_says_nothing_extra(self) -> None:
        self._cli("claude")
        (self.home / ".claude").mkdir()
        entry = self._inventory()["claude_code"]
        self.assertTrue(entry["can_connect"])
        self.assertIsNone(entry["note"])

    def test_an_already_connected_claude_code_gets_no_note(self) -> None:
        """It is not an offer any more, so there is nothing to explain."""
        self._cli("claude")
        self._desktop_extension()
        self._install_claude_code_plugin()
        entry = self._inventory()["claude_code"]
        self.assertTrue(entry["connected"])
        self.assertIsNone(entry["note"])

    def test_the_panel_shows_the_note_in_place_of_the_plain_line(self) -> None:
        """The field is only worth setting if the dashboard actually renders it."""
        page = (Path(agents.__file__).with_name("setup_app.py")).read_text(encoding="utf-8")
        self.assertIn("String(agent.note??'Installed here. Not connected yet.')", page)


class ClaudeDesktopTests(_Home):
    def _extension(self, desktop: Path, enabled: bool | None = True) -> None:
        (desktop / "Claude Extensions" / "local.mcpb.memorysafe-beta.memorysafe").mkdir(parents=True)
        if enabled is not None:
            _write(desktop / "Claude Extensions Settings" / "local.mcpb.memorysafe-beta.memorysafe.json",
                   json.dumps({"isEnabled": enabled}))

    def test_the_extension_is_connected_on_windows(self) -> None:
        desktop = self.home / "AppData" / "Roaming" / "Claude"
        self._extension(desktop)
        entry = self._inventory("win32")["claude_desktop"]
        self.assertEqual((entry["present"], entry["connected"], entry["how"]), (True, True, "extension"))

    def test_the_store_app_container_is_looked_inside(self) -> None:
        """The Microsoft Store build keeps its settings in the package's LocalCache.

        On the 19 Sep machine the extension lived under
        Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\Claude, which a process
        outside the package does not see at %APPDATA%\\Claude.
        """
        packaged = self.home / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
        self._extension(packaged)
        entry = self._inventory("win32")["claude_desktop"]
        self.assertTrue(entry["connected"])

    def test_a_disabled_extension_is_not_connected(self) -> None:
        desktop = self.home / "AppData" / "Roaming" / "Claude"
        self._extension(desktop, enabled=False)
        entry = self._inventory("win32")["claude_desktop"]
        self.assertTrue(entry["present"])
        self.assertFalse(entry["connected"])

    def test_claude_desktop_is_never_connected_from_here(self) -> None:
        """It asks the user to confirm every extension. That is its safety boundary, not ours to skip."""
        (self.home / "AppData" / "Roaming" / "Claude").mkdir(parents=True)
        entry = self._inventory("win32")["claude_desktop"]
        self.assertTrue(entry["present"])
        self.assertFalse(entry["can_connect"])
        self.assertEqual(agents.plan(entry, self.home), [])
        # The route that worked on the test machine, and the trap that did not.
        self.assertIn("Install Extension", entry["next_step"])
        self.assertIn("Notepad", entry["next_step"])

    def test_macos_location(self) -> None:
        self._extension(self.home / "Library" / "Application Support" / "Claude")
        self.assertTrue(self._inventory("darwin")["claude_desktop"]["connected"])


class PlanTests(_Home):
    def setUp(self) -> None:
        super().setUp()
        self.claude = self._cli("claude")
        (self.home / ".claude").mkdir()

    def _plan(self) -> list[list[str]]:
        return agents.plan(self._inventory()["claude_code"], self.home)

    def test_a_first_connect_adds_the_marketplace_then_installs(self) -> None:
        steps = self._plan()
        self.assertEqual([step[1:] for step in steps], [
            ["plugin", "marketplace", "add", agents.MARKETPLACE_SOURCE],
            ["plugin", "install", PLUGIN_ID],
        ])

    def test_a_known_marketplace_is_not_added_again(self) -> None:
        _write(self.home / ".claude" / "plugins" / "known_marketplaces.json", json.dumps({"memorysafe": {}}))
        self.assertEqual([step[1:] for step in self._plan()], [["plugin", "install", PLUGIN_ID]])

    def test_a_plugin_switched_off_is_enabled_not_reinstalled(self) -> None:
        # `install` on an installed plugin reports it as present and changes nothing.
        self._install_claude_code_plugin(enabled=False)
        self.assertEqual([step[1:] for step in self._plan()], [["plugin", "enable", PLUGIN_ID]])

    def test_codex_goes_through_codex(self) -> None:
        self._cli("codex")
        steps = agents.plan(self._inventory()["codex"], self.home)
        self.assertEqual([step[1:] for step in steps], [
            ["plugin", "marketplace", "add", agents.MARKETPLACE_SOURCE],
            ["plugin", "add", PLUGIN_ID],
        ])

    def test_codex_skips_a_marketplace_it_already_has(self) -> None:
        self._cli("codex")
        _write(self.home / ".codex" / "config.toml", '[marketplaces.memorysafe]\nsource = "x"\n')
        steps = agents.plan(self._inventory()["codex"], self.home)
        self.assertEqual([step[1:] for step in steps], [["plugin", "add", PLUGIN_ID]])

    def test_an_npm_batch_file_runs_through_cmd_with_its_path_quoted(self) -> None:
        """npm installs codex.cmd, and a batch file needs cmd.exe. Usernames have spaces."""
        argv = [r"C:\Users\Ana Maria\AppData\Roaming\npm\codex.cmd", "plugin", "add", PLUGIN_ID]
        command = agents._process_command(argv, "win32", {"SYSTEMROOT": r"C:\Windows"})
        self.assertEqual(
            command,
            r'"C:\Windows\System32\cmd.exe" /d /s /c ""C:\Users\Ana Maria\AppData\Roaming\npm\codex.cmd" plugin add memorysafe@memorysafe"',
        )
        # An .exe is started directly, as a list.
        self.assertEqual(agents._process_command([r"C:\x\claude.exe", "plugin"], "win32", {}), [r"C:\x\claude.exe", "plugin"])


class ConnectTests(_Home):
    def setUp(self) -> None:
        super().setUp()
        self.claude = self._cli("claude")
        (self.home / ".claude").mkdir()
        self.calls: list = []

    def _runner(self, installs: bool, returncode: int = 0):
        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            if installs and "install" in command:
                self._install_claude_code_plugin()
            return subprocess.CompletedProcess(command, returncode, stdout=b"done", stderr=b"")
        return run

    def _connect(self, runner) -> dict:
        return agents.connect("claude_code", self.home, self.env, agents.sys.platform, runner=runner)

    def test_connected_only_once_the_host_itself_says_so(self) -> None:
        result = self._connect(self._runner(installs=True))
        self.assertTrue(result["ok"])
        self.assertIn("Restart Claude Code", result["message"])
        self.assertEqual(len(self.calls), 2)
        # Never interactive: a prompt nobody can see would hang the dashboard's request.
        for _command, kwargs in self.calls:
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], agents.CONNECT_TIMEOUT_SECONDS)

    def test_an_installer_that_exits_zero_but_installs_nothing_is_a_failure(self) -> None:
        result = self._connect(self._runner(installs=False))
        self.assertFalse(result["ok"])
        # The user gets the exact commands to run themselves, by name rather than by path.
        self.assertIn("claude plugin install memorysafe@memorysafe", result["message"])

    def test_a_failing_marketplace_add_does_not_stop_the_install(self) -> None:
        # Adding an already-known marketplace fails on some versions; the install decides.
        def run(command, **kwargs):
            self.calls.append(command)
            if "install" in command:
                self._install_claude_code_plugin()
                return subprocess.CompletedProcess(command, 0, b"", b"")
            return subprocess.CompletedProcess(command, 1, b"", b"already exists")
        self.assertTrue(self._connect(run)["ok"])

    def test_a_missing_or_hung_installer_is_reported_not_raised(self) -> None:
        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 300)
        result = self._connect(run)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["steps"][0]["returncode"])

    def test_an_already_connected_assistant_is_left_alone(self) -> None:
        self._install_claude_code_plugin()
        result = self._connect(self._runner(installs=True))
        self.assertTrue(result["ok"])
        self.assertEqual(self.calls, [])

    def test_the_installer_output_is_kept_from_a_file_not_a_pipe(self) -> None:
        """On Windows, subprocess.run reads a timed-out child's pipes to the end after killing
        it, so node.exe or git.exe still holding them would outlast the 300 s timeout."""

        def run(command, **kwargs):
            self.assertNotIn("capture_output", kwargs)
            self.assertIs(kwargs["stderr"], subprocess.STDOUT)
            kwargs["stdout"].write(b"Added marketplace memorysafe")
            return subprocess.CompletedProcess(command, 0)
        result = self._connect(run)
        self.assertEqual(result["steps"][0]["output"], "Added marketplace memorysafe")

    def test_windows_installers_open_no_console_window(self) -> None:
        # The dashboard service runs detached, without a console, so each console program it
        # starts would otherwise flash a window of its own.
        agents.connect("claude_code", self.home, self.env, "win32", runner=self._runner(installs=True))
        self.assertTrue(self.calls)
        for _command, kwargs in self.calls:
            self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.calls.clear()
        (self.home / ".claude" / "plugins" / "installed_plugins.json").unlink()
        agents.connect("claude_code", self.home, self.env, "linux", runner=self._runner(installs=True))
        self.assertTrue(self.calls)
        for _command, kwargs in self.calls:
            self.assertNotIn("creationflags", kwargs)

    def test_a_cli_found_off_path_runs_with_its_folder_on_path(self) -> None:
        """npm's codex is `#!/usr/bin/env node`, and Homebrew's node sits beside it in a folder a
        launchd job's PATH lacks: without it, exit 127, "env: node: No such file or directory"."""

        # The host's own platform: the paths below are real ones on the machine running this.
        platform = agents.sys.platform
        Path(self.claude).unlink()
        local = self.home / ".local" / "bin"
        local.mkdir(parents=True)
        cli = local / ("claude.exe" if platform == "win32" else "claude")
        cli.write_text("", encoding="utf-8")
        cli.chmod(0o755)
        result = agents.connect("claude_code", self.home, self.env, platform, runner=self._runner(installs=False))
        self.assertTrue(self.calls)
        for _command, kwargs in self.calls:
            self.assertEqual(kwargs["env"]["PATH"].split(os.pathsep)[0], str(local))
        # And the command the user is told to run is one that exists in their terminal.
        self.assertIn(f"{cli} plugin install memorysafe@memorysafe", result["message"])

    def test_the_codex_app_s_bundled_cli_is_named_by_its_full_path(self) -> None:
        # The Store app puts no codex on PATH; "run codex plugin add" would fail to start.
        argv = [r"C:\Users\Ana Maria\AppData\Local\OpenAI\Codex\bin\abc\codex.exe", "plugin", "add", PLUGIN_ID]
        self.assertEqual(
            agents.command_line(argv, self.env, "win32"),
            r'"C:\Users\Ana Maria\AppData\Local\OpenAI\Codex\bin\abc\codex.exe" plugin add memorysafe@memorysafe',
        )

    def test_overlapping_connects_run_one_installer_at_a_time(self) -> None:
        """The start-up thread, both pages' Connect buttons and the ask-once answer can overlap.
        Two installers writing one host's plugin records at once can leave them half-written."""

        import threading
        import time

        running = []
        overlap = []

        def run(command, **kwargs):
            running.append(command)
            overlap.append(len(running))
            time.sleep(0.05)
            if "install" in command:
                self._install_claude_code_plugin()
            running.remove(command)
            return subprocess.CompletedProcess(command, 0)

        results = []
        threads = [threading.Thread(target=lambda: results.append(self._connect(run))) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(max(overlap), 1)
        self.assertEqual(len(overlap), 2)  # one marketplace add, one install
        self.assertEqual(sum("already connected" in result["message"] for result in results), 2)

    def test_claude_desktop_is_refused_with_its_next_step(self) -> None:
        (self.home / "AppData" / "Roaming" / "Claude").mkdir(parents=True)
        result = agents.connect("claude_desktop", self.home, self.env, "win32", runner=self._runner(installs=True))
        self.assertFalse(result["ok"])
        self.assertEqual(self.calls, [])
        self.assertIn("Install Extension", result["message"])


class DecisionTests(_Home):
    """Middle ground, chosen by the tester on 19 Sep: ask once, remember the answer.

    Never installing into another assistant unasked, and never asking twice. A yes also
    covers assistants installed later -- they connect when the dashboard service starts.
    """

    def setUp(self) -> None:
        super().setUp()
        self.state_dir = Path(self.temporary.name) / "state"
        self._cli("claude")
        (self.home / ".claude").mkdir()
        self.calls: list = []

    def _runner(self, command, **kwargs):
        self.calls.append(command)
        if "install" in command:
            self._install_claude_code_plugin()
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def _on_start(self) -> list:
        return agents.auto_connect_on_start(self.state_dir, self.home, self.env, agents.sys.platform, runner=self._runner)

    def test_an_unanswered_question_reads_as_none(self) -> None:
        self.assertIsNone(agents.read_auto_connect(self.state_dir))

    def test_the_answer_survives(self) -> None:
        agents.write_auto_connect(self.state_dir, True)
        self.assertIs(agents.read_auto_connect(self.state_dir), True)
        agents.write_auto_connect(self.state_dir, False)
        self.assertIs(agents.read_auto_connect(self.state_dir), False)

    def test_a_plugin_the_user_switched_off_stays_off(self) -> None:
        """A disabled plugin reads as not connected, and plan() answers that with `plugin
        enable`: after a yes, every start switched off MemorySafe back on."""

        self._install_claude_code_plugin()
        agents.write_auto_connect(self.state_dir, True)
        self.assertEqual(self._on_start(), [])
        self._install_claude_code_plugin(enabled=False)  # the user's /plugin disable
        self.assertEqual(self._on_start(), [])
        self.assertEqual(self.calls, [])
        # Turning the question off and on keeps what was settled.
        agents.write_auto_connect(self.state_dir, False)
        agents.write_auto_connect(self.state_dir, True)
        self.assertEqual(self._on_start(), [])

    def test_a_failed_attempt_is_not_repeated_at_every_start(self) -> None:
        def installs_nothing(command, **kwargs):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        agents.write_auto_connect(self.state_dir, True)
        first = agents.auto_connect_on_start(self.state_dir, self.home, self.env, agents.sys.platform, runner=installs_nothing)
        self.assertFalse(first[0]["ok"])
        self.calls.clear()
        self.assertEqual(
            agents.auto_connect_on_start(self.state_dir, self.home, self.env, agents.sys.platform, runner=installs_nothing), []
        )
        self.assertEqual(self.calls, [])
        # The panel's Connect button still works: an explicit yes or click is not "automatic".
        self.assertFalse(agents.connect_all(self.home, self.env, agents.sys.platform, installs_nothing)[0]["ok"])
        self.assertTrue(self.calls)

    def test_an_explicit_yes_settles_what_it_connects(self) -> None:
        agents.write_auto_connect(self.state_dir, True)
        results = agents.connect_all(self.home, self.env, agents.sys.platform, self._runner, state_dir=self.state_dir)
        self.assertTrue(results[0]["ok"])
        (self.home / ".claude" / "plugins" / "installed_plugins.json").unlink()  # the user removes it
        self.calls.clear()
        self.assertEqual(self._on_start(), [])
        self.assertEqual(self.calls, [])

    def test_nothing_is_installed_at_start_before_a_yes(self) -> None:
        self.assertEqual(self._on_start(), [])
        agents.write_auto_connect(self.state_dir, False)
        self.assertEqual(self._on_start(), [])
        self.assertEqual(self.calls, [])

    def test_after_a_yes_a_newly_installed_assistant_connects_at_start(self) -> None:
        agents.write_auto_connect(self.state_dir, True)
        results = self._on_start()
        self.assertEqual([result["agent"] for result in results], ["claude_code"])
        self.assertTrue(results[0]["ok"])
        # And once it is connected, the next start leaves it alone.
        self.calls.clear()
        self.assertEqual(self._on_start(), [])
        self.assertEqual(self.calls, [])

    def test_the_dashboard_service_starts_it_off_the_request_path(self) -> None:
        from memorysafe_chatgpt import setup_app

        paths = type("Paths", (), {"state_dir": self.state_dir})()
        with patch.object(setup_app.agents_module, "auto_connect_on_start") as auto:
            setup_app._auto_connect_on_start(paths).join(timeout=5)
        auto.assert_called_once_with(self.state_dir)

    def test_a_copy_that_loses_the_port_starts_no_installer(self) -> None:
        """Two hosts starting together each see 8765 free and each spawn the dashboard. The
        loser's bind fails; it must not have started connecting by then."""

        from memorysafe_chatgpt import setup_app

        paths = type("Paths", (), {"state_dir": self.state_dir})()
        with patch.object(setup_app.SetupPaths, "from_environment", return_value=paths), \
                patch.object(setup_app, "ensure_device_identity"), \
                patch.object(setup_app, "_snapshot_on_start"), \
                patch.object(setup_app, "_warm_token_metrics"), \
                patch.object(setup_app, "ThreadingHTTPServer", side_effect=OSError("address in use")), \
                patch.object(setup_app, "_auto_connect_on_start") as auto:
            with self.assertRaises(OSError):
                setup_app.main()
        auto.assert_not_called()

    def test_a_failing_installer_does_not_take_the_dashboard_down(self) -> None:
        from memorysafe_chatgpt import setup_app

        paths = type("Paths", (), {"state_dir": self.state_dir})()
        with patch.object(setup_app.agents_module, "auto_connect_on_start", side_effect=RuntimeError("boom")):
            thread = setup_app._auto_connect_on_start(paths)
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())


class CommandTests(_Home):
    def _run(self, *arguments: str) -> str:
        output = StringIO()
        with patch("sys.argv", ["memorysafe", "connect", *arguments]), \
                patch("pathlib.Path.home", return_value=self.home), \
                patch.dict(agents.os.environ, self.env, clear=True), \
                redirect_stdout(output):
            cli_main()
        return output.getvalue()

    def test_the_dry_run_runs_nothing_and_says_what_it_would_do(self) -> None:
        self._cli("claude")
        (self.home / ".claude").mkdir()
        with patch.object(agents.subprocess, "run") as run:
            text = self._run()
        run.assert_not_called()
        self.assertIn("dry run", text)
        self.assertIn("would run  claude plugin marketplace add", text)
        self.assertIn("memorysafe connect --apply", text)
        self.assertIn("Not on this computer", text)

    def test_json_lists_every_assistant(self) -> None:
        report = json.loads(self._run("--json"))
        self.assertFalse(report["applied"])
        self.assertEqual({entry["id"] for entry in report["agents"]}, {"claude_desktop", "claude_code", "codex"})

    def test_apply_connects_and_asks_for_a_restart(self) -> None:
        self._cli("claude")
        (self.home / ".claude").mkdir()

        def run(command, **kwargs):
            if "install" in command:
                self._install_claude_code_plugin()
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with patch.object(agents.subprocess, "run", side_effect=run):
            text = self._run("--apply")
        self.assertIn("Connected. Restart Claude Code", text)
        self.assertIn("Restart Claude Code to start using MemorySafe there.", text)


class DashboardEndpointTests(unittest.TestCase):
    """The dashboard's Connect button runs an installer, so the endpoint behind it must be
    as hard to reach as /api/forget: from this computer only, with this page's token."""

    def setUp(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        from memorysafe_chatgpt import setup_app

        self.setup_app = setup_app
        # SetupHandler.paths defaults to the real data root. The decision is written to
        # its state dir, so a test that did not redirect it would answer the question on
        # behalf of whoever runs the suite.
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.state_dir = root / "runtime-state"
        temporary_paths = setup_app.SetupPaths(
            install_root=root, state_dir=self.state_dir, database_path=root / "data" / "memorysafe.sqlite3",
            secret_file=root / ".secrets" / "key", tunnel_id_file=root / "tunnel-id",
            health_url_file=root / "tunnel.url", legal_dir=root / "legal", connect_url="https://example.invalid",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), setup_app.SetupHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.connected: list[str] = []
        self.connect_all_calls: list[dict] = []
        fake_inventory = [{"id": "claude_code", "label": "Claude Code", "present": True, "connected": False,
                           "how": None, "can_connect": True, "command": "claude", "next_step": None}]
        def fake_connect(agent: str) -> dict:
            self.connected.append(agent)
            return {"agent": agent, "ok": True, "steps": [], "message": "Connected."}

        self.patches = [
            patch.object(setup_app.SetupHandler, "paths", temporary_paths),
            patch.object(setup_app.agents_module, "inventory", return_value=fake_inventory),
            patch.object(setup_app.agents_module, "connect", side_effect=fake_connect),
            patch.object(setup_app.agents_module, "connect_all",
                         side_effect=lambda **kwargs: self.connect_all_calls.append(kwargs) or [fake_connect("claude_code")]),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None, token: str | None = None, host: str | None = None):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-MemorySafe-Setup-Token"] = token
        if host is not None:
            headers["Host"] = host
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, payload

    def test_the_list_is_served_on_this_computer(self) -> None:
        status, payload = self._request("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(payload["agents"][0]["id"], "claude_code")

    def test_the_list_is_refused_under_another_host_name(self) -> None:
        # A DNS-rebinding page reaches 127.0.0.1 under its own host name.
        status, _ = self._request("GET", "/api/agents", host="attacker.example")
        self.assertEqual(status, 403)

    def test_connect_without_the_page_token_runs_nothing(self) -> None:
        status, _ = self._request("POST", "/api/agents/connect", {"agent": "claude_code"})
        self.assertEqual(status, 403)
        status, _ = self._request("POST", "/api/agents/connect", {"agent": "claude_code"}, token="guessed")
        self.assertEqual(status, 403)
        self.assertEqual(self.connected, [])

    def test_connect_accepts_only_the_assistants_it_knows(self) -> None:
        token = self.setup_app.SetupHandler.setup_token
        status, _ = self._request("POST", "/api/agents/connect", {"agent": "claude_code; rm -rf ~"}, token=token)
        self.assertEqual(status, 400)
        self.assertEqual(self.connected, [])

    def test_the_list_says_the_question_was_never_answered(self) -> None:
        _status, payload = self._request("GET", "/api/agents")
        self.assertIsNone(payload["auto_connect"])

    def test_yes_needs_the_page_token(self) -> None:
        status, _ = self._request("POST", "/api/agents/decision", {"auto_connect": True})
        self.assertEqual(status, 403)
        self.assertEqual(self.connected, [])
        self.assertIsNone(agents.read_auto_connect(self.state_dir))

    def test_only_a_real_yes_or_no_is_an_answer(self) -> None:
        token = self.setup_app.SetupHandler.setup_token
        status, _ = self._request("POST", "/api/agents/decision", {"auto_connect": "yes"}, token=token)
        self.assertEqual(status, 400)
        self.assertIsNone(agents.read_auto_connect(self.state_dir))

    def test_yes_connects_them_now_and_is_remembered(self) -> None:
        token = self.setup_app.SetupHandler.setup_token
        status, payload = self._request("POST", "/api/agents/decision", {"auto_connect": True}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(self.connected, ["claude_code"])
        self.assertEqual(self.connect_all_calls, [{"state_dir": self.state_dir}])
        self.assertTrue(payload["auto_connect"])
        self.assertIs(agents.read_auto_connect(self.state_dir), True)
        _status, listed = self._request("GET", "/api/agents")
        self.assertTrue(listed["auto_connect"])

    def test_no_connects_nothing_and_is_remembered_so_it_is_not_asked_again(self) -> None:
        token = self.setup_app.SetupHandler.setup_token
        status, payload = self._request("POST", "/api/agents/decision", {"auto_connect": False}, token=token)
        self.assertEqual(status, 200)
        self.assertEqual(self.connected, [])
        self.assertIs(agents.read_auto_connect(self.state_dir), False)
        self.assertFalse(payload["auto_connect"])

    def test_connect_with_the_token_connects_and_returns_the_new_list(self) -> None:
        token = self.setup_app.SetupHandler.setup_token
        status, payload = self._request("POST", "/api/agents/connect", {"agent": "claude_code"}, token=token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.connected, ["claude_code"])
        self.assertIn("agents", payload)


class DocumentTests(unittest.TestCase):
    def test_both_install_documents_say_one_install_is_enough(self) -> None:
        """A feature nobody is told about is not an install fix.

        The README and INSTALL.md already drifted apart once (the Windows migrate line), so
        both have to carry the connect section, with the command for all three platforms.
        """
        root = Path(__file__).resolve().parents[1]
        for name in ("plugin/README-template.md", "plugin/INSTALL.md"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("One install, every assistant", text, name)
            self.assertIn("One memory, every assistant", text, name)
            for shim in (
                "~/.local/share/MemorySafe/bin/memorysafe connect",
                "~/Library/Application\\ Support/MemorySafe/bin/memorysafe connect",
                "%LOCALAPPDATA%\\MemorySafe\\bin\\memorysafe.cmd connect",
            ):
                self.assertIn(shim, text, name)

    def test_both_documents_give_the_same_claude_cli_install_as_the_dashboard(self) -> None:
        """A tester's machine had neither the claude command nor Node, and the README said
        only that it was needed. The commands now live in three places -- here, and in the
        next_step the dashboard shows -- so they are pinned to each other rather than left to
        drift when the installer's address changes."""
        root = Path(__file__).resolve().parents[1]
        for name in ("plugin/README-template.md", "plugin/INSTALL.md"):
            text = (root / name).read_text(encoding="utf-8")
            for platform in ("win32", "darwin"):
                self.assertIn(agents._install_claude_cli_hint(platform), text, name)


class DashboardPanelTests(unittest.TestCase):
    def test_the_panel_ships_hidden_and_writes_text_never_html(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        page = dashboard_html(local_api_token="t")
        self.assertIn('id="agents-panel" aria-label="Your assistants" hidden', page)
        # Agent labels and next steps are written with textContent. A next step carries a
        # URL; innerHTML anywhere in the panel would turn data into markup.
        start = page.index("function renderAgents")
        end = page.index('byId("refresh").addEventListener')
        self.assertNotIn("innerHTML", page[start:end])
        # Only the standalone dashboard has the token the endpoint needs; inside a host's
        # iframe the panel stays hidden rather than showing buttons that cannot work.
        self.assertIn("if (!standalone) return;", page[start:end])

    def test_a_yes_with_nothing_left_to_connect_clears_the_progress_line(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        page = dashboard_html(local_api_token="t")
        decide = page[page.index("async function decide"):page.index('byId("agents-yes").addEventListener')]
        after = decide[decide.index("if (results.length)"):]
        self.assertIn("} else {", after)
        self.assertIn("message.hidden = true;", after.split("} else {")[1])

    def test_the_question_ships_hidden(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        page = dashboard_html(local_api_token="t")
        self.assertIn('id="agents-ask" hidden', page)
        self.assertIn('id="agents-auto" hidden', page)


class SetupPageTests(unittest.TestCase):
    """The tester went looking for how to add Codex under the dashboard's "Setup" link.

    On Windows that page said "this Mac" six times, and its steps were the ChatGPT
    tunnel's -- a tunnel ID and an OpenAI runtime key, for a route only the macOS
    installer sets up -- so it read "DESKTOP CONNECTOR OFFLINE" for good. Setup has to
    set up this computer's assistants, and keep ChatGPT to the platform where it exists.
    """

    def _page(self, platform: str) -> str:
        from http.server import ThreadingHTTPServer
        import http.client
        import threading

        from memorysafe_chatgpt import setup_app

        with patch.object(setup_app.sys, "platform", platform):
            server = ThreadingHTTPServer(("127.0.0.1", 0), setup_app.SetupHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                connection.request("GET", "/")
                page = connection.getresponse().read().decode("utf-8")
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
        return page

    def test_setup_connects_the_assistants(self) -> None:
        page = self._page("win32")
        self.assertIn("Connect your assistants", page)
        self.assertIn('id="agent-list"', page)
        self.assertIn("/api/agents/connect", page)

    def test_windows_is_not_called_a_mac_outside_the_chatgpt_steps(self) -> None:
        page = self._page("win32")
        chatgpt = page[page.index('<section id="chatgpt-wrap"'):page.index('<div class="footer">')]
        rest = page.replace(chatgpt, "")
        self.assertNotIn("Mac", rest.split("<script>")[0].replace("BlinkMacSystemFont", ""))
        self.assertIn("This computer", rest)
        self.assertIn("Agreement saved on this computer.", page)

    def test_the_chatgpt_steps_are_shown_only_where_the_tunnel_exists(self) -> None:
        from memorysafe_chatgpt import setup_app

        self.assertIn("byId('chatgpt-wrap').hidden=!false;", self._page("win32"))
        with patch.object(setup_app, "chatgpt_tunnel_installed", return_value=True):
            self.assertIn("byId('chatgpt-wrap').hidden=!true;", self._page("darwin"))

    def test_only_the_macos_installer_s_tunnel_counts(self) -> None:
        # A Mac with only the plugin has no tunnel, and its steps could never complete.
        from memorysafe_chatgpt import setup_app

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.object(setup_app.sys, "platform", "darwin"):
                self.assertFalse(setup_app.chatgpt_tunnel_installed(home))
                _write(home / "Library" / "LaunchAgents" / "ca.memorysafe.beta.tunnel.plist", "")
                self.assertTrue(setup_app.chatgpt_tunnel_installed(home))
            with patch.object(setup_app.sys, "platform", "win32"):
                self.assertFalse(setup_app.chatgpt_tunnel_installed(home))

    def test_the_footer_carries_the_real_version(self) -> None:
        from memorysafe_chatgpt.bootstrap_catalog import VERSION

        page = self._page("win32")
        self.assertIn(f"Private Beta {VERSION}", page)
        self.assertNotIn("Private Beta 0.3 ", page)

    def test_the_automatic_capture_label_follows_the_box(self) -> None:
        # It was fixed text: ticked, and "Automatic capture is off." beside it.
        page = self._page("win32")
        self.assertIn('id="automatic-state"', page)
        self.assertIn("'Automatic capture is on.'", page)
        self.assertIn("consentDirty=true;syncAutomatic()", page)

    def test_the_dashboard_link_is_plain_setup_again(self) -> None:
        from memorysafe_chatgpt.dashboard import dashboard_html

        self.assertIn(">Setup</a>", dashboard_html(local_api_token="t"))


if __name__ == "__main__":
    unittest.main()
