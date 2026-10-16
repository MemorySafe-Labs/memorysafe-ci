from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client


class StdioTransportTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(os.name == "nt", "spawns scripts/start_memorysafe.sh directly; that is the "
                                       "ChatGPT/Codex-on-macOS dev connector launcher at the repo root "
                                       "(still live, distinct from the retired plugin .cmd), a #!/bin/sh "
                                       "script that assumes a POSIX .venv/bin/python and has no Windows "
                                       "counterpart -- the shipped plugin's stdio transport is exercised "
                                       "on Windows by tests/e2e/first_run.py through plugin/scripts/"
                                       "start.cmd instead")
    async def test_real_subprocess_initializes_and_serves_tools(self) -> None:
        plugin_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            parameters = StdioServerParameters(
                command=str(plugin_root / "scripts" / "start_memorysafe.sh"),
                env={"MEMORYSAFE_DB_PATH": str(Path(temp_dir) / "stdio.sqlite3")},
                cwd=plugin_root,
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.server_info.name, "memorysafe")

                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 12)
                    self.assertIn("memorysafe_protect", {tool.name for tool in tools.tools})

                    resources = await session.list_resources()
                    self.assertEqual(len(resources.resources), 1)
                    self.assertEqual(
                        str(resources.resources[0].uri),
                        "ui://memorysafe/dashboard-v3.html",
                    )

                    result = await session.call_tool("memorysafe_health", {})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content["active_memories"], 0)
                    self.assertFalse(result.structured_content["automatic_mode"])

                    enabled = await session.call_tool(
                        "memorysafe_set_auto_mode", {"enabled": True}
                    )
                    self.assertFalse(enabled.is_error)
                    self.assertTrue(enabled.structured_content["enabled"])

                    dashboard = await session.call_tool("memorysafe_health", {"open": False})
                    self.assertFalse(dashboard.is_error)
                    self.assertEqual(dashboard.structured_content["status"], "healthy")

                    captured = await session.call_tool(
                        "memorysafe_auto_capture",
                        {
                "facts": ["I prefer project updates in English."],
                "category": "preference",
            },
                    )
                    self.assertFalse(captured.is_error)
                    self.assertEqual(captured.structured_content["saved"], 1)

                    verified = await session.call_tool("memorysafe_health", {})
                    self.assertFalse(verified.is_error)
                    self.assertEqual(verified.structured_content["active_memories"], 1)
                    self.assertEqual(verified.structured_content["automatic_captures"], 1)
                    self.assertEqual(
                        verified.structured_content["automatic_candidates_evaluated"], 1
                    )
                    self.assertIn(
                        "evaluated 1 candidate",
                        verified.structured_content["automatic_status"],
                    )
