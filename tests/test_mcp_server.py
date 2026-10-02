from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from memorysafe_chatgpt import server as connector


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["MEMORYSAFE_DB_PATH"] = str(Path(self.temp_dir.name) / "mcp.sqlite3")
        connector._stores.clear()

    async def asyncTearDown(self) -> None:
        connector._stores.clear()
        os.environ.pop("MEMORYSAFE_DB_PATH", None)
        self.temp_dir.cleanup()

    async def test_tools_are_advertised_with_safety_annotations(self) -> None:
        tools = {tool.name: tool for tool in await connector.server.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "memorysafe_remember",
                "memorysafe_set_auto_mode",
                "memorysafe_auto_capture",
                "memorysafe_find",
                "memorysafe_forget",
                "memorysafe_protect",
                "memorysafe_explain",
                "memorysafe_review_conflicts",
                "memorysafe_resolve_conflict",
                "memorysafe_restore",
                "memorysafe_health",
                "memorysafe_doctor",
            },
        )
        self.assertTrue(tools["memorysafe_find"].annotations.read_only_hint)
        # Diagnosis must never be able to change the install it is inspecting.
        self.assertTrue(tools["memorysafe_doctor"].annotations.read_only_hint)
        self.assertTrue(tools["memorysafe_health"].annotations.read_only_hint)
        self.assertFalse(tools["memorysafe_auto_capture"].annotations.read_only_hint)
        # The URI carries a version so the client reloads the component instead of
        # serving a cached one. Bump it in dashboard.py whenever the page changes, and
        # here in the same commit - a stale constant here goes red for a reason that
        # has nothing to do with the change that broke it.
        self.assertEqual(
            tools["memorysafe_health"].meta["ui"]["resourceUri"],
            "ui://memorysafe/dashboard-v3.html",
        )
        self.assertEqual(
            tools["memorysafe_health"].meta["openai/outputTemplate"],
            "ui://memorysafe/dashboard-v3.html",
        )
        self.assertTrue(tools["memorysafe_forget"].annotations.destructive_hint)
        self.assertFalse(tools["memorysafe_remember"].annotations.open_world_hint)

        resources = await connector.server.list_resources()
        self.assertEqual(len(resources), 1)
        self.assertEqual(str(resources[0].uri), "ui://memorysafe/dashboard-v3.html")
        self.assertEqual(resources[0].mime_type, "text/html;profile=mcp-app")

    async def test_tools_can_be_called_through_mcp(self) -> None:
        remembered = await connector.server.call_tool(
            "memorysafe_remember",
            {
                "content": "My preferred editor is VS Code.",
                "category": "preference",
            },
        )
        self.assertFalse(remembered.is_error)

        found = await connector.server.call_tool(
            "memorysafe_find", {"query": "preferred editor", "limit": 5}
        )
        self.assertFalse(found.is_error)
        self.assertEqual(found.structured_content["count"], 1)

        skipped_off = await connector.server.call_tool(
            "memorysafe_auto_capture",
            {
                "facts": ["I prefer afternoon meetings."],
                "category": "preference",
            },
        )
        self.assertFalse(skipped_off.is_error)
        self.assertEqual(skipped_off.structured_content["saved"], 0)

        enabled = await connector.server.call_tool(
            "memorysafe_set_auto_mode", {"enabled": True}
        )
        self.assertFalse(enabled.is_error)
        self.assertTrue(enabled.structured_content["enabled"])

        captured = await connector.server.call_tool(
            "memorysafe_auto_capture",
            {
                "facts": ["I prefer afternoon meetings."],
                "category": "preference",
            },
        )
        self.assertFalse(captured.is_error)
        self.assertEqual(captured.structured_content["saved"], 1)

        for payload in ("SIN 046 454 286", "call me at 514-779-6472"):
            # The word list caught "social insurance number" but not a bare SIN, which
            # is how anyone actually writes one.
            leak = await connector.server.call_tool(
                "memorysafe_auto_capture", {"facts": [payload], "category": "other"}
            )
            self.assertEqual(leak.structured_content["saved"], 0, payload)

        skipped_sensitive = await connector.server.call_tool(
            "memorysafe_auto_capture",
            {
                "facts": ["My API key is sk-this-should-never-be-saved-12345."],
                "category": "other",
            },
        )
        self.assertFalse(skipped_sensitive.is_error)
        self.assertEqual(skipped_sensitive.structured_content["saved"], 0)

        dashboard = await connector.server.call_tool("memorysafe_health", {"open": False})
        self.assertFalse(dashboard.is_error)
        self.assertEqual(dashboard.structured_content["active_memories"], 2)
        self.assertEqual(len(dashboard.structured_content["recent_memories"]), 2)
        self.assertTrue(dashboard.structured_content["automatic_mode"])
        self.assertEqual(dashboard.structured_content["automatic_captures"], 1)
        self.assertEqual(dashboard.structured_content["automatic_skips"], 4)
        self.assertEqual(dashboard.structured_content["automatic_candidates_evaluated"], 5)
        self.assertIn("evaluated 5 candidates", dashboard.structured_content["automatic_status"])
        self.assertEqual(dashboard.structured_content["automatic_last_decision"], "SKIP_AUTO")

    async def test_health_carries_recall_to_the_assistant(self) -> None:
        """Recall telemetry has to survive the tool result, not just the dashboard.

        DashboardResult did not declare `recall`, so pydantic dropped it: an assistant
        asked whether MemorySafe was helping and received nothing, while the dashboard
        looked correct because it reads the raw payload.
        """

        await connector.server.call_tool(
            "memorysafe_remember",
            {"content": "Primo-adoptants closes 4 November 2026", "category": "project"},
        )
        await connector.server.call_tool(
            "memorysafe_find", {"query": "when does primo close", "limit": 3}
        )
        health = await connector.server.call_tool("memorysafe_health", {})
        recall = health.structured_content.get("recall")
        self.assertIsNotNone(recall, "recall was dropped from the tool result")
        self.assertGreaterEqual(recall["times_memory_was_consulted"], 1)

    async def test_instructions_tell_the_agent_how_to_finish_setup(self) -> None:
        """Testers ask the connected agent; it has to already know the first steps."""

        text = connector.server.instructions or ""
        self.assertIn("memorysafe_health", text)
        self.assertIn("127.0.0.1:8765/dashboard", text)
        self.assertIn(".mcpb", text)
        tools = {tool.name: tool for tool in await connector.server.list_tools()}
        self.assertIn("set it up", tools["memorysafe_health"].description.lower())


if __name__ == "__main__":
    unittest.main()
