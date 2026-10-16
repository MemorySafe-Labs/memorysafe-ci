from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import provisioning
from memorysafe_chatgpt.bootstrap_catalog import RESOURCES, SERVER_INSTRUCTIONS, TOOLS
from memorysafe_chatgpt.server import server


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "plugin" / "scripts" / "bootstrap_server.py"


def _load_bootstrap_server():
    spec = importlib.util.spec_from_file_location("memorysafe_bootstrap_server_test", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bootstrap_server = _load_bootstrap_server()

# Stands in for the real server: answers the private initialize after FAKE_CHILD_DELAY
# and every tools/call with "proxied", logging each call id it receives to FAKE_CHILD_LOG.
FAKE_CHILD = """
import json, os, sys, time
delay = float(os.environ.get("FAKE_CHILD_DELAY", "0"))
log = os.environ.get("FAKE_CHILD_LOG")
probe = os.environ.get("FAKE_CHILD_PORT_PROBE")
if probe:
    import socket
    port, path = probe.split(":", 1)
    try:
        socket.create_connection(("127.0.0.1", int(port)), timeout=0.25).close()
        state = "busy"
    except OSError:
        state = "free"
    with open(path, "w") as handle:
        handle.write(state)
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("method") == "initialize":
        time.sleep(delay)
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"}}}
        print(json.dumps(reply), flush=True)
    elif message.get("method") == "tools/call":
        if log:
            with open(log, "a") as handle:
                handle.write(str(message["id"]) + "\\n")
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                          "result": {"content": [{"type": "text", "text": "proxied"}]}}), flush=True)
""".lstrip()

# Stands in for install_runtime.py: records its PID, sleeps, then exits with FAKE_BUILD_EXIT.
FAKE_BUILD = """
import os, sys, time
record = os.environ.get("FAKE_BUILD_PID_FILE")
if record:
    with open(record, "w") as handle:
        handle.write(str(os.getpid()))
time.sleep(float(os.environ.get("FAKE_BUILD_DELAY", "0")))
sys.exit(int(os.environ.get("FAKE_BUILD_EXIT", "0")))
""".lstrip()


def _without_generated_titles(value):
    if isinstance(value, dict):
        return {
            key: _without_generated_titles(item)
            for key, item in value.items()
            if key not in {"title", "outputSchema"}
        }
    if isinstance(value, list):
        return [_without_generated_titles(item) for item in value]
    return value


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(port: int, path: str, timeout: float = 5.0) -> tuple:
    """(status code, body) once something answers on the port, retrying until timeout."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8")
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _refused(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.25).close()
    except OSError:
        return True
    return False


def _runtime_key() -> str:
    for line in (ROOT / "plugin" / "scripts" / "runtime.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("RUNTIME_KEY="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("plugin/scripts/runtime.env has no RUNTIME_KEY")


class BootstrapCatalogTests(unittest.TestCase):
    def test_lightweight_catalog_matches_the_real_server(self) -> None:
        async def inspect():
            live_tools = []
            for tool in await server.list_tools():
                payload = tool.model_dump(by_alias=True, exclude_none=True)
                live_tools.append(
                    {
                        key: payload[key]
                        for key in ("name", "title", "description", "inputSchema", "annotations", "_meta")
                        if key in payload
                    }
                )
            live_resources = [
                item.model_dump(by_alias=True, exclude_none=True)
                for item in await server.list_resources()
            ]
            return live_tools, live_resources

        live_tools, live_resources = asyncio.run(inspect())
        self.assertEqual(_without_generated_titles(TOOLS), _without_generated_titles(live_tools))
        self.assertEqual(RESOURCES, live_resources)

    def test_the_instructions_every_conversation_receives_hold_nothing_internal(self) -> None:
        """They go to every user of a public plugin. They pointed the model at a sales
        folder on one developer's Desktop, named a p-value, and denied using a research
        method no user has heard of, for a version that is not the one shipping."""
        for internal in ("~/", "sales-package", "CLAIMS_LOCK", "claims lock", "p-value", "p=0.017", "AUPRC", "MVI"):
            self.assertNotIn(internal, SERVER_INSTRUCTIONS)
        for kept in (
            "Search memory before answering about past work, people or decisions. ChatGPT.com is not on this SQLite. ",
            "Forget removes a memory from recall; it does not erase it. Quote memory content, not IDs.",
        ):
            self.assertIn(kept, SERVER_INSTRUCTIONS)

    def test_bootstrap_imports_only_the_lightweight_package_modules(self) -> None:
        """It runs before the runtime exists, on whatever python3 the machine has."""
        import ast

        tree = ast.parse(BOOTSTRAP.read_text())
        package_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("memorysafe_chatgpt"):
                package_imports.append(node.module)
            elif isinstance(node, ast.Import):
                package_imports.extend(
                    alias.name for alias in node.names if alias.name.startswith("memorysafe_chatgpt")
                )
        self.assertEqual(
            sorted(set(package_imports)),
            [
                "memorysafe_chatgpt.bootstrap_catalog",
                "memorysafe_chatgpt.progress_page",
                "memorysafe_chatgpt.provisioning",
            ],
        )


class WindowsCompatibilityTests(unittest.TestCase):
    def test_runtime_child_uses_the_windows_interpreter_layout(self) -> None:
        """Pending mode was dead on Windows: this built runtime/bin/python inline
        instead of reaching the os.name branch install_runtime already has."""
        with patch.object(bootstrap_server.os, "name", "nt"), \
             patch.dict(os.environ, {"MEMORYSAFE_INSTALL_ROOT": "C:\\data"}):
            command = bootstrap_server._runtime_child_command()
        self.assertIn("Scripts", command[0])
        self.assertTrue(command[0].endswith("python.exe"))

    def test_detached_build_uses_creationflags_on_windows(self) -> None:
        """start_new_session maps to setsid and is a silent no-op on Windows, so a
        host disconnect killed the build mid-flight and the next start began again."""
        with patch.object(bootstrap_server.os, "name", "nt"):
            options = bootstrap_server._detached_spawn_options()
        self.assertNotIn("start_new_session", options)
        self.assertEqual(
            options["creationflags"],
            bootstrap_server._DETACHED_PROCESS | bootstrap_server._CREATE_NEW_PROCESS_GROUP,
        )

    def test_detached_build_uses_a_new_session_on_posix(self) -> None:
        with patch.object(bootstrap_server.os, "name", "posix"):
            self.assertEqual(
                bootstrap_server._detached_spawn_options(), {"start_new_session": True}
            )

    def test_windows_creation_flags_are_literals(self) -> None:
        """subprocess exposes DETACHED_PROCESS and friends only on Windows, so
        reading them off the module would raise AttributeError on Linux - both in
        CI and in any test that patches os.name. They are spelled out instead.
        """
        self.assertEqual(bootstrap_server._DETACHED_PROCESS, 0x00000008)
        self.assertEqual(bootstrap_server._CREATE_NEW_PROCESS_GROUP, 0x00000200)
        self.assertEqual(bootstrap_server._CREATE_NO_WINDOW, 0x08000000)

    def test_stdout_is_reconfigured_before_the_proxy_can_write_anything(self) -> None:
        """Python opens stdout with newline=None on Windows, which rewrites every "\\n"
        this process writes to "\\r\\n" -- corrupting the newline-delimited JSON-RPC
        stream this is the only channel for. BootstrapProxy.__init__ can start
        background threads that write before main()'s own loop ever runs, so the real
        invariant is "before the proxy exists at all", not just "before the read loop"
        -- this asserts exactly that, by recording call order rather than bytes:
        os.linesep is already "\\n" on this POSIX box, so reconfigure(newline="") and
        the untouched default write identical bytes here regardless of the fix, and
        there is nothing to observe at the wire level on Linux either way.
        """
        order: list = []

        class RecordingStdout:
            def reconfigure(self, **kwargs) -> None:
                order.append(("reconfigure", kwargs))

            def write(self, text: str) -> None:
                order.append(("write", text))

            def flush(self) -> None:
                pass

        class FakeProxy:
            def __init__(self, pending: bool = False) -> None:
                order.append(("proxy_constructed", pending))

            def handle(self, message: dict) -> None:
                order.append(("handle", message))

            def close(self) -> None:
                order.append(("close", None))

        with patch.object(bootstrap_server.sys, "stdout", RecordingStdout()), \
             patch.object(bootstrap_server.sys, "stdin", iter([])), \
             patch.object(bootstrap_server, "BootstrapProxy", FakeProxy), \
             patch.dict(os.environ, {"MEMORYSAFE_RUNTIME_PENDING": "0"}):
            bootstrap_server.main()

        self.assertEqual(order[0], ("reconfigure", {"newline": ""}))
        self.assertEqual(order[1], ("proxy_constructed", False))


class ExplainFailureCachingTests(unittest.TestCase):
    """_explain_failure re-executed degraded_server.py by path on every call: once per
    progress-page poll (every 2s while a build is failed) and once per doctor call during
    a failed build, for what is a pure function lookup. _load_degraded_explain caches the
    loaded explain function so the module is loaded at most once per process."""

    def setUp(self) -> None:
        bootstrap_server._load_degraded_explain.cache_clear()

    def tearDown(self) -> None:
        bootstrap_server._load_degraded_explain.cache_clear()

    def test_degraded_server_is_loaded_only_once_across_two_calls(self) -> None:
        real_spec_from_file_location = bootstrap_server.importlib.util.spec_from_file_location
        calls = []

        def counting(*args, **kwargs):
            calls.append((args, kwargs))
            return real_spec_from_file_location(*args, **kwargs)

        with patch.object(bootstrap_server.importlib.util, "spec_from_file_location", counting):
            first = bootstrap_server._explain_failure("uv_checksum_mismatch")
            second = bootstrap_server._explain_failure("download_failed")

        self.assertEqual(len(calls), 1, "degraded_server.py should be loaded at most once")
        # Different reasons still get their own wording -- the cached callable, not a
        # cached result, is what gets reused.
        self.assertNotEqual(first, second)
        self.assertIn("checksum", first[0])
        self.assertIn("download", second[0])

    def test_a_failed_load_still_falls_back_and_is_not_permanently_cached(self) -> None:
        with patch.object(
            bootstrap_server.importlib.util,
            "spec_from_file_location",
            side_effect=OSError("no such file"),
        ):
            result = bootstrap_server._explain_failure("uv_checksum_mismatch")
        self.assertEqual(result, bootstrap_server._UNEXPLAINED_FAILURE)

        # A later, successful load is not blocked by the earlier failure: lru_cache never
        # memoizes a raised exception.
        result = bootstrap_server._explain_failure("uv_checksum_mismatch")
        self.assertNotEqual(result, bootstrap_server._UNEXPLAINED_FAILURE)


class _ProxyHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.fake_child = base / "fake_child.py"
        self.fake_child.write_text(FAKE_CHILD, encoding="utf-8")
        self.fake_build = base / "fake_build.py"
        self.fake_build.write_text(FAKE_BUILD, encoding="utf-8")
        self.child_log = base / "child-calls.log"
        self.build_pid_file = base / "fake-build.pid"
        self.data = base / "data"
        self.processes: list[subprocess.Popen] = []
        self.port = _free_port()

    def tearDown(self) -> None:
        for process in self.processes:
            process.terminate()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        # A build spawned just before the proxy was terminated may not have recorded its
        # PID yet, and may write that record while rmtree runs ("directory not empty" on
        # POSIX, WinError 32 on Windows). Cleanup is retried until it can be stopped, or
        # until it finishes on its own.
        deadline = time.monotonic() + 10
        while True:
            self._stop_detached_build()
            try:
                self.temporary.cleanup()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    def _stop_detached_build(self) -> None:
        """Stop the build the proxy started, which terminating the proxy does not.

        The proxy starts its build detached on purpose -- a first start's build must outlive
        a host that gives up on the proxy -- and that build has claude-install.log open as
        its stdout. Windows will not delete an open file, so a test that ended while the
        fake build was still sleeping failed tearDown with WinError 32 on windows-latest,
        which skipped the v0.4.3 release's publish job.
        """
        try:
            pid = int(self.build_pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            # TerminateProcess on Windows.
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + 5
        while provisioning.process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)

    def _launch(self, extra: dict[str, str], pop: tuple[str, ...] = ()) -> queue.Queue:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["MEMORYSAFE_BOOTSTRAP_CHILD_COMMAND"] = json.dumps(
            [sys.executable, str(self.fake_child)]
        )
        environment["FAKE_CHILD_LOG"] = str(self.child_log)
        environment["FAKE_BUILD_PID_FILE"] = str(self.build_pid_file)
        # Every proxy that starts in pending mode serves the progress page; never on the
        # machine's real dashboard address.
        environment["MEMORYSAFE_SETUP_PORT"] = str(self.port)
        environment.update(extra)
        for key in pop:
            environment.pop(key, None)
        process = subprocess.Popen(
            [sys.executable, str(BOOTSTRAP)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        self.processes.append(process)
        self.process = process
        replies: queue.Queue = queue.Queue()

        def read_replies() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                replies.put(json.loads(line))

        threading.Thread(target=read_replies, daemon=True).start()
        return replies

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def _handshake(self) -> None:
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call(self, request_id: int) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "memorysafe_health", "arguments": {}},
            }
        )

    @staticmethod
    def _reply(replies: queue.Queue, request_id: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no reply to request {request_id} within {timeout}s")
            reply = replies.get(timeout=remaining)
            if reply.get("id") == request_id:
                return reply


class BootstrapProtocolTests(_ProxyHarness):
    def test_initialize_and_discovery_do_not_wait_for_the_child(self) -> None:
        replies = self._launch({"FAKE_CHILD_DELAY": "5"})
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self._send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        initialized = self._reply(replies, 1, timeout=1)
        listed = self._reply(replies, 2, timeout=1)
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "memorysafe")
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            [tool["name"] for tool in TOOLS],
        )

    def test_requests_are_queued_then_proxied_after_child_initialization(self) -> None:
        replies = self._launch({"FAKE_CHILD_DELAY": "0.05"})
        self._handshake()
        self._call(3)
        self.assertEqual(self._reply(replies, 3, timeout=2)["result"]["content"][0]["text"], "proxied")

    def test_a_ready_runtime_serves_no_progress_page(self) -> None:
        replies = self._launch({"FAKE_CHILD_DELAY": "0.05"})
        self._handshake()
        self._call(3)
        self._reply(replies, 3, timeout=2)
        self.assertTrue(_refused(self.port))


class PendingModeTests(_ProxyHarness):
    """The plugin's first start: no runtime yet, and a host that will not wait for one."""

    def _start_pending(
        self, *, delay: str, exit_code: str = "0", deadline: str = "40", extra: dict | None = None
    ) -> queue.Queue:
        environment = {
            "MEMORYSAFE_RUNTIME_PENDING": "1",
            "MEMORYSAFE_RUNTIME_BUILD_COMMAND": json.dumps([sys.executable, str(self.fake_build)]),
            "MEMORYSAFE_INSTALL_ROOT": str(self.data),
            "MEMORYSAFE_STATE_DIR": str(self.data / "runtime-state"),
            "MEMORYSAFE_PENDING_DEADLINE_SECONDS": deadline,
            "FAKE_BUILD_DELAY": delay,
            "FAKE_BUILD_EXIT": exit_code,
        }
        environment.update(extra or {})
        return self._launch(environment)

    def _record_step(self, step: str) -> None:
        """What the real builder writes, from a builder that is this (live) test process."""
        from memorysafe_chatgpt import provisioning

        (self.data / "runtime").mkdir(parents=True, exist_ok=True)
        provisioning.Reporter(str(self.data), _runtime_key()).step(step)

    def test_handshake_is_answered_before_the_runtime_exists(self) -> None:
        replies = self._start_pending(delay="5")
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self._send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        initialized = self._reply(replies, 1, timeout=1)
        listed = self._reply(replies, 2, timeout=1)
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "memorysafe")
        self.assertEqual(len(listed["result"]["tools"]), len(TOOLS))

    def test_a_call_made_during_setup_is_answered_once_the_build_succeeds(self) -> None:
        replies = self._start_pending(delay="0.3")
        self._handshake()
        self._call(3)
        self.assertEqual(self._reply(replies, 3, timeout=5)["result"]["content"][0]["text"], "proxied")

    def test_a_failed_build_starts_limited_mode_that_explains_it(self) -> None:
        replies = self._start_pending(delay="0", exit_code="3")
        self._handshake()
        self._call(4)
        report = self._reply(replies, 4, timeout=10)["result"]["structuredContent"]
        self.assertEqual(report["reason_code"], "uv_checksum_mismatch")
        self.assertFalse(report["memorysafe_working"])
        self.assertTrue((self.data / "runtime-state" / "logs" / "claude-install.log").is_file())

    def test_a_successful_build_whose_runtime_child_cannot_start_ends_in_limited_mode(self) -> None:
        """A successful build is not the finish line: choosing the real runtime's command
        reads MEMORYSAFE_INSTALL_ROOT again, and with it unset that raised KeyError and
        killed the build thread silently -- every later call then got the "still finishing
        setup" message forever, because _child_failed was never reached. Reproduced by
        review: pending mode, a build that exits 0, and no MEMORYSAFE_INSTALL_ROOT."""
        replies = self._launch(
            {
                "MEMORYSAFE_RUNTIME_PENDING": "1",
                "MEMORYSAFE_RUNTIME_BUILD_COMMAND": json.dumps([sys.executable, str(self.fake_build)]),
                "MEMORYSAFE_STATE_DIR": str(self.data / "runtime-state"),
                "FAKE_BUILD_DELAY": "0",
                "FAKE_BUILD_EXIT": "0",
            },
            pop=("MEMORYSAFE_BOOTSTRAP_CHILD_COMMAND", "MEMORYSAFE_INSTALL_ROOT"),
        )
        self._handshake()
        self._call(7)
        report = self._reply(replies, 7, timeout=5)["result"]["structuredContent"]
        self.assertFalse(report["memorysafe_working"])
        self.assertEqual(report["reason_code"], "runtime_build_failed")

    def test_a_call_that_outwaits_the_deadline_is_answered_and_never_run(self) -> None:
        """Codex gives a tool call sixty seconds. A remember that timed out on the host
        side must not quietly run once setup finishes."""
        replies = self._start_pending(delay="2", deadline="0.5")
        self._handshake()
        self._call(5)
        late = self._reply(replies, 5, timeout=2)
        self.assertTrue(late["result"]["isError"])
        self.assertIn("one-time setup", late["result"]["content"][0]["text"])

        # Retried until the child is up, not called once after a fixed sleep. On a loaded
        # Windows machine the fake build and the fake child's two Python starts outlasted
        # 2.5 s, so a single call also expired and the test failed with the product working
        # (#4, 19 Sep). Every call that expires on the way is one more that must never run.
        request_id = 6
        give_up = time.monotonic() + 30
        while True:
            self._call(request_id)
            reply = self._reply(replies, request_id, timeout=5)
            # A child that died answers with a JSON-RPC error and no result at all; say so
            # rather than raising KeyError from inside the loop.
            self.assertIn("result", reply, reply.get("error"))
            if not reply["result"].get("isError") or time.monotonic() > give_up:
                break
            request_id += 1
        self.assertEqual(reply["result"]["content"][0]["text"], "proxied")
        self.assertEqual(self.child_log.read_text().split(), [str(request_id)])

    def test_teardown_stops_the_detached_build(self) -> None:
        """Terminating the proxy leaves its detached build running, holding the install log
        open; on Windows that made the temporary directory undeletable."""
        self._start_pending(delay="30")
        deadline = time.monotonic() + 10
        while not self.build_pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(self.build_pid_file.read_text(encoding="utf-8"))
        self.process.terminate()
        self.process.wait(timeout=5)
        self.assertTrue(provisioning.process_alive(pid))
        self._stop_detached_build()
        self.assertFalse(provisioning.process_alive(pid))

    def test_the_dashboard_address_shows_progress_during_the_build(self) -> None:
        """The README names this address as the proof an install worked, and it refused
        connections for the whole first build: a tester downloaded the extension three
        times in three minutes."""
        self._record_step("packages")
        self._start_pending(delay="5")
        code, body = _get(self.port, "/dashboard")
        self.assertEqual(code, 200)
        self.assertIn("step 3 of 5, installing packages", body)

    def test_the_page_is_gone_before_the_real_server_starts(self) -> None:
        """claude_launcher starts the real dashboard only on a free address."""
        probe = Path(self.temporary.name) / "port-at-child-start"
        replies = self._start_pending(delay="1", extra={"FAKE_CHILD_PORT_PROBE": f"{self.port}:{probe}"})
        self.assertEqual(_get(self.port, "/")[0], 200)
        self._handshake()
        self._call(3)
        self._reply(replies, 3, timeout=5)
        self.assertEqual(probe.read_text(), "free")

    def test_a_failed_build_keeps_explaining_itself_on_the_page(self) -> None:
        replies = self._start_pending(delay="0", exit_code="3")
        self._handshake()
        self._call(4)
        self._reply(replies, 4, timeout=10)
        code, body = _get(self.port, "/dashboard")
        self.assertEqual(code, 200)
        self.assertIn("checksum", body)
        self.assertIn("run the MemorySafe doctor", body)

    def test_the_doctor_answers_during_setup_without_waiting(self) -> None:
        """INSTALL.md sends people to the doctor when a fresh install does not respond, and
        it waited in the same queue as every other call."""
        self._record_step("python")
        replies = self._start_pending(delay="1.5")
        self._handshake()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "memorysafe_doctor", "arguments": {}},
            }
        )
        report = self._reply(replies, 8, timeout=1)["result"]["structuredContent"]
        self.assertEqual(report["status"], "provisioning")
        self.assertFalse(report["memorysafe_working"])
        self.assertIn("step 2 of 5", report["headline"])
        self.assertIn(f"http://127.0.0.1:{self.port}/dashboard", " ".join(report["what_to_do"]))
        self._call(9)
        self.assertEqual(self._reply(replies, 9, timeout=5)["result"]["content"][0]["text"], "proxied")
        self.assertEqual(self.child_log.read_text().split(), ["9"])

    def test_a_call_that_outwaits_the_deadline_is_told_the_current_step(self) -> None:
        self._record_step("verify")
        replies = self._start_pending(delay="3", deadline="0.5")
        self._handshake()
        self._call(5)
        text = self._reply(replies, 5, timeout=2)["result"]["content"][0]["text"]
        self.assertIn("step 4 of 5, checking the install", text)
        self.assertIn("Nothing was saved", text)
        self.assertIn(f"http://127.0.0.1:{self.port}/dashboard", text)


if __name__ == "__main__":
    unittest.main()
