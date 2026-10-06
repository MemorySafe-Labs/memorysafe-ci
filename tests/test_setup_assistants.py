"""scripts/setup_assistants.py now that Claude Code and Codex install MemorySafe as plugins.

Registering MemorySafe by hand as well lists every tool twice, so the macOS installer
asks only for the Claude Desktop step and tells the tester how to add the plugins.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

import scripts.setup_assistants as setup_assistants


ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CodexConfigIsValidTomlTests(unittest.TestCase):
    """The block configure_codex writes has to parse, on every platform.

    It did not on Windows. command and args went through json.dumps; the three env
    lines were interpolated raw, so a drive path put \\U straight into a TOML basic
    string, where it is an escape. Codex could then parse none of its config and
    started with no servers at all -- not just without MemorySafe. A machine here
    carried that block from a 0.3.x install until 21 September 2026, and it came
    back twice after being removed, which is how it was finally noticed.
    """

    def _section(self, install_root, database) -> str:
        import json

        launch = {"command": "cmd.exe", "args": ["/c", str(install_root)]}
        with patch.object(setup_assistants, "_mcp_launch", return_value=launch), tempfile.TemporaryDirectory() as home:
            config = Path(home) / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text("model = 'gpt-5'", encoding="utf-8")
            with patch.object(setup_assistants, "HOME", Path(home)):
                setup_assistants.configure_codex(install_root, database)
            return config.read_text(encoding="utf-8")

    def test_a_windows_path_still_parses(self) -> None:
        import tomllib

        root = PureWindowsPath(r"C:\Users\Unknown\AppData\Local\MemorySafe")
        text = self._section(root, root / "data" / "memorysafe.sqlite3")
        parsed = tomllib.loads(text)
        env = parsed["mcp_servers"]["memorysafe"]["env"]
        self.assertEqual(env["MEMORYSAFE_INSTALL_ROOT"], str(root))

    def test_a_posix_path_still_parses(self) -> None:
        import tomllib

        root = PurePosixPath("/Users/dana/Library/Application Support/MemorySafe")
        text = self._section(root, root / "data" / "memorysafe.sqlite3")
        env = tomllib.loads(text)["mcp_servers"]["memorysafe"]["env"]
        self.assertEqual(env["MEMORYSAFE_STATE_DIR"], str(root / "runtime-state"))

class SetupAssistantsTests(unittest.TestCase):
    def test_linux_setup_detects_lowercase_claude_desktop_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            (home / ".config" / "claude-desktop").mkdir(parents=True)
            with patch.object(setup_assistants.sys, "platform", "linux"), patch.object(
                setup_assistants, "HOME", home
            ), patch.object(setup_assistants.shutil, "which", return_value=None):
                found = setup_assistants.detect()

            self.assertTrue(found["claude_desktop"])

    def test_desktop_only_leaves_claude_code_and_codex_to_their_plugins(self) -> None:
        found = {"claude_desktop": True, "claude_code": True, "codex": True, "chatgpt_web": False}
        output = StringIO()
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            setup_assistants, "detect", return_value=found
        ), patch.object(setup_assistants, "configure_codex") as codex, patch.object(
            setup_assistants, "configure_claude_code"
        ) as claude_code, patch.object(
            setup_assistants, "reveal_claude_desktop_extension", return_value="opened"
        ), patch.dict(
            sys.modules, {"notify": types.SimpleNamespace(notify=lambda *args, **kwargs: None)}
        ), patch.object(
            setup_assistants.sys, "argv", ["setup_assistants.py", "--desktop-only", "--install-root", temporary]
        ), redirect_stdout(output):
            self.assertEqual(setup_assistants.main(), 0)
        codex.assert_not_called()
        claude_code.assert_not_called()
        self.assertIn("claude plugin install memorysafe@memorysafe", output.getvalue())
        self.assertIn("codex plugin add memorysafe@memorysafe", output.getvalue())

    def test_the_macos_installer_asks_for_desktop_only(self) -> None:
        self.assertIn('"--desktop-only"', (ROOT / "scripts" / "install_macos.py").read_text())

    def test_a_home_directory_that_cannot_be_resolved_does_not_break_import(self) -> None:
        """HOME = Path.home() at module scope used to run unconditionally at import
        time, with no platform gate, so importing this module for any reason at all --
        on any machine where Path.home() cannot resolve a home directory -- raised
        RuntimeError before a single line of the module's own logic ran. This module's
        only Windows caller was retired along with install_windows.py, and its
        remaining caller (install_macos.py) is macOS-only, so this is unreached today;
        it is still fixed, since an import-time raise is a trap for whoever imports the
        module next.
        """
        with patch("pathlib.Path.home", side_effect=RuntimeError("no home directory")):
            module = _load_script(
                "setup_assistants_no_home", ROOT / "scripts" / "setup_assistants.py"
            )
        self.assertIsNone(module.HOME)

    def test_canonical_install_root_survives_no_home_when_localappdata_is_set(self) -> None:
        """canonical_install_root()'s win32 branch used to read
        os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local") -- a .get() default
        evaluated unconditionally -- so once HOME can be None (see the test above) this
        raised TypeError on the overwhelming majority of real Windows machines, where
        LOCALAPPDATA is set and that fallback is never used.
        """
        with patch.object(setup_assistants, "HOME", None), patch.object(
            setup_assistants.sys, "platform", "win32"
        ), patch.dict("os.environ", {"LOCALAPPDATA": "C:\\Users\\t\\AppData\\Local"}, clear=True):
            root = setup_assistants.canonical_install_root()
        self.assertEqual(root, Path("C:\\Users\\t\\AppData\\Local") / "MemorySafe")

    def test_an_environment_install_root_never_evaluates_the_canonical_default(self) -> None:
        """main() used to read
        os.environ.get("MEMORYSAFE_INSTALL_ROOT", str(canonical_install_root())) --
        a .get() default evaluated unconditionally the moment that expression is
        reached, regardless of whether the env var is actually set. --install-root on
        the command line does not reach it at all (the outer "override or ..." already
        short-circuits then), so the repro needs the *env var* path specifically: no
        --install-root, MEMORYSAFE_INSTALL_ROOT set. canonical_database() legitimately
        calls canonical_install_root() once regardless (a separate, always-needed call
        this task did not touch), so the fix is proven by call count, not by
        forbidding the call outright.
        """
        found = {"claude_desktop": False, "claude_code": False, "codex": False, "chatgpt_web": False}
        output = StringIO()
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            setup_assistants, "detect", return_value=found
        ), patch.object(
            setup_assistants,
            "canonical_install_root",
            return_value=Path("/should-not-be-used-as-install-root"),
        ) as canonical, patch.object(
            setup_assistants.sys, "argv", ["setup_assistants.py"]
        ), patch.dict(
            "os.environ", {"MEMORYSAFE_INSTALL_ROOT": temporary}, clear=False
        ), redirect_stdout(output):
            self.assertEqual(setup_assistants.main(), 1)
        self.assertEqual(canonical.call_count, 1)


if __name__ == "__main__":
    unittest.main()
