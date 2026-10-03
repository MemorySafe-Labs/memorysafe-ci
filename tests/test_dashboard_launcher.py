"""Which dashboard the plugin launcher may replace on port 8765.

Only one it started itself, from an older plugin: an update deletes that plugin's
folder while its dashboard keeps the port. A launchd-managed dashboard, or anything
else on the port, is never touched.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import claude_launcher
from memorysafe_chatgpt.bootstrap_catalog import VERSION



# Captured before setUpModule replaces the attribute, for the one test that has to
# exercise the real thing.
_the_real_window_opener = claude_launcher._open_the_window_in_the_background
_no_real_window = None


def setUpModule() -> None:
    """Stop a first start from opening a real browser window during the run.

    _start_dashboard hands the window to a daemon thread that polls for the page and
    then shells out to xdg-open or open. A test that forgets to stop it leaves that
    thread running after it finishes, and the call lands inside whatever test is
    running seconds later. That is how
    test_a_dashboard_it_started_from_an_older_plugin_is_replaced failed on CI with
    Popen called twice, the second an xdg-open, while passing every other run.

    Stopping it per test works until the next test forgets. This cannot forget.
    Tests that assert on the window patch this again themselves.
    """
    global _no_real_window
    _no_real_window = patch.object(claude_launcher, "_open_the_window_in_the_background")
    _no_real_window.start()


def tearDownModule() -> None:
    if _no_real_window is not None:
        _no_real_window.stop()

class DashboardLauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"MEMORYSAFE_STATE_DIR": str(self.state)})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def _record(self, version: str, pid: int = 4242) -> None:
        (self.state / "dashboard.json").write_text(
            json.dumps({"pid": pid, "version": version, "plugin_root": "/old/plugin"})
        )

    def _start(
        self,
        *,
        running: list[bool],
        reported: str | None = None,
        reported_pid: int | None = 4242,
        alive: bool = True,
        kill_error: Exception | None = None,
    ):
        """Start the dashboard with the stale-dashboard termination path mocked.

        _replace_stale_dashboard signals the stale process differently by platform: POSIX
        calls os.kill(pid, SIGTERM); real Windows (os.name == "nt", not simulated here --
        see claude_launcher._replace_stale_dashboard) calls subprocess.run(["taskkill", ...])
        instead. Mocking only Popen and leaving subprocess.run real meant that on Windows
        the taskkill branch ran the real subprocess.run, which builds its own Popen and
        unpacks communicate() from it -- but Popen was mocked, so unpacking a bare
        MagicMock crashed. subprocess.run is never used for anything else in this flow, so
        it is safe to mock outright and treat like the POSIX os.kill mock: kill_error
        becomes its side_effect, and the returned mock is what callers assert against.
        """
        status = None if reported is None else {"version": reported, "pid": reported_pid}
        with patch.object(claude_launcher, "_dashboard_is_running", side_effect=running), patch.object(
            claude_launcher, "_dashboard_status", return_value=status
        ), patch.object(claude_launcher, "_process_alive", return_value=alive), patch.object(
            claude_launcher.subprocess, "Popen"
        ) as popen, patch.object(
            claude_launcher.time, "sleep"
        ), patch.object(
            claude_launcher, "_open_the_window_in_the_background"
        ) as thread:
            # A first start hands the window to a thread. Every test here gets a fresh
            # state dir, so every one of them is a first start; left real, each would
            # leave a thread polling a mocked port for the whole timeout.
            self.window_thread = thread
            popen.return_value.pid = 5151
            if os.name == "nt":
                with patch.object(claude_launcher.subprocess, "run", side_effect=kill_error) as run:
                    claude_launcher._start_dashboard()
                return run, popen
            with patch.object(claude_launcher.os, "kill", side_effect=kill_error) as kill:
                claude_launcher._start_dashboard()
            return kill, popen

    def _written_record(self) -> dict:
        return json.loads((self.state / "dashboard.json").read_text())

    def _assert_terminated(self, kill, pid: int) -> None:
        """Assert the mock _start() returned was called the way _replace_stale_dashboard
        actually signals a stale dashboard on this host: SIGTERM via os.kill on POSIX,
        taskkill via subprocess.run on real Windows."""
        if os.name == "nt":
            kill.assert_called_once_with(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=claude_launcher._CREATE_NO_WINDOW,
            )
        else:
            kill.assert_called_once_with(pid, signal.SIGTERM)

    def test_a_free_port_starts_a_dashboard_and_records_it(self) -> None:
        kill, popen = self._start(running=[False])
        popen.assert_called_once()
        kill.assert_not_called()
        self.assertEqual((self._written_record()["pid"], self._written_record()["version"]), (5151, VERSION))

    def test_an_unrecorded_dashboard_is_left_alone(self) -> None:
        kill, popen = self._start(running=[True], reported="0.3.0")
        kill.assert_not_called()
        popen.assert_not_called()

    def test_a_dashboard_it_started_from_an_older_plugin_is_replaced(self) -> None:
        self._record("0.3.0")
        kill, popen = self._start(running=[True, False], reported="0.3.0", reported_pid=4242)
        self._assert_terminated(kill, 4242)
        popen.assert_called_once()
        self.assertEqual(self._written_record()["pid"], 5151)

    def test_a_recorded_pid_that_does_not_serve_the_port_is_never_signalled(self) -> None:
        """After a reboot the recorded PID can belong to an unrelated process, while a
        launchd dashboard on the port reports the very version that was recorded. A live
        PID and a matching version proved nothing; only the port naming the PID does."""
        self._record("0.3.0", pid=4242)
        kill, popen = self._start(running=[True], reported="0.3.0", reported_pid=777)
        kill.assert_not_called()
        popen.assert_not_called()

    def test_a_dashboard_that_does_not_report_its_pid_is_never_signalled(self) -> None:
        self._record("0.3.0", pid=4242)
        kill, popen = self._start(running=[True], reported="0.3.0", reported_pid=None)
        kill.assert_not_called()
        popen.assert_not_called()

    def test_the_status_read_returns_the_version_and_the_serving_pid(self) -> None:
        def answer(payload: dict) -> io.BytesIO:
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        cases = (
            ({"product": "MemorySafe Beta", "version": "0.3.0", "pid": 42}, {"version": "0.3.0", "pid": 42}),
            ({"product": "MemorySafe Beta", "version": "0.3.0"}, {"version": "0.3.0", "pid": None}),
            ({"product": "MemorySafe Beta", "version": "0.3.0", "pid": "42"}, {"version": "0.3.0", "pid": None}),
            ({"product": "Something else", "version": "0.3.0", "pid": 42}, None),
        )
        for payload, expected in cases:
            with patch.object(claude_launcher.urllib.request, "urlopen", return_value=answer(payload)):
                self.assertEqual(claude_launcher._dashboard_status("127.0.0.1", 8765), expected, payload)

    def test_a_current_dashboard_is_left_alone(self) -> None:
        self._record(VERSION)
        kill, popen = self._start(running=[True], reported=VERSION)
        kill.assert_not_called()
        popen.assert_not_called()

    def test_a_dashboard_that_is_not_the_recorded_one_is_left_alone(self) -> None:
        """The recorded process may have exited and a launchd agent taken the port."""
        self._record("0.3.0")
        kill, popen = self._start(running=[True], reported="0.2.9")
        kill.assert_not_called()
        popen.assert_not_called()

    def test_a_dead_recorded_process_is_never_signalled(self) -> None:
        self._record("0.3.0")
        kill, popen = self._start(running=[True], reported="0.3.0", alive=False)
        kill.assert_not_called()
        popen.assert_not_called()

    def test_a_pid_reused_since_the_aliveness_check_is_never_fatal(self) -> None:
        """The recorded process can exit during the network round trip in
        _dashboard_status. _process_alive already treats a reused PID's
        PermissionError as "alive", so the kill that follows can be the one
        that discovers the process is gone (ProcessLookupError) or now
        belongs to someone else (PermissionError). Either must degrade to
        leaving the port alone, not propagate out of _start_dashboard and
        stop the MCP server from starting."""
        self._record("0.3.0")
        kill, popen = self._start(running=[True], reported="0.3.0", kill_error=ProcessLookupError())
        self._assert_terminated(kill, 4242)
        popen.assert_not_called()

    def test_a_kill_forbidden_by_the_os_is_never_fatal(self) -> None:
        self._record("0.3.0")
        kill, popen = self._start(running=[True], reported="0.3.0", kill_error=PermissionError())
        self._assert_terminated(kill, 4242)
        popen.assert_not_called()

    def test_process_alive_never_signals_on_windows(self) -> None:
        """os.kill(pid, 0) TERMINATES the target on Windows, which is why the old
        guard returned False unconditionally - and why the dashboard never started
        there at all. Liveness must go through OpenProcess, and os.kill must not be
        reached on nt even by accident.
        """
        asked = []

        def explode(*args, **kwargs):
            self.fail("os.kill must never run on Windows: signal 0 terminates the target")

        with patch.object(claude_launcher.os, "name", "nt"), \
             patch.object(claude_launcher, "_windows_process_alive", lambda pid: asked.append(pid) or True), \
             patch.object(claude_launcher.os, "kill", explode):
            self.assertTrue(claude_launcher._process_alive(4321))
        self.assertEqual(asked, [4321])

    def test_process_alive_still_signals_on_posix(self) -> None:
        seen = []
        with patch.object(claude_launcher.os, "name", "posix"), \
             patch.object(claude_launcher.os, "kill", lambda pid, sig: seen.append((pid, sig))):
            self.assertTrue(claude_launcher._process_alive(99))
        self.assertEqual(seen, [(99, 0)])

    def test_dashboard_spawn_detaches_without_a_console_on_windows(self) -> None:
        with patch.object(claude_launcher.os, "name", "nt"):
            options = claude_launcher._dashboard_spawn_options()
        self.assertNotIn("start_new_session", options)
        self.assertEqual(
            options["creationflags"],
            claude_launcher._DETACHED_PROCESS | claude_launcher._CREATE_NO_WINDOW,
        )


class FirstStartWindowTests(unittest.TestCase):
    """The dashboard shows itself once, on the first start after an install.

    It is where an install proves itself and where the other assistants get connected,
    so someone who never opens it never sees either. Once, though: a window on every
    start is spam, and an install that updates to this version is not a first start.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"MEMORYSAFE_STATE_DIR": str(self.state)})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def _start(self):
        with patch.object(claude_launcher, "_dashboard_is_running", return_value=False), patch.object(
            claude_launcher.subprocess, "Popen"
        ) as popen, patch.object(claude_launcher, "_open_the_window_in_the_background") as window:
            popen.return_value.pid = 5151
            claude_launcher._start_dashboard()
        return window

    def test_a_first_start_opens_the_window_and_records_that_it_did(self) -> None:
        window = self._start()
        window.assert_called_once_with("127.0.0.1", 8765)
        self.assertTrue((self.state / "dashboard-opened.json").exists())

    def test_the_window_goes_to_a_daemon_thread(self) -> None:
        """Waiting for a browser must never hold up the MCP server the host is timing.

        Patched here rather than in the helper above, because patching threading
        itself reaches every thread in the process -- subprocess.run uses one to read
        a child's output -- and doing that broke six unrelated tests elsewhere.
        """
        with patch.object(claude_launcher.threading, "Thread") as thread:
            _the_real_window_opener("127.0.0.1", 8765)
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs["target"], claude_launcher._open_dashboard_when_ready)
        self.assertEqual(thread.call_args.kwargs["args"], ("127.0.0.1", 8765))
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once()

    def test_the_second_start_opens_nothing(self) -> None:
        self._start()
        self.assertIsNone(self._start().call_args)

    def test_an_install_that_has_started_a_dashboard_before_is_not_a_first_start(self) -> None:
        """The record predates this feature, so updating to it never costs a window."""
        (self.state / "dashboard.json").write_text(json.dumps({"pid": 11, "version": "0.4.6"}))
        self.assertIsNone(self._start().call_args)

    def test_the_record_written_for_this_start_does_not_count_as_a_previous_one(self) -> None:
        """_start_dashboard writes dashboard.json itself; reading it after would make
        every start look like one that had come before, and no window would ever open."""
        thread = self._start()
        thread.assert_called_once()

    def test_a_state_dir_it_cannot_write_still_gets_its_window(self) -> None:
        with patch.object(claude_launcher.os, "replace", side_effect=OSError("read-only")):
            self._start().assert_called_once()

    def test_the_window_waits_for_the_page_to_answer(self) -> None:
        answers = [None, None, {"version": VERSION, "pid": 9}]
        with patch.object(claude_launcher, "_dashboard_status", side_effect=answers), patch.object(
            claude_launcher.time, "sleep"
        ), patch.object(claude_launcher, "_open_dashboard_window") as opened:
            claude_launcher._open_dashboard_when_ready("127.0.0.1", 8765)
        opened.assert_called_once_with("http://127.0.0.1:8765/dashboard")

    def test_a_page_that_never_answers_opens_no_window(self) -> None:
        with patch.object(claude_launcher, "_dashboard_status", return_value=None), patch.object(
            claude_launcher.time, "sleep"
        ), patch.object(claude_launcher, "_FIRST_OPEN_TIMEOUT_SECONDS", 0.05), patch.object(
            claude_launcher, "_open_dashboard_window"
        ) as opened:
            claude_launcher._open_dashboard_when_ready("127.0.0.1", 8765)
        opened.assert_not_called()

    def test_a_browser_that_will_not_start_never_reaches_the_mcp_server(self) -> None:
        """This runs in a thread beside a serving MCP server; it may not raise."""
        with patch.object(
            claude_launcher, "_dashboard_status", return_value={"version": VERSION, "pid": 9}
        ), patch.object(claude_launcher, "_open_dashboard_window", side_effect=OSError("no browser")):
            claude_launcher._open_dashboard_when_ready("127.0.0.1", 8765)


class DefaultStateDirTests(unittest.TestCase):
    """_start_dashboard's MEMORYSAFE_STATE_DIR fallback used to hardcode
    ~/Library/Application Support with no platform branch at all, so on Windows the
    state directory resolved to a macOS-only path that cannot exist there. Every test
    above pins MEMORYSAFE_STATE_DIR explicitly in setUp, so none of them ever exercised
    this default -- and in a real install the launcher always sets it too, so the bug
    was invisible until MEMORYSAFE_STATE_DIR was unset for some other reason.

    _default_state_dir() branches on sys.platform, not os.name -- that is the attribute
    patched below. Patching os.name instead would flip pathlib's own POSIX/NT flavour
    and make Path.home() raise "Could not determine home directory" (or constructing a
    Path raise NotImplementedError) on this POSIX test runner, a wall earlier tasks in
    this plan hit twice. Expected paths are also built from literal strings, with HOME
    pinned in the patched environment, rather than from a Path.home() call made inside
    or outside the patch that could silently disagree with it.
    """

    def test_win32_uses_localappdata(self) -> None:
        expected = Path("C:\\Users\\t\\AppData\\Local") / "MemorySafe" / "runtime-state"
        with patch.object(claude_launcher.sys, "platform", "win32"), patch.dict(
            os.environ, {"LOCALAPPDATA": "C:\\Users\\t\\AppData\\Local"}, clear=True
        ):
            self.assertEqual(claude_launcher._default_state_dir(), expected)

    def test_darwin_uses_application_support(self) -> None:
        """Path.home() is resolved by the concrete Path subclass Python picked for this
        host (PosixPath on POSIX, WindowsPath on Windows), not by the sys.platform value
        patched above -- Path.__new__ chooses that subclass from the real os.name. A
        WindowsPath's expanduser() reads USERPROFILE (falling back to HOMEDRIVE+HOMEPATH),
        never HOME, so setting only HOME here left Path.home() unable to resolve "~" on a
        real Windows runner regardless of which platform was being simulated. Setting both
        keys the same way satisfies whichever flavour Path actually is on this host.
        """
        expected = Path("/Users/t") / "Library" / "Application Support" / "MemorySafe" / "runtime-state"
        with patch.object(claude_launcher.sys, "platform", "darwin"), patch.dict(
            os.environ, {"HOME": "/Users/t", "USERPROFILE": "/Users/t"}, clear=True
        ):
            self.assertEqual(claude_launcher._default_state_dir(), expected)

    def test_linux_uses_xdg_data_home_when_set(self) -> None:
        expected = Path("/x/data") / "MemorySafe" / "runtime-state"
        with patch.object(claude_launcher.sys, "platform", "linux"), patch.dict(
            os.environ, {"XDG_DATA_HOME": "/x/data", "HOME": "/home/t"}, clear=True
        ):
            self.assertEqual(claude_launcher._default_state_dir(), expected)

    def test_linux_falls_back_to_dot_local_share_without_xdg(self) -> None:
        """See test_darwin_uses_application_support above: Path.home() is resolved by the
        real host's Path flavour, and a WindowsPath never consults HOME, only USERPROFILE
        (or HOMEDRIVE+HOMEPATH) -- so both are set here too."""
        expected = Path("/home/t") / ".local" / "share" / "MemorySafe" / "runtime-state"
        with patch.object(claude_launcher.sys, "platform", "linux"), patch.dict(
            os.environ, {"HOME": "/home/t", "USERPROFILE": "/home/t"}, clear=True
        ):
            self.assertEqual(claude_launcher._default_state_dir(), expected)


class StartDashboardStateDirTests(unittest.TestCase):
    """_start_dashboard read the state dir as
    os.environ.get("MEMORYSAFE_STATE_DIR", _default_state_dir()), and a function call
    passed as a .get() default is evaluated before the call runs -- so this always ran
    _default_state_dir(), including its win32 Path.home() fallback, even when
    MEMORYSAFE_STATE_DIR was already set and its result was about to be discarded.
    Path.home() raises RuntimeError when it cannot resolve a home directory, so a
    caller who had correctly set MEMORYSAFE_STATE_DIR still crashed on a machine where
    that would have raised. Fixed with `or`, which short-circuits.
    """

    def test_an_explicit_state_dir_is_used_without_ever_calling_the_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            with patch.dict(
                os.environ, {"MEMORYSAFE_STATE_DIR": str(state)}, clear=True
            ), patch.object(
                claude_launcher,
                "_default_state_dir",
                side_effect=AssertionError("must not be called when MEMORYSAFE_STATE_DIR is set"),
            ), patch.object(
                claude_launcher, "_dashboard_is_running", return_value=False
            ), patch.object(
                claude_launcher.subprocess, "Popen"
            ) as popen:
                popen.return_value.pid = 1234
                claude_launcher._start_dashboard()
            self.assertTrue((state / "logs").is_dir())


if __name__ == "__main__":
    unittest.main()
