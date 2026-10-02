#!/usr/bin/env python3
"""Play the host against a freshly built plugin on a machine where nothing is set up yet.

This downloads uv, a Python and the pinned packages for real, so it runs in its own CI
job and never in the unit suite:

    python3 tests/e2e/first_run.py dist/memorysafe-plugin/plugins/memorysafe
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HANDSHAKE_SECONDS = 2.0
SETUP_SECONDS = 300.0
FACT = "The first-run check stores this fact."


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    print(f"ok   {message}", flush=True)


def launch_command(scripts: Path) -> list[str]:
    # The marketplace entry names an extensionless command and the host resolves the
    # extension. This mirrors that resolution so the e2e test exercises the same file
    # the host would pick.
    if os.name == "nt":
        return [str(scripts / "start.cmd")]
    return ["/bin/sh", str(scripts / "start")]


def runtime_python(runtime: Path) -> Path:
    # Mirrors install_runtime.runtime_python: a relocatable venv's interpreter sits at
    # Scripts\python.exe on Windows and bin/python everywhere else.
    if os.name == "nt":
        return runtime / "Scripts" / "python.exe"
    return runtime / "bin" / "python"


def cli_shim(data_root: Path) -> Path:
    # start.cmd writes memorysafe.cmd so cmd.exe's PATHEXT resolution finds it; start
    # writes the extensionless memorysafe run through its shebang.
    if os.name == "nt":
        return data_root / "bin" / "memorysafe.cmd"
    return data_root / "bin" / "memorysafe"


def dump_diagnostics(plugin: Path, data_root: Path) -> None:
    """Print what a failure needs, since it all lives under a temp data root that CI
    throws away once the job ends. Called from main()'s failure path only -- a passing
    run never calls this and stays quiet.
    """
    banner = "=" * 70
    print(f"\n{banner}\nFIRST-RUN DIAGNOSTICS\n{banner}", file=sys.stderr)
    print(f"data root: {data_root}", file=sys.stderr)

    runtime_env_path = plugin / "scripts" / "runtime.env"
    uv_version = None
    try:
        for line in runtime_env_path.read_text().splitlines():
            if line.startswith("UV_VERSION="):
                uv_version = line.split("=", 1)[1]
                break
    except OSError as error:
        print(f"could not read {runtime_env_path}: {error}", file=sys.stderr)

    if uv_version:
        # Distinguishes "never downloaded" from "downloaded and rejected": ensure_uv(.cmd)
        # only moves the binary into place once it passes its checksum.
        uv_name = "uv.exe" if os.name == "nt" else "uv"
        uv_path = data_root / "tools" / f"uv-{uv_version}" / uv_name
        print(f"{uv_path}: {'present' if uv_path.is_file() else 'absent'}", file=sys.stderr)
    else:
        print(f"could not read UV_VERSION from {runtime_env_path}; skipping the uv binary check", file=sys.stderr)

    log = data_root / "runtime-state" / "logs" / "claude-install.log"
    print(f"\n--- {log} ---", file=sys.stderr)
    if log.is_file():
        print(log.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
    else:
        print("no install log was written", file=sys.stderr)
    print(f"--- end {log.name} ---", file=sys.stderr)
    print(f"{banner}\n", file=sys.stderr)
    sys.stderr.flush()


class BuildClock:
    """When each step of the runtime build began, read from the builder's progress record.

    The checks above say only that setup finished. On Windows it finished four times later
    than on Linux, on the same network, and nothing said whether the time went to
    downloads, to writing files or to the first import. The record holds only the current
    step, so this samples it; a step shorter than the sampling interval can be missed.
    """

    def __init__(self, data_root: Path, key: str) -> None:
        self.record = data_root / "runtime" / f".{key}.progress.json"
        self.started = time.monotonic()
        self.marks: list[tuple[str, float]] = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def _watch(self) -> None:
        while not self.stopped.wait(0.02):
            try:
                record = json.loads(self.record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Not written yet, or caught halfway through the builder's replace.
                continue
            label = record.get("step") if record.get("state") == "building" else record.get("state")
            if label and (not self.marks or self.marks[-1][0] != label):
                self.marks.append((label, time.monotonic() - self.started))

    def report(self) -> None:
        self.stopped.set()
        self.thread.join()
        answered = time.monotonic() - self.started
        if self.marks:
            print(f"time {'before the build began':<24}{self.marks[0][1]:6.1f}s", flush=True)
        for (label, began), (_next, ended) in zip(self.marks, self.marks[1:]):
            print(f"time {label:<24}{ended - began:6.1f}s", flush=True)
        if self.marks and self.marks[-1][0] == "ready":
            print(f"time {'ready to first answer':<24}{answered - self.marks[-1][1]:6.1f}s", flush=True)
        print(f"time {'start to first answer':<24}{answered:6.1f}s", flush=True)


class Host:
    def __init__(self, plugin: Path, data_root: Path) -> None:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("MEMORYSAFE_")}
        environment["MEMORYSAFE_INSTALL_ROOT"] = str(data_root)
        self.process = subprocess.Popen(
            launch_command(plugin / "scripts"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=environment,
        )
        self.replies: queue.Queue = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.replies.put(json.loads(line))

    def _send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            reply = self.replies.get(timeout=max(0.01, deadline - time.monotonic()))
            if reply.get("id") == request_id:
                return reply

    def initialize(self) -> float:
        started = time.monotonic()
        reply = self.request(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "first-run", "version": "1"}},
            HANDSHAKE_SECONDS,
        )
        elapsed = time.monotonic() - started
        check(reply["result"]["serverInfo"]["name"] == "memorysafe", "the handshake names MemorySafe")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return elapsed

    def call_when_ready(self, name: str, arguments: dict) -> dict:
        deadline = time.monotonic() + SETUP_SECONDS
        while True:
            result = self.request("tools/call", {"name": name, "arguments": arguments}, 60).get("result", {})
            if result.get("isError") and "one-time setup" in json.dumps(result) and time.monotonic() < deadline:
                time.sleep(1)
                continue
            return result

    def close(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()
        self.process.wait(timeout=15)


def main(argv: list[str]) -> int:
    plugin = Path(argv[1]).resolve()
    data_root = Path(tempfile.mkdtemp(prefix="memorysafe-first-run-"))
    try:
        runtime_env = dict(
            line.split("=", 1)
            for line in (plugin / "scripts" / "runtime.env").read_text().splitlines()
            if "=" in line
        )
        key = runtime_env["RUNTIME_KEY"]

        clock = BuildClock(data_root, key)
        first = Host(plugin, data_root)
        try:
            elapsed = first.initialize()
            check(elapsed < HANDSHAKE_SECONDS, f"a first start answers the handshake in {elapsed:.2f}s")
            remembered = first.call_when_ready("memorysafe_remember", {"content": FACT, "category": "project"})
            clock.report()
            check(not remembered.get("isError"), f"remember succeeds once setup finishes: {json.dumps(remembered)[:300]}")
            found = first.call_when_ready("memorysafe_find", {"query": "first-run check"})
            check(FACT in json.dumps(found), "find returns what was remembered")
        finally:
            first.close()
        runtime = data_root / "runtime" / key
        check((runtime / "ready").is_file(), "the runtime was published under its lock key")
        # With the ready marker, this is the launcher's own fast-path test, checked before the
        # second start so that start is known to skip pending mode. The CLI shim cannot tell:
        # every start writes it.
        check(os.access(runtime_python(runtime), os.X_OK), "the second start will take the fast path")
        check(os.access(cli_shim(data_root), os.X_OK), "the first start wrote the CLI shim")
        if os.name == "nt":
            # The clean-machine risk this guards: WinGet declares Microsoft.VCRedist.2015+
            # as a dependency of uv, and nobody has confirmed whether the standalone zip
            # ensure_uv.cmd downloads needs it too. If it does, uv.exe fails to start on a
            # bare runner with no VCRedist installed -- this assertion says so plainly,
            # instead of leaving it to surface as a confusing downstream failure.
            uv_exe = data_root / "tools" / f"uv-{runtime_env['UV_VERSION']}" / "uv.exe"
            check(
                uv_exe.is_file(),
                f"uv.exe was installed and ran to build the runtime ({uv_exe}) -- if this "
                "fails, uv.exe could not start on this bare runner, which points at a "
                "missing VCRedist 2015+ rather than anything in MemorySafe's own code",
            )

        second = Host(plugin, data_root)
        try:
            elapsed = second.initialize()
            check(elapsed < HANDSHAKE_SECONDS, f"a second start answers the handshake in {elapsed:.2f}s")
            found = second.request("tools/call", {"name": "memorysafe_find", "arguments": {"query": "first-run check"}}, 60)
            check(FACT in json.dumps(found) and not found["result"].get("isError"), "a second start serves memories at once")
        finally:
            second.close()
        # Only on success: a failed run keeps its data root, install log included, to inspect.
        shutil.rmtree(data_root, ignore_errors=True)
        return 0
    except BaseException:
        # check() fails via SystemExit, so this must catch BaseException, not just
        # Exception, to reach every failure path -- and then re-raise unchanged so the
        # exit code and message stay exactly what they were before this diagnostic.
        dump_diagnostics(plugin, data_root)
        raise


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
