"""plugin/scripts/start: which way a plugin start goes, and what it hands over.

A probe stands in for bootstrap_server.py and prints the decision it was given, so these
tests check the launcher's choices without starting a server or touching the network.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugin" / "scripts"
PROBE = """
import json, os, sys
print(json.dumps({
    "runtime": os.environ.get("FAKE_RUNTIME"),
    "pending": os.environ.get("MEMORYSAFE_RUNTIME_PENDING"),
    "root": os.environ.get("MEMORYSAFE_INSTALL_ROOT"),
    "db": os.environ.get("MEMORYSAFE_DB_PATH"),
    "state": os.environ.get("MEMORYSAFE_STATE_DIR"),
    "pythonpath": os.environ.get("PYTHONPATH"),
    "tiktoken_cache": os.environ.get("TIKTOKEN_CACHE_DIR"),
}))
"""
# Stands in for memorysafe_chatgpt.cli, so the shim can be run end to end.
CLI_PROBE = """
import json, os
print(json.dumps({
    "cli": True,
    "runtime": os.environ.get("FAKE_RUNTIME"),
    "root": os.environ.get("MEMORYSAFE_INSTALL_ROOT"),
    "pythonpath": os.environ.get("PYTHONPATH"),
    "tiktoken_cache": os.environ.get("TIKTOKEN_CACHE_DIR"),
}))
"""


@unittest.skipIf(os.name == "nt", "exercises scripts/start through /bin/sh, which does not exist on Windows; "
                                   "scripts/start.cmd is covered by WindowsLauncherTextTests below and, end to "
                                   "end, by the windows-latest first-run job")
class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.plugin = base / "plugin"
        scripts = self.plugin / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(SOURCE / "start", scripts / "start")
        shutil.copy2(SOURCE / "ensure_uv", scripts / "ensure_uv")
        (scripts / "runtime.env").write_text("RUNTIME_KEY=testkey\nPYTHON_VERSION=3.12\nUV_VERSION=0.12.15\n")
        (scripts / "uv-checksums").write_text("")
        (scripts / "bootstrap_server.py").write_text(PROBE)
        package = self.plugin / "src" / "memorysafe_chatgpt"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "cli.py").write_text(CLI_PROBE)
        self.home = base / "home"
        self.home.mkdir()
        self.data = base / "data"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, stdin_text: str | None = None, **extra: str) -> subprocess.CompletedProcess:
        environment = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "MEMORYSAFE_INSTALL_ROOT": str(self.data),
        }
        environment.update(extra)
        stdin = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(
            ["/bin/sh", str(self.plugin / "scripts" / "start")],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            **stdin,
        )

    def _decision(self, **extra: str) -> dict:
        result = self._run(**extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        # stdout is the MCP channel: the probe's one line is the only thing allowed on it.
        self.assertEqual(len(lines), 1, result.stdout)
        return json.loads(lines[0])

    def _fake_runtime(self, *, ready: bool) -> Path:
        runtime = self.data / "runtime" / "testkey"
        python = runtime / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\nFAKE_RUNTIME=1 exec {sys.executable} "$@"\n')
        python.chmod(0o755)
        if ready:
            (runtime / "ready").write_text("ready\n")
        return python

    def test_a_ready_runtime_is_used_directly(self) -> None:
        python = self._fake_runtime(ready=True)
        decision = self._decision()
        self.assertEqual((decision["runtime"], decision["pending"]), ("1", None))
        self.assertEqual(decision["db"], str(self.data / "data" / "memorysafe.sqlite3"))
        self.assertEqual(decision["state"], str(self.data / "runtime-state"))
        self.assertEqual(decision["pythonpath"], str(self.plugin / "src"))
        self.assertEqual(decision["tiktoken_cache"], str(self.data / "cache" / "tiktoken"))
        shim = self.data / "bin" / "memorysafe"
        self.assertTrue(os.access(shim, os.X_OK))
        self.assertIn(str(python), shim.read_text())
        self.assertIn("memorysafe_chatgpt.cli", shim.read_text())

    def test_a_runtime_without_its_ready_marker_is_not_trusted(self) -> None:
        self._fake_runtime(ready=False)
        decision = self._decision(PYTHON_SOURCE=sys.executable)
        self.assertEqual((decision["runtime"], decision["pending"]), (None, "1"))

    def test_a_first_start_hands_over_to_pending_mode(self) -> None:
        decision = self._decision(PYTHON_SOURCE=sys.executable)
        self.assertEqual((decision["runtime"], decision["pending"]), (None, "1"))
        self.assertTrue((self.data / "runtime-state" / "logs").is_dir())
        self.assertEqual(decision["tiktoken_cache"], str(self.data / "cache" / "tiktoken"))

    def test_a_first_start_writes_the_cli_shim(self) -> None:
        """INSTALL.md, the marketplace README and the skill all say to run
        <data root>/bin/memorysafe migrate right after installing, which is a pending
        start. The shim used to be written only once the runtime was ready, so the
        command they named did not exist during the first session."""
        self._decision(PYTHON_SOURCE=sys.executable)
        shim = self.data / "bin" / "memorysafe"
        self.assertTrue(shim.is_file())
        self.assertTrue(os.access(shim, os.X_OK))

    def test_the_cli_shim_says_setup_is_unfinished_before_the_runtime_exists(self) -> None:
        self._decision(PYTHON_SOURCE=sys.executable)
        result = subprocess.run(
            [str(self.data / "bin" / "memorysafe"), "doctor"],
            env={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "MemorySafe is still finishing its one-time setup. Progress: http://127.0.0.1:8765/dashboard",
            result.stderr,
        )

    def test_the_cli_shim_points_at_the_page_on_the_configured_port(self) -> None:
        """While the runtime builds the CLI cannot run the doctor at all; the progress page is
        its view of setup, so it must name the address the page is actually on."""
        self._decision(PYTHON_SOURCE=sys.executable, MEMORYSAFE_SETUP_PORT="9123")
        result = subprocess.run(
            [str(self.data / "bin" / "memorysafe"), "doctor"],
            env={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertIn("Progress: http://127.0.0.1:9123/dashboard", result.stderr)

    def test_the_cli_shim_runs_the_cli_on_the_runtime_python(self) -> None:
        self._fake_runtime(ready=True)
        self._decision()
        result = subprocess.run(
            [str(self.data / "bin" / "memorysafe")],
            env={"PATH": os.environ["PATH"], "HOME": str(self.home), "PYTHONPATH": "/somewhere/else"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "cli": True,
                "runtime": "1",
                "root": str(self.data),
                "pythonpath": str(self.plugin / "src"),
                "tiktoken_cache": str(self.data / "cache" / "tiktoken"),
            },
        )

    def test_a_first_start_writes_the_mcp_shim(self) -> None:
        """A stable command for MCP clients MemorySafe has no plugin for. Written on the
        first start like the CLI shim: the path people paste into a client's config has
        to exist before that client has ever launched it."""
        self._decision(PYTHON_SOURCE=sys.executable)
        shim = self.data / "bin" / "memorysafe-mcp"
        self.assertTrue(shim.is_file())
        self.assertTrue(os.access(shim, os.X_OK))

    def test_the_mcp_shim_starts_the_launcher_not_the_server(self) -> None:
        """A generic client has the same connection deadline the bootstrap proxy exists
        to meet, so the shim must go through the launcher and get the proxy, the
        first-start build and the degraded fallback -- not run the server directly."""
        self._fake_runtime(ready=True)
        self._decision()
        result = subprocess.run(
            [str(self.data / "bin" / "memorysafe-mcp")],
            env={"PATH": os.environ["PATH"], "HOME": str(self.home), "MEMORYSAFE_INSTALL_ROOT": str(self.data)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Reaching the probe at all is the point: it stands in for bootstrap_server.py,
        # so the one line on stdout is the proxy answering, and the environment in it is
        # the launcher's own work, which a shim that ran the server directly would skip.
        decision = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(decision["runtime"], "1")
        self.assertEqual(decision["root"], str(self.data))
        self.assertEqual(decision["pythonpath"], str(self.plugin / "src"))

    def test_the_mcp_shim_follows_the_plugin_that_started_last(self) -> None:
        """The plugin path carries the version and changes on every update. The shim is
        rewritten on every start, so a client configured once keeps working."""
        self._decision(PYTHON_SOURCE=sys.executable)
        newer = Path(self.temporary.name) / "plugin-0.5.0"
        shutil.copytree(self.plugin, newer)
        subprocess.run(
            ["/bin/sh", str(newer / "scripts" / "start")],
            env={"PATH": os.environ["PATH"], "HOME": str(self.home), "MEMORYSAFE_INSTALL_ROOT": str(self.data),
                 "PYTHON_SOURCE": sys.executable},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        shim = (self.data / "bin" / "memorysafe-mcp").read_text()
        self.assertIn(str(newer / "scripts" / "start"), shim)
        self.assertNotIn(str(self.plugin / "scripts" / "start"), shim)

    def test_the_mcp_shim_says_so_when_its_plugin_is_gone(self) -> None:
        """Removing MemorySafe from the host it was installed in leaves the shim pointing
        at nothing. A client would otherwise report only that the command failed."""
        self._decision(PYTHON_SOURCE=sys.executable)
        shutil.rmtree(self.plugin)
        result = subprocess.run(
            [str(self.data / "bin" / "memorysafe-mcp")],
            env={"PATH": os.environ["PATH"], "HOME": str(self.home)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("no longer installed", result.stderr)

    def test_an_inherited_pythonpath_cannot_shadow_the_pinned_dependencies(self) -> None:
        decision = self._decision(PYTHON_SOURCE=sys.executable, PYTHONPATH="/users/own/site-packages")
        self.assertEqual(decision["pythonpath"], str(self.plugin / "src"))

    def test_an_explicit_database_path_is_kept(self) -> None:
        decision = self._decision(PYTHON_SOURCE=sys.executable, MEMORYSAFE_DB_PATH="/elsewhere/store.sqlite3")
        self.assertEqual(decision["db"], "/elsewhere/store.sqlite3")

    def test_the_unresolved_desktop_token_means_the_standard_folder(self) -> None:
        xdg = Path(self.temporary.name) / "xdg"
        decision = self._decision(
            PYTHON_SOURCE=sys.executable,
            MEMORYSAFE_INSTALL_ROOT="${user_config.memory_directory}",
            XDG_DATA_HOME=str(xdg),
        )
        if platform.system() == "Linux":
            expected = xdg / "MemorySafe"
        else:
            expected = self.home / "Library" / "Application Support" / "MemorySafe"
        self.assertEqual(decision["root"], str(expected))

    def test_a_failed_cli_shim_write_is_logged(self) -> None:
        python = self._fake_runtime(ready=True)
        # Make data/bin unwritable by creating it as a file instead of a directory.
        # The launcher tries to mkdir -p it and write the shim, but fails silently
        # and should log the failure instead of discarding it.
        bin_path = self.data / "bin"
        bin_path.write_text("this is not a directory\n")
        decision = self._decision()
        # The launcher should still succeed and start the server.
        self.assertEqual((decision["runtime"], decision["pending"]), ("1", None))
        self.assertEqual(decision["db"], str(self.data / "data" / "memorysafe.sqlite3"))
        # The failure to write the shim should be logged.
        install_log = self.data / "runtime-state" / "logs" / "claude-install.log"
        self.assertTrue(install_log.exists())
        self.assertGreater(len(install_log.read_text()), 0)

    def test_uv_providing_python_reads_none_of_the_hosts_messages(self) -> None:
        """Without a Python, uv runs before the proxy exists, on the host's own stdin. Any
        of it uv read would be JSON-RPC the proxy never sees. And uv python install links
        python3.12 into ~/.local/bin unless told not to, outside the MemorySafe folder."""
        base = Path(self.temporary.name)
        calls = base / "uv-calls.log"
        swallowed = base / "uv-stdin.log"
        uv = base / "fake-uv"
        uv.write_text(
            "#!/bin/sh\n"
            f"echo \"$*\" >> '{calls}'\n"
            f"cat >> '{swallowed}'\n"
            f"if [ \"$1 $2\" = 'python find' ]; then echo '{sys.executable}'; fi\n"
        )
        uv.chmod(0o755)
        handshake = '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}\n'
        result = self._run(stdin_text=handshake, PYTHON_SOURCE="/nonexistent/python3", MEMORYSAFE_UV=str(uv))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pending"], "1")
        install, find = calls.read_text().splitlines()
        self.assertTrue(install.startswith("python install "), install)
        self.assertIn("--no-bin", install.split())
        self.assertTrue(find.startswith("python find "), find)
        self.assertEqual(swallowed.read_text(), "")

    def test_no_python_and_no_uv_fails_without_writing_to_stdout(self) -> None:
        result = self._run(PYTHON_SOURCE="/nonexistent/python3", MEMORYSAFE_UV="/nonexistent/uv")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("no usable Python", result.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "the Command Line Tools stub exists only on macOS")
    def test_the_macos_python_stub_is_never_run_without_command_line_tools(self) -> None:
        fake_bin = Path(self.temporary.name) / "bin"
        fake_bin.mkdir()
        xcode_select = fake_bin / "xcode-select"
        xcode_select.write_text("#!/bin/sh\nexit 2\n")
        xcode_select.chmod(0o755)
        result = self._run(
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            PYTHON_SOURCE="/usr/bin/python3",
            MEMORYSAFE_UV="/nonexistent/uv",
        )
        self.assertEqual(result.returncode, 1)


class WindowsLauncherTextTests(unittest.TestCase):
    """start.cmd is proven by the windows-latest first-run job, not here.

    These guard the three ways it has silently diverged from start before:
    a prepended PYTHONPATH let a developer's site-packages win over the
    hash-pinned ones, a missing TIKTOKEN_CACHE_DIR sent the tokenizer download
    outside the data root, and a hardcoded VERSION forced a rebuild on every
    release even when the lock had not moved.
    """

    def setUp(self) -> None:
        self.text = (ROOT / "plugin" / "scripts" / "start.cmd").read_text(encoding="utf-8")

    def test_overwrites_pythonpath_instead_of_prepending(self) -> None:
        self.assertIn('set "PYTHONPATH=%PLUGIN_DIR%\\src"', self.text)
        self.assertNotIn("%PYTHONPATH%", self.text)

    def test_clears_an_inherited_virtualenv(self) -> None:
        self.assertIn('set "VIRTUAL_ENV="', self.text)

    def test_keeps_the_tokenizer_cache_in_the_data_root(self) -> None:
        self.assertIn("TIKTOKEN_CACHE_DIR", self.text)

    def test_uses_the_runtime_key_not_a_hardcoded_version(self) -> None:
        self.assertIn("%RUNTIME_KEY%", self.text)
        self.assertNotIn("version-", self.text)
        self.assertNotIn("claude-runtime", self.text)

    def test_treats_the_unresolved_desktop_token_as_empty_first(self) -> None:
        token = "${user_config.memory_directory}"
        self.assertIn('if "%MEMORYSAFE_INSTALL_ROOT%"=="' + token + '"', self.text)
        self.assertLess(self.text.index(token), self.text.index("%LOCALAPPDATA%\\MemorySafe"))

    def test_never_reads_a_variable_the_host_strips(self) -> None:
        for name in ("COMSPEC", "PATHEXT", "TMP", "ProgramFiles(x86)", "windir"):
            self.assertNotIn("%" + name + "%", self.text)

    def test_hands_over_to_limited_mode_instead_of_exiting(self) -> None:
        # Exiting here shows as "Server disconnected", taking every MemorySafe tool
        # with it - including the one that could explain the failure.
        self.assertIn("degraded_server.py", self.text)
        self.assertIn("MEMORYSAFE_DEGRADED_REASON", self.text)

    def test_the_three_paths_appear_in_order(self) -> None:
        """The order IS the behaviour under test here, not just its presence.

        None of the tests in this class execute start.cmd - windows-latest CI is what
        proves it runs at all - so nothing here would fail if the fast, pending and
        uv-provisioned branches were silently reordered. That would be a real
        regression (e.g. always rebuilding instead of using a ready runtime, or
        trying uv before a plain `python3` on PATH), so pin the order by comparing
        the positions of one anchor unique to each branch: the `ready` marker check
        that only the fast path reads, MEMORYSAFE_RUNTIME_PENDING which only the
        pending path sets, and the call into ensure_uv.cmd which only the uv path
        makes.
        """
        fast = self.text.index("%RUNTIME_DIR%\\ready")
        pending = self.text.index("MEMORYSAFE_RUNTIME_PENDING")
        uv = self.text.index("ensure_uv.cmd")
        self.assertLess(fast, pending, "the fast path must be tried before the pending path")
        self.assertLess(pending, uv, "the pending path must be tried before the uv-provisioned path")

    def test_never_reads_a_stale_errorlevel(self) -> None:
        """%errorlevel% inside a parenthesized block is substituted when the block is
        parsed, before any command in it has run, so it reports the exit code from
        before the block instead of the child process's. start.cmd uses
        EnableDelayedExpansion and !errorlevel! everywhere an exit code from inside
        the same block is read; %errorlevel% must never reappear."""
        self.assertNotIn("%errorlevel%", self.text)
        self.assertIn("!errorlevel!", self.text)

    def _subroutine(self, label: str) -> str:
        """The body of one :label. `call :label` carries the label's own text, so an
        unanchored search finds the call site and returns the whole script after it."""
        body = self.text[self.text.index(f"\n:{label}\n") :]
        return body[: body.index("exit /b 0")]

    def test_writes_the_mcp_shim_beside_the_cli_one(self) -> None:
        self.assertIn("call :write_mcp_shim", self.text)
        self.assertIn(r'set "MCP_SHIM=%DATA_ROOT%\bin\memorysafe-mcp.cmd"', self.text)

    def test_the_mcp_shim_calls_the_launcher_not_the_server(self) -> None:
        """Same reason as POSIX: a generic client needs the bootstrap proxy, the
        first-start build and the degraded fallback, all of which live in start.cmd."""
        shim = self._subroutine("write_mcp_shim")
        self.assertIn(r'echo "%SCRIPT_DIR%start.cmd" %%*', shim)
        self.assertNotIn("bootstrap_server.py", shim)

    def test_the_mcp_shim_says_so_when_its_plugin_is_gone(self) -> None:
        shim = self._subroutine("write_mcp_shim")
        self.assertIn("no longer installed", shim)
        # cmd needs the redirection escaped, or start.cmd writes the message at
        # generation time instead of putting it in the shim.
        self.assertIn("^>^&2", shim)

    def test_the_cli_shim_points_at_the_progress_page(self) -> None:
        self.assertIn('set "SETUP_PORT=%MEMORYSAFE_SETUP_PORT%"', self.text)
        self.assertIn('if "%SETUP_PORT%"=="" set "SETUP_PORT=8765"', self.text)
        self.assertIn("Progress: http://127.0.0.1:%SETUP_PORT%/dashboard", self.text)
        self.assertNotIn("Try again in a minute", self.text)
        # Expanded when :write_cli_shim's block is parsed, so it must be set before the call.
        self.assertLess(self.text.index('set "SETUP_PORT='), self.text.index("call :write_cli_shim"))


class LauncherParityTests(unittest.TestCase):
    """start and start.cmd are twins for uv's environment; the settings must match.

    Task 3's review found start.cmd complete but start missing UV_PYTHON_PREFERENCE and
    the VIRTUAL_ENV strip. Checking both files in the same test is what keeps that gap
    from reopening: a setting added to one launcher and not the other is exactly how they
    drifted apart the first time.
    """

    def setUp(self) -> None:
        self.posix = (ROOT / "plugin" / "scripts" / "start").read_text(encoding="utf-8")
        self.windows = (ROOT / "plugin" / "scripts" / "start.cmd").read_text(encoding="utf-8")

    def test_both_launchers_pin_the_same_uv_settings(self) -> None:
        """POSIX shipped without stripping VIRTUAL_ENV while Windows stripped it.

        An activated venv in the user's shell is inherited by the launcher, and uv
        would then resolve against it instead of the private runtime - the same bug
        the launcher already prevents for PYTHONPATH. The two launchers are twins;
        a setting present in one and absent in the other is how they drift apart.
        """
        for setting in ("UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "UV_PYTHON_PREFERENCE"):
            self.assertIn(setting, self.posix, setting)
            self.assertIn(setting, self.windows, setting)
        self.assertIn("only-managed", self.posix)
        self.assertIn("only-managed", self.windows)

    def test_neither_launcher_inherits_an_activated_virtualenv(self) -> None:
        self.assertIn("unset VIRTUAL_ENV", self.posix)
        self.assertIn('set "VIRTUAL_ENV="', self.windows)

    def test_windows_scopes_uv_python_preference_to_the_install_call(self) -> None:
        """Real uv (0.12.11) rejects --managed-python and --python-preference together:
        `error: the argument \\`--managed-python\\` cannot be used with \\`--python-preference\\``.
        Windows batch `set` is not scoped to a single command the way POSIX start's inline
        `VAR=value command` is - a `set` persists for the rest of the script. Setting
        UV_PYTHON_PREFERENCE before `uv python install` and never clearing it would leave it
        standing when `uv python find --managed-python` runs next, so uv would error out,
        MANAGED_PYTHON would stay empty, and every Python-less Windows machine would fall
        through to the degraded server instead of getting a managed interpreter. The fix
        clears the variable between the two calls; this pins that it stays cleared there.
        """
        install = self.windows.index("python install")
        cleared = self.windows.index('set "UV_PYTHON_PREFERENCE="')
        find = self.windows.index("python find")
        self.assertLess(install, cleared, "UV_PYTHON_PREFERENCE must be cleared after python install")
        self.assertLess(cleared, find, "UV_PYTHON_PREFERENCE must be cleared before python find")

    def test_the_windows_launchers_are_checked_out_with_crlf(self) -> None:
        """cmd.exe resolves `call :label` by byte offset, and on an LF-only batch file it
        lands mid-line and reports "The system cannot find the batch label specified".
        Whether it does depends on where the label sits, so start.cmd worked by luck:
        adding one `rem` line above :write_cli_shim made the call fail, the CLI shim was
        never written, and nothing said so -- the server still starts, so only the
        first-run E2E caught it. .gitattributes gives these files eol=crlf; this fails if
        that carve-out is dropped, before the next edit walks into it again."""
        for name in ("start.cmd", "ensure_uv.cmd"):
            raw = (ROOT / "plugin" / "scripts" / name).read_bytes()
            self.assertEqual(
                raw.count(b"\n") - raw.count(b"\r\n"),
                0,
                f"{name} has bare LF lines. An existing clone keeps whatever it "
                "checked out before .gitattributes gained eol=crlf for these: "
                "git checkout -- plugin/scripts to refresh it.",
            )

    def test_both_launchers_write_the_mcp_shim(self) -> None:
        """The stable path other MCP clients are told to use. Present on one platform and
        absent on the other is exactly the drift this class exists to catch."""
        self.assertIn("write_mcp_shim", self.posix)
        self.assertIn("write_mcp_shim", self.windows)
        self.assertIn("bin/memorysafe-mcp", self.posix)
        self.assertIn(r"bin\memorysafe-mcp.cmd", self.windows)

    def test_both_install_documents_give_other_clients_the_shim_path(self) -> None:
        """A stable path nobody is told about connects nothing."""
        for name in ("plugin/INSTALL.md", "plugin/README-template.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("Other MCP clients", text, name)
            self.assertIn("MemorySafe/bin/memorysafe-mcp", text, name)
            self.assertIn(r"MemorySafe\bin\memorysafe-mcp.cmd", text, name)

    def test_both_shims_send_an_unfinished_setup_to_the_progress_page(self) -> None:
        for name in ("start", "start.cmd"):
            text = (ROOT / "plugin" / "scripts" / name).read_text(encoding="utf-8")
            self.assertIn("MemorySafe is still finishing its one-time setup. Progress: http://127.0.0.1:", text, name)
            self.assertNotIn("Try again in a minute", text, name)


if __name__ == "__main__":
    unittest.main()
