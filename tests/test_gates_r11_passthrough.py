"""Round 11: the round-10 "possibly stale" flag reaches hosts (MCP find, `memorysafe find --json`
and the plain CLI listing) when MEMORYSAFE_GATE_STALE is on, and nothing changes when it is off.

The MCP result models are chosen when the server module is imported, so each case runs in a
fresh interpreter with the environment it needs."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

SCRIPT = r'''
import asyncio, json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from memorysafe_chatgpt import storage
now = [datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)]
storage._now = lambda: now[0].isoformat(timespec="seconds")
store = storage.MemoryStore(Path(os.environ["MEMORYSAFE_DB_PATH"]))
store.remember("The Quillfeather contract is still unsigned as of today", "project")
now[0] += timedelta(days=45)
store.remember("Lunch note: the soup at the food hall was fine", "other")
from memorysafe_chatgpt import server, cli
server._stores.clear()
found = asyncio.run(server.server.call_tool("memorysafe_find", {"query": "Quillfeather contract signed", "limit": 3}))
tools = {t.name: t for t in asyncio.run(server.server.list_tools())}
schema = tools["memorysafe_find"].output_schema or tools["memorysafe_find"].outputSchema
direct = store.find("Quillfeather contract signed", 3, record=False)
print(json.dumps({"mcp": found.structured_content, "schema": json.dumps(schema, sort_keys=True), "direct": direct}))
'''


def _run(extra_env: dict, code: str = SCRIPT, args: list | None = None) -> str:
    temp = tempfile.mkdtemp()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
    env.update({"MEMORYSAFE_DB_PATH": str(Path(temp) / "m.sqlite3"), "MEMORYSAFE_STATE_DIR": str(Path(temp) / "s"),
                "PYTHONPATH": SRC, "HOME": temp, **extra_env})
    out = subprocess.run([sys.executable, "-c", code, *(args or [])], env=env, capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise AssertionError(out.stderr[-3000:])
    return out.stdout


class PossiblyStalePassthroughTests(unittest.TestCase):
    def test_on_mcp_find_carries_possibly_stale(self):
        data = json.loads(_run({"MEMORYSAFE_GATE_STALE": "1"}).strip().splitlines()[-1])
        hit = data["mcp"]["memories"][0]
        self.assertIn("Quillfeather", hit["content"])
        self.assertIs(hit["possibly_stale"], True)
        self.assertIn("possibly_stale", data["schema"])
        self.assertIs(data["direct"][0]["possibly_stale"], True)

    def test_off_mcp_find_and_schema_unchanged(self):
        data = json.loads(_run({}).strip().splitlines()[-1])
        hit = data["mcp"]["memories"][0]
        self.assertNotIn("possibly_stale", hit)
        self.assertNotIn("possibly_stale", data["schema"])
        self.assertNotIn("possibly_stale", data["direct"][0])

    def _cli(self, extra_env: dict, *argv: str) -> str:
        code = SCRIPT.split("from memorysafe_chatgpt import server, cli")[0] + (
            "from memorysafe_chatgpt import cli\n"
            "sys.argv=['memorysafe', *sys.argv[1:]]\n"
            "cli.main()\n")
        return _run(extra_env, code, list(argv))

    def test_cli_json_and_text(self):
        on_json = json.loads(self._cli({"MEMORYSAFE_GATE_STALE": "1"}, "find", "Quillfeather contract signed", "--json"))
        self.assertIs(on_json["memories"][0]["possibly_stale"], True)
        off_json = json.loads(self._cli({}, "find", "Quillfeather contract signed", "--json"))
        self.assertNotIn("possibly_stale", off_json["memories"][0])
        self.assertIn("POSSIBLY STALE", self._cli({"MEMORYSAFE_GATE_STALE": "1"}, "find", "Quillfeather contract signed"))
        self.assertNotIn("POSSIBLY STALE", self._cli({}, "find", "Quillfeather contract signed"))


if __name__ == "__main__":
    unittest.main()
