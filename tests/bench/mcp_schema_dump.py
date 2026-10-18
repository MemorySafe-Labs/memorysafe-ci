"""Round 14 (reviewer B: "the declared MCP output schemas do not list these fields"). Prints, as JSON, the
input/output schema of every MemorySafe MCP tool exactly as a host sees it from tools/list, for the flags in
the environment. Run it twice and compare:

  python tests/bench/mcp_schema_dump.py > off.json
  MEMORYSAFE_GATE_CAPACITY=1 MEMORYSAFE_GATE_USEFUL=1 MEMORYSAFE_GATE_EXTINCTION=1 MEMORYSAFE_GATE_STALE=1 \\
      python tests/bench/mcp_schema_dump.py > on.json

The server module decides its result models when it is imported, so the flags must be set BEFORE the
server process starts (a host that started the server with the flags off sees the 0.4.19 schemas).
Uses a temporary state directory; never opens a live store, never binds a port."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.setdefault("MEMORYSAFE_STATE_DIR", tmp)
        os.environ.setdefault("MEMORYSAFE_DB_PATH", os.path.join(tmp, "m.sqlite3"))
        from memorysafe_chatgpt import server

        tools = asyncio.run(server.server.list_tools())
        out = {}
        for tool in tools:
            data = tool.model_dump(by_alias=True, exclude_none=True) if hasattr(tool, "model_dump") else dict(tool)
            out[data["name"]] = {"inputSchema": data.get("inputSchema"), "outputSchema": data.get("outputSchema")}
        flags = sorted(k for k, v in os.environ.items() if k.startswith("MEMORYSAFE_GATE") and v not in ("", "0"))
        json.dump({"python": sys.version.split()[0], "gate_flags": flags, "tools": out}, sys.stdout, indent=1, sort_keys=True)
        print()


if __name__ == "__main__":
    main()
