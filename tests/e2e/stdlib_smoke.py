#!/usr/bin/env python3
"""Run the Python 3.8 files on Python 3.8, the way the launcher runs them.

The unit tests import the full package, which needs 3.10 or later, so they cannot prove
these files still run on the python3 an older Mac or Linux machine already has. CI runs:

    uv run --no-project --python 3.8 tests/e2e/stdlib_smoke.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugin" / "scripts"
FAKE_CHILD = """
import json, sys
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}}), flush=True)
    elif message.get("method") == "tools/call":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
            "result": {"content": [{"type": "text", "text": "proxied"}]}}), flush=True)
"""


def check(condition, message):
    if not condition:
        raise SystemExit("FAIL: " + message)
    print("ok   " + message)


def talk(command, messages, environment, wait):
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=environment,
    )
    for message in messages:
        process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    time.sleep(wait)
    output, _errors = process.communicate(timeout=30)
    return {reply.get("id"): reply for reply in (json.loads(line) for line in output.splitlines() if line.strip())}


def handshake_then_call():
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memorysafe_find", "arguments": {}}},
    ]


def main():
    check(sys.version_info[:2] == (3, 8), "running on Python 3.8 (got %d.%d)" % sys.version_info[:2])
    work = Path(tempfile.mkdtemp(prefix="memorysafe-smoke-"))
    base = dict(os.environ, PYTHONPATH=str(ROOT / "src"), MEMORYSAFE_INSTALL_ROOT=str(work))
    child = work / "child.py"
    child.write_text(FAKE_CHILD)

    for exit_code, expectation in ((0, "proxied"), (3, "uv_checksum_mismatch")):
        environment = dict(
            base,
            MEMORYSAFE_RUNTIME_PENDING="1",
            MEMORYSAFE_RUNTIME_BUILD_COMMAND=json.dumps([sys.executable, "-c", "import sys; sys.exit(%d)" % exit_code]),
            MEMORYSAFE_BOOTSTRAP_CHILD_COMMAND=json.dumps([sys.executable, str(child)]),
        )
        replies = talk([sys.executable, str(SCRIPTS / "bootstrap_server.py")], handshake_then_call(), environment, 3)
        check(replies[1]["result"]["serverInfo"]["name"] == "memorysafe", "pending proxy answers initialize (build exit %d)" % exit_code)
        check(expectation in json.dumps(replies[2]), "pending proxy then serves %s" % expectation)

    replies = talk(
        [sys.executable, str(SCRIPTS / "degraded_server.py")],
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
        dict(base, MEMORYSAFE_DEGRADED_REASON="download_failed"),
        0,
    )
    check(replies[1]["result"]["tools"][0]["name"] == "memorysafe_doctor", "limited-mode server lists its tool")

    database = work / "data" / "memorysafe.sqlite3"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(str(database))
    connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
    connection.execute("INSERT INTO settings VALUES ('automatic_mode', 'enabled', 'now')")
    connection.commit()
    connection.close()
    hook = subprocess.run(
        [sys.executable, str(SCRIPTS / "capture_hook.py")],
        input=json.dumps({"prompt": "I prefer concise answers from now on."}),
        stdout=subprocess.PIPE,
        universal_newlines=True,
        env=dict(base, MEMORYSAFE_DB_PATH=str(database)),
        timeout=30,
    )
    check(hook.returncode == 0 and "memorysafe_auto_capture" in hook.stdout, "capture hook hints when automatic mode is on")

    plugin = work / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    (plugin / "scripts" / "runtime.env").write_text("RUNTIME_KEY=smoke\nPYTHON_VERSION=3.12\nUV_VERSION=0.12.15\n")
    (plugin / "scripts" / "ensure_uv").write_text("#!/bin/sh\nexit 2\n")
    (plugin / "requirements.lock").write_text("")
    build = subprocess.run(
        [sys.executable, str(SCRIPTS / "install_runtime.py"), "--plugin-dir", str(plugin), "--data-root", str(work / "root")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
    )
    check(build.returncode == 2, "uv builder reports a download failure as exit 2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
