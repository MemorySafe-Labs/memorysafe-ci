from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import agents as agents_module
from memorysafe_chatgpt import doctor as doctor_module
from memorysafe_chatgpt.cli import _print_human, main as cli_main
from memorysafe_chatgpt.doctor import (
    _REMEDIES,
    _runtime_python,
    create_support_bundle,
    detect_layout,
    next_actions,
    redact,
    run_doctor,
)
from memorysafe_chatgpt.storage import MemoryStore


def _check_by_id(report: dict, identifier: str) -> dict | None:
    return next((check for check in report["checks"] if check["id"] == identifier), None)


class StaleDashboardAdviceTests(unittest.TestCase):
    """The warning has to say what to do, not only that something is wrong.

    On the machine where this was found the dashboard served 0.4.6 through three
    releases. The doctor reported it every time, as a sentence with no remedy in it,
    and it read as a note rather than as the reason the page was out of date.
    """

    def test_the_warning_names_the_remedy_and_the_process(self) -> None:
        from memorysafe_chatgpt import doctor as doctor_module

        with patch.object(doctor_module, "_dashboard_report", return_value={"version": "0.4.6", "pid": 25916}):
            check = doctor_module._dashboard_version_check()
        self.assertEqual(check["status"], "warning")
        summary = check["summary"]
        self.assertIn("0.4.6", summary)
        self.assertIn("25916", summary, "name the process, or stopping it is guesswork")
        self.assertIn("Restart the assistant", summary)
    def test_a_dashboard_without_a_pid_is_named_not_stopped(self) -> None:
        """A pre-0.4 dashboard reports no PID. "stop process None" helps nobody."""
        from memorysafe_chatgpt import doctor as doctor_module

        with patch.object(doctor_module, "_dashboard_report", return_value={"version": "0.3.5", "pid": None}):
            summary = doctor_module._dashboard_version_check()["summary"]
        self.assertNotIn("None", summary)
        self.assertIn("Claude Desktop", summary)


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "Application Support" / "MemorySafe"
        for relative in (
            "src/memorysafe_chatgpt/server.py",
            "src/memorysafe_chatgpt/setup_app.py",
            "scripts/run_mcp_service.sh",
            "scripts/run_setup_service.sh",
            "scripts/run_tunnel_service.sh",
            ".venv/bin/python",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test\n")

        secret = self.root / ".secrets" / "tunnel-runtime-key"
        secret.parent.mkdir(parents=True)
        self.runtime_key = "sk-" + "a" * 40
        secret.write_text(self.runtime_key)
        tunnel_id = self.root / "runtime-state" / "tunnel-id"
        tunnel_id.parent.mkdir(parents=True)
        self.tunnel_id = "tunnel_1234567890abcdef"
        tunnel_id.write_text(self.tunnel_id)

        wrapper = Path(self.temporary.name) / "bin" / "memorysafe-mcp-stdio"
        wrapper.parent.mkdir()
        wrapper.write_text("#!/bin/zsh\nexit 0\n")
        config = self.root / "config" / "tunnel-client" / "memorysafe-runtime.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"mcp": {"commands": [{"command": str(wrapper)}]}}))

        self.memory_text = "My unreleased product codename is private-alpha."
        store = MemoryStore(self.root / "data" / "memorysafe.sqlite3")
        store.remember(self.memory_text, "project", 0.8, 0.95)
        log = self.root / "runtime-state" / "logs" / "setup.error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            f"ERROR {self.memory_text} key={self.runtime_key} id={self.tunnel_id}\n"
            "OSError: [Errno 48] Address already in use\n"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_doctor_reports_counts_without_memory_content(self) -> None:
        report = run_doctor(self.root)
        rendered = json.dumps(report)
        database = next(check for check in report["checks"] if check["id"] == "database")
        self.assertEqual(database["status"], "pass")
        self.assertEqual(database["details"]["active_memories"], 1)
        self.assertNotIn(self.memory_text, rendered)
        self.assertNotIn(self.runtime_key, rendered)
        self.assertNotIn(self.tunnel_id, rendered)
        self.assertFalse(report["privacy"]["uploaded_automatically"])

    def test_support_bundle_is_sanitized_and_local(self) -> None:
        bundle = create_support_bundle(self.root)
        self.assertTrue(bundle.is_file())
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"README.txt", "doctor.json", "sanitized-errors.json"},
            )
            rendered = "\n".join(
                archive.read(name).decode("utf-8") for name in archive.namelist()
            )
            errors = json.loads(archive.read("sanitized-errors.json"))
        self.assertNotIn(self.memory_text, rendered)
        self.assertNotIn(self.runtime_key, rendered)
        self.assertNotIn(self.tunnel_id, rendered)
        self.assertIn(
            {"log": "setup.error.log", "signature": "port_in_use", "count": 1},
            errors,
        )

    @unittest.skipIf(os.name == "nt", "asserts a live 0o600 stat; Windows returns 0o666 for every file "
                                       "regardless of what create_support_bundle sets, since NTFS has no "
                                       "POSIX mode bits to narrow -- test_support_bundle_is_sanitized_and_"
                                       "local above still runs on Windows and covers the sanitisation a "
                                       "Windows user's bundle needs just as much")
    def test_support_bundle_is_created_with_owner_only_permissions(self) -> None:
        bundle = create_support_bundle(self.root)
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)

    def test_redaction_removes_direct_identifiers(self) -> None:
        rendered = redact(
            f"{Path.home()} {self.runtime_key} {self.tunnel_id} person@example.com 1234567890123"
        )
        self.assertNotIn(str(Path.home()), rendered)
        self.assertNotIn(self.runtime_key, rendered)
        self.assertNotIn(self.tunnel_id, rendered)
        self.assertNotIn("person@example.com", rendered)
        self.assertNotIn("1234567890123", rendered)

    def test_cli_json_is_agent_readable(self) -> None:
        output = StringIO()
        with patch("sys.argv", ["memorysafe", "--install-root", str(self.root), "doctor", "--json"]), redirect_stdout(output):
            cli_main()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["product"], "MemorySafe Beta")
        self.assertIn(payload["overall_status"], {"healthy", "degraded", "error"})

    def test_dashboard_version_check_flags_an_older_dashboard(self) -> None:
        """A 0.3.x Claude Desktop extension can still hold port 8765, and the install
        instructions now tell people to open that port to confirm the install worked.
        They would see the old dashboard and believe the new one is running.

        It cannot be stopped: /api/status has reported "version" since 95e6f7f but
        "pid" only since 71f3cc7, so a pre-0.4 dashboard reports no PID, and
        _replace_stale_dashboard will not kill a process it cannot prove owns the
        port. Reporting it is the whole remedy until the uninstaller ships.
        """
        with patch.object(
            doctor_module, "_dashboard_report", return_value={"version": "0.3.4", "pid": None}
        ):
            result = doctor_module._dashboard_version_check()
        self.assertEqual(result["id"], "dashboard_version")
        self.assertEqual(result["status"], "warning")
        self.assertIn("0.3.4", result["summary"])
        # pid arrived in 71f3cc7; a pre-0.4 dashboard reports none, so this one
        # must not be offered as something the doctor could stop.
        self.assertFalse(result["details"]["stoppable"])

        # A version mismatch with a real PID is the other branch: a 0.4+
        # dashboard from a stale build still holding the port, which
        # _replace_stale_dashboard *can* act on -- stoppable must say True.
        with patch.object(
            doctor_module, "_dashboard_report", return_value={"version": "0.4.0", "pid": 4242}
        ):
            result = doctor_module._dashboard_version_check()
        self.assertTrue(result["details"]["stoppable"])

    def test_dashboard_version_check_is_absent_when_nothing_answers(self) -> None:
        """A None from _dashboard_report must not become a stray entry in checks.

        _dashboard_report returns None for every unusable answer -- nothing
        listening, a non-JSON body, JSON that is not a dict, another product on
        the port, a missing version. run_doctor appends only a real dict, because
        a None in checks breaks _sanitize and the overall_status rollup.
        """
        with patch.object(doctor_module, "_dashboard_report", return_value=None):
            self.assertIsNone(doctor_module._dashboard_version_check())

    def test_dashboard_report_ignores_something_else_on_the_port(self) -> None:
        """Anything may be listening on 8765. Only a MemorySafe dashboard counts.

        A non-MemorySafe JSON body, a non-dict body and a body with no version
        string all have to read as "no dashboard", not as a stale one -- doctor
        must not report a warning because an unrelated service holds the port.
        """
        bodies = {
            "wrong product": b'{"product": "Something Else", "version": "1.0"}',
            "not a dict": b"[]",
            "no version": b'{"product": "MemorySafe Beta"}',
        }
        for label, body in bodies.items():
            with self.subTest(label):
                with patch.object(
                    doctor_module.urllib.request, "urlopen", return_value=BytesIO(body)
                ):
                    self.assertIsNone(doctor_module._dashboard_report())


class ClaudeInstallTests(unittest.TestCase):
    """A Claude install ships no tunnel and no run_*_service.sh scripts.

    Requiring them reported "error" on a correct install and told a Claude user
    their ChatGPT connection was broken.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "MemorySafe"
        interpreter = self.root / _runtime_python("claude-runtime")
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("test\n")
        store = MemoryStore(self.root / "data" / "memorysafe.sqlite3")
        store.remember("A Claude-side memory.", "project", 0.8, 0.9)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_claude_only_install_is_not_an_error(self) -> None:
        report = run_doctor(self.root)
        self.assertEqual(report["runtimes"], ["claude"])
        self.assertEqual(_check_by_id(report, "installation")["status"], "pass")
        self.assertEqual(_check_by_id(report, "database")["status"], "pass")
        self.assertIn(report["overall_status"], {"healthy", "degraded"})

    def test_claude_install_is_never_told_about_chatgpt(self) -> None:
        report = run_doctor(self.root)
        self.assertIsNone(_check_by_id(report, "configuration"))
        self.assertIsNone(_check_by_id(report, "private_connection"))
        self.assertIsNone(_check_by_id(report, "launch_services"))
        self.assertNotIn("ChatGPT", json.dumps(report))

    def test_extension_folder_says_so_instead_of_listing_missing_files(self) -> None:
        bundle = Path(self.temporary.name) / "Claude Extensions" / "memorysafe"
        bundle.mkdir(parents=True)
        (bundle / "manifest.json").write_text("{}")
        (bundle / "mcpb_server.py").write_text("test\n")
        report = run_doctor(bundle)
        self.assertEqual(len(report["checks"]), 1)
        layout = _check_by_id(report, "install_layout")
        self.assertEqual(layout["status"], "error")
        self.assertIn("extension folder", layout["summary"])
        self.assertNotIn("missing", json.dumps(layout))

    def test_one_directory_can_serve_both_assistants(self) -> None:
        for relative in (
            "src/memorysafe_chatgpt/server.py",
            "src/memorysafe_chatgpt/setup_app.py",
            "scripts/run_mcp_service.sh",
            "scripts/run_setup_service.sh",
            "scripts/run_tunnel_service.sh",
            _runtime_python(".venv"),
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test\n")
        layout = detect_layout(self.root)
        self.assertEqual(set(layout.runtimes), {"chatgpt", "claude"})
        report = run_doctor(self.root)
        installation = _check_by_id(report, "installation")
        self.assertEqual(installation["details"]["missing"], [])
        self.assertEqual(set(installation["details"]["checked_runtimes"]), {"chatgpt", "claude"})
        self.assertIsNotNone(_check_by_id(report, "configuration"))

    def test_windows_runtime_uses_the_windows_interpreter_path(self) -> None:
        with patch.object(doctor_module.os, "name", "nt"):
            self.assertEqual(_runtime_python("claude-runtime"), "claude-runtime/Scripts/python.exe")
        # Patched back to "posix" explicitly rather than left to the ambient os.name: on a
        # real Windows runner os.name genuinely is "nt" outside the `with` above too, so
        # this assertion asserted the POSIX path unconditionally and failed there even
        # though _runtime_python is correct on both branches.
        with patch.object(doctor_module.os, "name", "posix"):
            self.assertEqual(_runtime_python("claude-runtime"), "claude-runtime/bin/python")

    def test_missing_database_is_a_first_run_not_a_failure(self) -> None:
        (self.root / "data" / "memorysafe.sqlite3").unlink()
        report = run_doctor(self.root)
        database = _check_by_id(report, "database")
        self.assertEqual(database["status"], "warning")
        self.assertNotEqual(report["overall_status"], "error")


class WindowsPathResolutionTests(unittest.TestCase):
    """DoctorPaths.from_install_root hardcoded ~/Library/Application Support with no
    platform branch at all, so a Windows run with no explicit install_root and no
    MEMORYSAFE_INSTALL_ROOT override resolved under a macOS-only path.
    """

    def test_default_install_root_is_localappdata_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "Local"
            local.mkdir()
            with patch.object(doctor_module.sys, "platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": str(local)}, clear=True
            ):
                paths = doctor_module.DoctorPaths.from_install_root()
            self.assertEqual(paths.install_root, (local / "MemorySafe").resolve())
            self.assertEqual(
                paths.database_path, (local / "MemorySafe" / "data" / "memorysafe.sqlite3").resolve()
            )

    def test_localappdata_present_never_evaluates_home_fallback(self) -> None:
        """os.environ.get("LOCALAPPDATA", Path.home() / ...) evaluates Path.home()
        unconditionally, since Python evaluates a call's arguments before the call
        runs -- even when LOCALAPPDATA is set and the fallback is discarded. On
        Windows, Path.home() can raise RuntimeError when neither USERPROFILE nor
        HOMEDRIVE/HOMEPATH is set, which crashed doctor path resolution even though
        LOCALAPPDATA was present and the fallback was never going to be used.
        """
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "Local"
            local.mkdir()
            with patch.object(doctor_module.sys, "platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": str(local)}, clear=True
            ), patch.object(
                doctor_module.Path, "home", side_effect=RuntimeError("no home directory")
            ):
                paths = doctor_module.DoctorPaths.from_install_root()
            self.assertEqual(paths.install_root, (local / "MemorySafe").resolve())

    def test_an_explicit_override_never_evaluates_the_platform_default(self) -> None:
        """One level of indirection removed from the bug above: from_install_root used
        to compute platform_default -- including its win32 Path.home() fallback -- as a
        plain statement ABOVE the check for install_root/MEMORYSAFE_INSTALL_ROOT, so it
        ran unconditionally even for a caller who set the override and whose value was
        always going to win. A user who set MEMORYSAFE_INSTALL_ROOT still crashed if
        Path.home() would have raised on this branch, purely from computing a default
        that was never used. This never depended on LOCALAPPDATA being set or unset;
        the override alone must be enough to skip the platform branch entirely.
        """
        with patch.object(doctor_module.sys, "platform", "win32"), patch.dict(
            os.environ, {"MEMORYSAFE_INSTALL_ROOT": "/tmp/memorysafe-doctor-test"}, clear=True
        ), patch.object(
            doctor_module.Path, "home", side_effect=RuntimeError("no home directory")
        ):
            paths = doctor_module.DoctorPaths.from_install_root()
        self.assertEqual(paths.install_root, Path("/tmp/memorysafe-doctor-test").resolve())

    def test_msix_redirected_paths_display_as_localappdata(self) -> None:
        """Claude Desktop ships as an MSIX package, so Windows redirects LOCALAPPDATA
        into AppData\\Local\\Packages\\Claude_<publisher-hash>\\LocalCache\\... .
        Doctor printed that whole path at a user who had never seen it and could not
        retype it. The resolved path stays under real_path for --json.
        """
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "Packages" / "Claude_abc123" / "LocalCache" / "Local"
            target = local / "MemorySafe" / "data" / "memorysafe.sqlite3"
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")
            with patch.object(doctor_module.sys, "platform", "win32"), patch.dict(
                os.environ, {"LOCALAPPDATA": str(local)}, clear=False
            ):
                shown = doctor_module._display_path(target)
        self.assertTrue(shown.startswith("%LOCALAPPDATA%"), shown)
        self.assertNotIn("Packages", shown)
        self.assertIn("MemorySafe", shown)


class LaunchServiceReportingTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "_launch_services_check() returns None as soon as os.name == 'nt' "
                                       "(see the early-return guarded by test_returns_none_on_windows_"
                                       "instead_of_crashing_on_getuid below), so on real Windows `check` "
                                       "here is None and can never reach launchctl's macOS-only reporting "
                                       "branches this test exercises")
    def test_running_but_unregistered_is_reported_as_an_autostart_gap(self) -> None:
        unregistered = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="Could not find service")
        with patch.object(doctor_module.subprocess, "run", return_value=unregistered), patch.object(
            doctor_module, "_process_running", return_value=True
        ):
            check = doctor_module._launch_services_check()
        self.assertEqual(check["status"], "warning")
        self.assertIn("not registered to start at login", check["summary"])
        self.assertNotIn("unavailable", check["summary"])

    @unittest.skipIf(os.name == "nt", "_launch_services_check() returns None as soon as os.name == 'nt' "
                                       "(see the early-return guarded by test_returns_none_on_windows_"
                                       "instead_of_crashing_on_getuid below), so on real Windows `check` "
                                       "here is None and can never reach launchctl's macOS-only reporting "
                                       "branches this test exercises")
    def test_nothing_running_is_reported_as_not_running(self) -> None:
        unregistered = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with patch.object(doctor_module.subprocess, "run", return_value=unregistered), patch.object(
            doctor_module, "_process_running", return_value=False
        ):
            check = doctor_module._launch_services_check()
        self.assertEqual(check["status"], "warning")
        self.assertIn("not running", check["summary"])

    def test_returns_none_on_windows_instead_of_crashing_on_getuid(self) -> None:
        """os.getuid() does not exist on Windows, and AttributeError was never in the
        caught tuple -- so a caller that reached this without the run_doctor darwin
        gate crashed instead of getting a report back.
        """
        with patch.object(doctor_module.os, "name", "nt"), patch.object(
            doctor_module.subprocess, "run"
        ) as run:
            self.assertIsNone(doctor_module._launch_services_check())
        run.assert_not_called()


class ReportSanitizationTests(unittest.TestCase):
    def test_an_install_root_outside_home_is_redacted_in_details(self) -> None:
        with tempfile.TemporaryDirectory(prefix="someone-") as temporary:
            root = Path(temporary) / "MemorySafe"
            interpreter = root / _runtime_python("claude-runtime")
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("test\n")
            (root / "data").mkdir(parents=True)
            report = run_doctor(root)
            rendered = json.dumps(report)
        self.assertNotIn("person@example.com", rendered)
        self.assertNotIn(str(Path.home()), rendered)


if __name__ == "__main__":
    unittest.main()


class NextActionsTests(unittest.TestCase):
    """The doctor has to say what to do, not just what is wrong.

    The diagnosis engine existed from 0.3.x but was reachable only as a shell
    command, and Claude Desktop has no shell -- so the testers most likely to
    need it were the least able to run it.
    """

    def test_a_healthy_report_asks_for_nothing(self) -> None:
        self.assertEqual(next_actions({"checks": [{"id": "database", "status": "pass"}]}), [])

    def test_restart_is_offered_before_anyone_inspects_a_file(self) -> None:
        actions = next_actions(
            {"checks": [{"id": "configuration", "status": "error", "summary": "x"}]}
        )
        self.assertEqual(actions[0]["check"], "restart")
        self.assertIn("reopen", actions[0]["action"].lower())

    def test_errors_are_ordered_ahead_of_warnings(self) -> None:
        actions = next_actions(
            {
                "checks": [
                    {"id": "private_connection", "status": "warning", "summary": "w"},
                    {"id": "installation", "status": "error", "summary": "e"},
                ]
            }
        )
        ordered = [a["check"] for a in actions]
        self.assertLess(ordered.index("installation"), ordered.index("private_connection"))

    def test_an_empty_new_store_is_not_reported_as_a_fault(self) -> None:
        actions = next_actions(
            {"checks": [{"id": "database", "status": "warning", "summary": "none yet"}]}
        )
        self.assertIn("normal", actions[0]["action"])

    def test_every_remedy_avoids_terminal_instructions(self) -> None:
        for check_id, by_status in _REMEDIES.items():
            for status, text in by_status.items():
                for banned in ("terminal", "python3", "command line", "shell"):
                    self.assertNotIn(
                        banned,
                        text.lower(),
                        f"{check_id}/{status} sends a non-technical user to a terminal",
                    )

    def test_run_doctor_includes_next_actions(self) -> None:
        self.assertIn("next_actions", run_doctor())


class UnconfiguredChatGPTTests(unittest.TestCase):
    """A feature the user never set up is not a fault in the one they did."""

    def test_untouched_chatgpt_setup_is_informational(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config" / "tunnel-client").mkdir(parents=True)
            check = doctor_module._configuration_check(
                doctor_module.DoctorPaths.from_install_root(root)
            )
        self.assertEqual(check["status"], "info")
        self.assertTrue(check["details"]["not_configured"])
        self.assertEqual(check["details"]["problems"], [])

    def test_informational_checks_do_not_make_a_report_unhealthy(self) -> None:
        report = {
            "checks": [
                {"id": "database", "status": "pass", "summary": ""},
                {"id": "configuration", "status": "info", "summary": ""},
            ]
        }
        self.assertEqual(next_actions(report), [])


class PluginLayoutTests(unittest.TestCase):
    """A plugin install keeps only runtime/<key>/ in the data root, and may sit beside an
    older manual registration that lists every tool a second time."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "MemorySafe"
        runtime = self.root / "runtime" / "abc123"
        runtime.mkdir(parents=True)
        (runtime / "ready").write_text("ready\n")
        (self.root / "data").mkdir()
        self.home = base / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _report(self) -> dict:
        with patch("pathlib.Path.home", return_value=self.home):
            return run_doctor(self.root)

    def test_a_plugin_runtime_is_recognised_as_an_install(self) -> None:
        report = self._report()
        self.assertEqual(report["runtimes"], ["plugin"])
        self.assertEqual(_check_by_id(report, "install_layout")["status"], "pass")

    def test_a_manual_registration_beside_the_plugin_is_reported(self) -> None:
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"memorysafe": {"command": "x"}}}))
        plugins = self.home / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {"memorysafe@memorysafe": [{"scope": "user"}]}})
        )
        report = self._report()
        registration = _check_by_id(report, "registration")
        self.assertEqual(registration["status"], "warning")
        self.assertEqual(registration["details"]["registered_twice"], ["Claude Code"])
        self.assertIn("registration", [action["check"] for action in report["next_actions"]])

    def test_a_manual_entry_in_a_host_without_the_plugin_is_not_a_second_registration(self) -> None:
        """The plugin runtime is shared by every host, so it proves nothing about Codex.
        Without the Codex plugin, Codex's manual entry is its only registration, and the
        fix this warning names would have removed it."""
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "config.toml").write_text('[mcp_servers.memorysafe]\ncommand = "x"\n')
        registration = _check_by_id(self._report(), "registration")
        self.assertEqual(registration["status"], "pass")
        self.assertNotIn("registered_twice", registration["details"])

    def test_no_warning_without_a_manual_registration(self) -> None:
        self.assertEqual(_check_by_id(self._report(), "registration")["status"], "pass")

    def test_capture_hook_check_reports_info_on_windows(self) -> None:
        """hooks.json hardcodes /bin/sh, which Windows does not have, so the
        prompt-time capture nudge never fires there and nothing said so.

        The status is "info", not "warning": run_doctor's own comment records that
        "info" is deliberately not a fault, and commit a09de8b established that a
        hook which never runs is harmless -- it cannot block a prompt. A batch
        polyglot fix was tried and reverted because Windows CI could not attribute
        its failures; do not resurrect it from this check. This fixture ships hooks/
        (the marketplace layout Windows actually installs from), so the Windows
        branch is reached rather than the missing-hooks-dir guard below.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plugin_root = Path(tmp)
            (plugin_root / "hooks").mkdir()
            (plugin_root / "hooks" / "hooks.json").write_text("{}")
            with patch.object(doctor_module.sys, "platform", "win32"):
                result = doctor_module._capture_hook_check(plugin_root)
        self.assertEqual(result["id"], "capture_hook")
        self.assertEqual(result["status"], "info")
        self.assertIn("Windows", result["summary"])

    def test_capture_hook_check_passes_when_the_hook_is_installed(self) -> None:
        """On POSIX, with hooks.json and the hook script both present, the hint works."""
        with tempfile.TemporaryDirectory() as tmp:
            plugin_root = Path(tmp)
            (plugin_root / "hooks").mkdir()
            (plugin_root / "hooks" / "hooks.json").write_text("{}")
            (plugin_root / "scripts").mkdir()
            (plugin_root / "scripts" / "capture_hook").write_text("#!/bin/sh\n")
            with patch.object(doctor_module.sys, "platform", "linux"):
                result = doctor_module._capture_hook_check(plugin_root)
        self.assertEqual(result["id"], "capture_hook")
        self.assertEqual(result["status"], "pass")

    def test_capture_hook_check_warns_when_hooks_dir_is_missing_its_manifest(self) -> None:
        """A marketplace install ships hooks/, so a hooks/ directory with no hooks.json
        inside it is a genuinely broken install, not the Desktop false positive this
        check otherwise guards against -- and "warning" is the right word for it."""
        with tempfile.TemporaryDirectory() as tmp:
            plugin_root = Path(tmp)
            (plugin_root / "hooks").mkdir()
            with patch.object(doctor_module.sys, "platform", "linux"):
                result = doctor_module._capture_hook_check(plugin_root)
        self.assertEqual(result["id"], "capture_hook")
        self.assertEqual(result["status"], "warning")

    def test_capture_hook_check_is_silent_when_the_bundle_never_shipped_hooks(self) -> None:
        """The Claude Desktop extension and the Claude Code .zip never ship hooks/ --
        only the marketplace tree does -- but detect_layout's has_plugin is true for
        both, because they launch through the same scripts/start and write the same
        runtime/<key>/ready marker. Before this guard existed, every real Desktop
        install got a false "warning" telling the user to reinstall to restore a hint
        their product never had, which downgraded overall_status to "degraded" for
        no reason. Returning None here, and run_doctor skipping a None, is what keeps
        that check quiet on a bundle with no hooks/ at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            plugin_root = Path(tmp)
            with patch.object(doctor_module.sys, "platform", "linux"):
                result = doctor_module._capture_hook_check(plugin_root)
        self.assertIsNone(result)


class PrintHumanTests(unittest.TestCase):
    """`_print_human` renders `memorysafe doctor`'s plain-text report -- the one
    plugin/INSTALL.md tells users to run under "If something looks wrong". Before
    this class, `_print_human` had zero test coverage anywhere in the suite; only
    the --json path was exercised. That gap is why a Windows plugin install could
    ship a capture_hook check with status "info" and nobody noticed that
    `{"pass": ..., "warning": ..., "error": ...}[check["status"]]` -- a total
    lookup with no "info" key -- raised KeyError('info') and crashed the report
    outright on the one command the install guide points people at.
    """

    def test_all_four_statuses_render_without_raising(self) -> None:
        """pass, info, warning and error must all render as a bullet line, not
        crash. "info" is the status the capture_hook check actually returns on
        every Windows plugin install; this pins that it renders instead of
        raising KeyError('info') as it did before the .get(..., "·") fix.
        """
        report = {
            "overall_status": "degraded",
            "checks": [
                {"status": "pass", "summary": "Everything installed correctly."},
                {"status": "info", "summary": "The prompt-time capture hint does not run on Windows."},
                {"status": "warning", "summary": "Nothing has been stored yet."},
                {"status": "error", "summary": "The memory database could not be read."},
            ],
        }
        buffer = StringIO()
        with redirect_stdout(buffer):
            _print_human(report)
        output = buffer.getvalue()
        for check in report["checks"]:
            self.assertIn(check["summary"], output)

    def test_unknown_status_still_renders_without_raising(self) -> None:
        """A status this dict has never heard of -- not just "info" -- must degrade
        to a bullet instead of crashing, so the next new check status can't repeat
        the incident that crashed `memorysafe doctor` when "info" first shipped.
        """
        report = {
            "overall_status": "healthy",
            "checks": [{"status": "future_status", "summary": "Something doctor.py doesn't know about yet."}],
        }
        buffer = StringIO()
        with redirect_stdout(buffer):
            _print_human(report)
        self.assertIn("Something doctor.py doesn't know about yet.", buffer.getvalue())


class AssistantsCheckTests(unittest.TestCase):
    """From the 19 Sep Windows tests. An assistant installed but not connected showed only on
    the dashboard (#4). And a Claude Code session in the Claude desktop app's Code tab got
    MemorySafe twice, from the desktop extension and the Claude Code plugin, 22 tools
    instead of 11, while `registration` passed (#7)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        empty_bin = Path(self.temporary.name) / "bin"
        empty_bin.mkdir()
        # No test may find a real claude or codex on the machine running it, on PATH or in
        # the fixed folders agents.py also looks in.
        self.env = {"PATH": str(empty_bin)}
        bin_dirs = patch.object(agents_module, "_POSIX_BIN_DIRS", ())
        bin_dirs.start()
        self.addCleanup(bin_dirs.stop)
        # Nor the ChatGPT app's bundle: a Mac with it installed carries a real codex there.
        app_dirs = patch.object(agents_module, "_MAC_APP_DIRS", ())
        app_dirs.start()
        self.addCleanup(app_dirs.stop)

    def _check(self) -> dict:
        return doctor_module._assistants_check(self.home, self.env, "linux")

    def _plugin(self) -> None:
        plugins = self.home / ".claude" / "plugins"
        plugins.mkdir(parents=True)
        (plugins / "installed_plugins.json").write_text(
            json.dumps({"version": 2, "plugins": {"memorysafe@memorysafe": [{"scope": "user"}]}})
        )

    def _extension(self, platform: str = "linux") -> None:
        # Where Claude Desktop keeps extensions differs per platform, and one test runs
        # against the host's own platform, so the folder comes from agents.py rather than
        # from a POSIX path spelled out here.
        desktop = agents_module.claude_desktop_dirs(self.home, self.env, platform)[0]
        (desktop / "Claude Extensions" / "local.mcpb.memorysafe-beta.memorysafe").mkdir(parents=True)

    def test_the_extension_and_the_plugin_together_are_reported(self) -> None:
        self._plugin()
        self._extension()
        check = self._check()
        self.assertEqual(check["status"], "info")
        self.assertTrue(check["details"]["loaded_twice_in_desktop_code_tab"])
        self.assertIn("listed twice", check["summary"])
        self.assertIn("remove its copy", check["summary"])
        # Removing that copy is only right for someone who never uses Claude Code in a terminal.
        self.assertIn("terminal", check["summary"])

    def test_an_assistant_installed_but_not_connected_is_named(self) -> None:
        self._plugin()
        (self.home / ".codex").mkdir()
        check = self._check()
        self.assertEqual(check["status"], "info")
        self.assertEqual(check["details"]["not_connected"], ["Codex"])
        self.assertFalse(check["details"]["loaded_twice_in_desktop_code_tab"])
        self.assertIn("Codex is on this computer but not connected", check["summary"])

    def test_several_unconnected_assistants_read_as_a_list(self) -> None:
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        (self.home / ".config" / "Claude").mkdir(parents=True)
        check = self._check()
        self.assertEqual(check["details"]["not_connected"], ["Claude Desktop", "Claude Code", "Codex"])
        self.assertIn("Claude Desktop, Claude Code and Codex are on this computer", check["summary"])

    def test_a_claude_code_that_only_runs_in_the_desktop_app_is_not_pushed_to_install_it(self) -> None:
        """~/.claude exists whenever Claude Code has only ever run inside the desktop app,
        where the extension already serves it. Reporting it as not connected sent that user
        to install the CLI and the plugin, which is what makes the Code tab load it twice:
        the check would have walked them into the very thing it reports."""
        self._extension()
        (self.home / ".claude").mkdir()
        check = self._check()
        self.assertEqual(check["status"], "pass")
        self.assertNotIn("Claude Code", check["summary"])

    def test_a_hand_written_claude_code_entry_doubles_up_with_the_extension_too(self) -> None:
        """`registration` reports a manual entry only when a plugin sits beside it, so this
        pair -- a 0.3.x entry plus the extension -- was reported by nothing."""
        self._extension()
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"memorysafe": {"command": "x"}}}))
        check = self._check()
        self.assertTrue(check["details"]["loaded_twice_in_desktop_code_tab"])
        self.assertIn("memorysafe migrate --apply", check["summary"])

    def test_the_plugin_is_removed_by_the_command_the_user_can_actually_type(self) -> None:
        """The people who hit this are on Windows, where the native installer leaves claude
        off PATH -- so `claude plugin uninstall` is "not recognized" for them."""
        self._plugin()
        self._extension()
        cli = self.home / ".local" / "bin" / "claude"
        cli.parent.mkdir(parents=True)
        cli.write_text("")
        cli.chmod(0o755)
        summary = self._check()["summary"]
        # Quoted or bare depending on the path, but never the bare name, which is what
        # "not recognized" comes from.
        self.assertIn(str(cli), summary)
        self.assertIn("plugin uninstall memorysafe@memorysafe", summary)

    def test_a_plugin_installed_from_inside_a_session_is_removed_from_inside_one(self) -> None:
        self._plugin()
        self._extension()
        self.assertIn("/plugin in a Claude Code session", self._check()["summary"])

    def test_an_unreadable_assistant_folder_does_not_take_the_doctor_down(self) -> None:
        """The doctor is what INSTALL.md tells people to run when something looks wrong.
        Every other check answers even when it cannot read what it wanted."""
        with patch.object(agents_module, "inventory", side_effect=PermissionError("Claude Extensions")):
            check = self._check()
        self.assertEqual(check["status"], "info")
        self.assertIn("could not be read", check["summary"])

    def test_the_check_runs_for_a_plugin_install(self) -> None:
        """Wired into run_doctor, with this class's isolation rather than the real machine's
        PATH and Claude folders."""
        root = Path(self.temporary.name) / "MemorySafe"
        (root / "runtime" / "abc123").mkdir(parents=True)
        (root / "runtime" / "abc123" / "ready").write_text("ready\n")
        (root / "data").mkdir()
        self._plugin()
        # run_doctor asks agents.py about the platform it is actually running on.
        self._extension(sys.platform)
        with patch("pathlib.Path.home", return_value=self.home), patch.dict(os.environ, self.env, clear=True):
            report = run_doctor(root)
        check = _check_by_id(report, "assistants")
        self.assertEqual(check["status"], "info")
        self.assertTrue(check["details"]["loaded_twice_in_desktop_code_tab"])

    def test_every_assistant_connected_once_passes(self) -> None:
        self._plugin()
        self.assertEqual(self._check()["status"], "pass")

    def test_neither_finding_is_a_fault(self) -> None:
        """Keeping both copies is right for someone who uses Claude Code in a terminal and in
        the desktop app, so the doctor must not call the install degraded or push a fix."""
        self._plugin()
        self._extension()
        report = {"checks": [self._check()]}
        self.assertEqual(next_actions(report), [])

