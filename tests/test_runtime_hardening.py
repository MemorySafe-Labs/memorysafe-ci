from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import install_macos
from scripts.install_macos import (
    create_cli_launcher,
    create_launcher_app,
    launch_agent_payload,
    migrate_database,
)
from scripts.rotate_runtime_log import rotate_log
from scripts.write_runtime_config import write_config


class RuntimeHardeningTests(unittest.TestCase):
    def test_runtime_config_uses_generated_no_space_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "Application Support" / "MemorySafe"
            (root / ".secrets").mkdir(parents=True)
            (root / "runtime-state").mkdir(parents=True)
            (root / "scripts").mkdir(parents=True)
            (root / ".secrets" / "tunnel-runtime-key").write_text("sk-" + "a" * 40)
            (root / "runtime-state" / "tunnel-id").write_text("tunnel_1234567890abcdef\n")
            (root / "scripts" / "run_mcp_service.sh").write_text("#!/bin/zsh\nexit 0\n")
            wrapper = base / "bin" / "memorysafe-mcp-stdio"

            with patch.dict(os.environ, {"MEMORYSAFE_MCP_WRAPPER_PATH": str(wrapper)}):
                config_path = write_config(root)

            payload = json.loads(config_path.read_text())
            command = payload["mcp"]["commands"][0]
            wrapper = wrapper.resolve()
            self.assertEqual(command, {"channel": "main", "command": str(wrapper)})
            self.assertNotIn(" ", command["command"])

    @unittest.skipIf(os.name == "nt", "asserts a forward-slash POSIX path inside the "
                                       "generated #!/bin/zsh wrapper, which is macOS-only "
                                       "ChatGPT-connector tooling; Windows "
                                       "renders the same target Path with backslashes via "
                                       "str(Path), which test_runtime_config_uses_generated_no_space_wrapper "
                                       "above still covers for the command shape and no-space "
                                       "guarantee that Windows does need")
    def test_runtime_config_wrapper_script_references_the_posix_launch_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "Application Support" / "MemorySafe"
            (root / ".secrets").mkdir(parents=True)
            (root / "runtime-state").mkdir(parents=True)
            (root / "scripts").mkdir(parents=True)
            (root / ".secrets" / "tunnel-runtime-key").write_text("sk-" + "a" * 40)
            (root / "runtime-state" / "tunnel-id").write_text("tunnel_1234567890abcdef\n")
            (root / "scripts" / "run_mcp_service.sh").write_text("#!/bin/zsh\nexit 0\n")
            wrapper = base / "bin" / "memorysafe-mcp-stdio"

            with patch.dict(os.environ, {"MEMORYSAFE_MCP_WRAPPER_PATH": str(wrapper)}):
                write_config(root)

            wrapper = wrapper.resolve()
            self.assertIn("Application Support/MemorySafe/scripts/run_mcp_service.sh", wrapper.read_text())

    @unittest.skipIf(os.name == "nt", "asserts a live 0o700 stat on a wrapper written by "
                                       "scripts/install_macos.py, which is macOS-only ChatGPT-connector "
                                       "tooling; Windows also returns 0o666 for every "
                                       "file regardless of what write_config sets, since NTFS has no "
                                       "POSIX mode bits to narrow -- "
                                       "test_runtime_config_uses_generated_no_space_wrapper above still "
                                       "runs on Windows and covers the wrapper's command shape and content")
    def test_runtime_config_wrapper_is_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "Application Support" / "MemorySafe"
            (root / ".secrets").mkdir(parents=True)
            (root / "runtime-state").mkdir(parents=True)
            (root / "scripts").mkdir(parents=True)
            (root / ".secrets" / "tunnel-runtime-key").write_text("sk-" + "a" * 40)
            (root / "runtime-state" / "tunnel-id").write_text("tunnel_1234567890abcdef\n")
            (root / "scripts" / "run_mcp_service.sh").write_text("#!/bin/zsh\nexit 0\n")
            wrapper = base / "bin" / "memorysafe-mcp-stdio"

            with patch.dict(os.environ, {"MEMORYSAFE_MCP_WRAPPER_PATH": str(wrapper)}):
                write_config(root)

            self.assertEqual(stat.S_IMODE(wrapper.resolve().stat().st_mode), 0o700)

    def test_runtime_config_rejects_wrapper_path_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "MemorySafe"
            (root / ".secrets").mkdir(parents=True)
            (root / "runtime-state").mkdir(parents=True)
            (root / "scripts").mkdir(parents=True)
            (root / ".secrets" / "tunnel-runtime-key").write_text("sk-" + "a" * 40)
            (root / "runtime-state" / "tunnel-id").write_text("tunnel_1234567890abcdef\n")
            (root / "scripts" / "run_mcp_service.sh").write_text("#!/bin/zsh\nexit 0\n")

            with patch.dict(
                os.environ,
                {"MEMORYSAFE_MCP_WRAPPER_PATH": str(base / "wrapper with spaces")},
            ):
                with self.assertRaisesRegex(ValueError, "cannot contain whitespace"):
                    write_config(root)

    def test_migrate_database_closes_both_connections(self) -> None:
        """`with sqlite3.connect(...) as x:` only commits or rolls back a
        transaction -- Connection.__exit__ never closes it. Windows opens
        sqlite files without FILE_SHARE_DELETE, so a leaked handle here would
        block deleting, renaming or replacing the migrated database shortly
        after. Not observable through a delete on POSIX (unlinking an open
        file there is legal), so assert both connections are actually closed
        instead, the way commit 0e0d903 tests the same fix in storage.py.
        """
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "legacy.sqlite3"
            target = base / "installed" / "memorysafe.sqlite3"
            sqlite3.connect(source).close()

            opened: list[sqlite3.Connection] = []
            real_connect = sqlite3.connect

            def _tracking_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                opened.append(connection)
                return connection

            with patch.object(install_macos.sqlite3, "connect", side_effect=_tracking_connect):
                migrated = migrate_database(source, target)

            self.assertTrue(migrated)
            self.assertEqual(len(opened), 2)
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_log_rotation_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "tunnel.log"
            log.write_bytes(b"a" * 20)
            self.assertTrue(rotate_log(log, max_bytes=10, backups=2))
            self.assertEqual(log.read_bytes(), b"")
            self.assertEqual((log.parent / "tunnel.log.1").read_bytes(), b"a" * 10)

            log.write_bytes(b"b" * 20)
            self.assertTrue(rotate_log(log, max_bytes=10, backups=2))
            self.assertEqual((log.parent / "tunnel.log.1").read_bytes(), b"b" * 10)
            self.assertEqual((log.parent / "tunnel.log.2").read_bytes(), b"a" * 10)

            log.write_bytes(b"c" * 20)
            self.assertTrue(rotate_log(log, max_bytes=10, backups=2))
            self.assertEqual((log.parent / "tunnel.log.1").read_bytes(), b"c" * 10)
            self.assertEqual((log.parent / "tunnel.log.2").read_bytes(), b"b" * 10)
            self.assertFalse((log.parent / "tunnel.log.3").exists())

    def test_launch_agents_are_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = launch_agent_payload(
                "ca.memorysafe.beta.tunnel",
                root / "scripts" / "run_tunnel_service.sh",
                root,
                "tunnel-service",
            )
            self.assertEqual(payload["ThrottleInterval"], 30)
            self.assertTrue(payload["KeepAlive"])

    def test_launcher_uses_space_safe_executable_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "MemorySafe Beta.app"
            create_launcher_app(app, root / "Application Support" / "MemorySafe", root / "missing.icns")
            with (app / "Contents" / "Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            self.assertEqual(info["CFBundleDisplayName"], "MemorySafe Beta")
            self.assertEqual(info["CFBundleExecutable"], "MemorySafeBeta")
            self.assertTrue((app / "Contents" / "MacOS" / "MemorySafeBeta").is_file())
            launcher = (app / "Contents" / "MacOS" / "MemorySafeBeta").read_text()
            self.assertIn("open_panel.sh", launcher)
            self.assertNotIn("/dashboard", launcher)

    def test_setup_service_yields_to_existing_memorysafe_dashboard(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "run_setup_service.sh").read_text()
        self.assertIn("memorysafe_dashboard_running", script)
        self.assertIn('payload.get("product") == "MemorySafe Beta"', script)
        self.assertIn("while memorysafe_dashboard_running", script)

    def test_cli_launcher_is_created_without_pip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Application Support" / "MemorySafe"
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("test\n")
            launcher = create_cli_launcher(root)
            rendered = launcher.read_text()
            self.assertIn("memorysafe_chatgpt.cli", rendered)
            self.assertEqual(launcher, root / "bin" / "memorysafe")
            self.assertNotIn("PYTHONPATH", rendered)
            self.assertIn("$INSTALL_ROOT/.venv/bin/python", rendered)

    @unittest.skipIf(os.name == "nt", "asserts a live 0o755 stat on a launcher written by "
                                       "scripts/install_macos.py, which is macOS-only ChatGPT-connector "
                                       "tooling; Windows also returns 0o666 for every "
                                       "file regardless of what create_cli_launcher sets, since NTFS has "
                                       "no POSIX mode bits to narrow -- "
                                       "test_cli_launcher_is_created_without_pip above still runs on "
                                       "Windows and covers the launcher's content")
    def test_cli_launcher_is_created_with_the_posix_exec_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Application Support" / "MemorySafe"
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("test\n")
            launcher = create_cli_launcher(root)
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
