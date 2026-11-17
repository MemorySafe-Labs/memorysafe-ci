"""What a tool result costs: only what is so is sent, and the declared schema still holds."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import server
from memorysafe_chatgpt.token_metrics import count_text_tokens


class LeanResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        database = Path(self.temporary.name) / "memorysafe.sqlite3"
        environment = patch.dict(os.environ, {"MEMORYSAFE_DB_PATH": str(database)})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = server._store()

    def call(self, name: str, arguments: dict):
        return asyncio.run(server.server.call_tool(name, arguments))

    def test_a_found_memory_carries_its_text_kind_date_and_id_and_no_bookkeeping(self) -> None:
        self.store.remember("The harbour survey is scheduled for spring", "other")
        result = self.call("memorysafe_find", {"query": "harbour survey", "limit": 5})
        memory = result.structured_content["memories"][0]
        self.assertEqual(sorted(memory), ["category", "content", "memory_id", "updated_at"])
        text = json.loads(result.content[0].text)
        self.assertEqual(text, result.structured_content)

    def test_protection_and_an_open_conflict_still_travel(self) -> None:
        self.store.remember("The launch review is on 4 May 2027", "decision")
        self.store.remember("The launch review is on 9 June 2027", "decision")
        memories = self.call("memorysafe_find", {"query": "launch review", "limit": 5}).structured_content["memories"]
        self.assertTrue(all(memory["protected"] for memory in memories))
        self.assertTrue(all(memory["needs_review"] and memory["open_conflict_ids"] and memory["review_note"]
                            for memory in memories))

    def test_what_is_sent_validates_against_the_declared_output_schema(self) -> None:
        self.store.remember("The harbour survey is scheduled for spring", "other")
        result = self.call("memorysafe_find", {"query": "harbour survey", "limit": 5})
        tool = next(t for t in server.server._tool_manager.list_tools() if t.name == "memorysafe_find")
        required = tool.output_schema["$defs"]["FoundMemory"]["required"]
        self.assertEqual(sorted(required), ["category", "content", "memory_id", "updated_at"])
        again = server.FindResult.model_validate(result.structured_content)
        self.assertFalse(again.memories[0].protected)
        self.assertEqual(again.memories[0].open_conflict_ids, [])

    def test_a_result_costs_less_than_the_full_record_did(self) -> None:
        for index in range(5):
            self.store.remember(f"Harbour survey note {index}: the tide gauge at pier {index + 3} reads low in spring", "project")
        result = self.call("memorysafe_find", {"query": "harbour survey tide gauge", "limit": 5})
        lean = count_text_tokens(result.content[0].text)
        full = count_text_tokens(json.dumps(
            {"query": "harbour survey tide gauge", "count": 5,
             "memories": self.store.find("harbour survey tide gauge", 5, record=False)}, indent=2))
        self.assertLess(lean, 0.75 * full, (lean, full))

    def test_remember_says_what_happened_without_the_scores(self) -> None:
        saved = self.call("memorysafe_remember", {"content": "The tide tables are in the blue binder", "category": "other"})
        self.assertNotIn("importance", saved.structured_content)
        self.assertNotIn("confidence", saved.structured_content)
        self.assertNotIn("evicted_memory_ids", saved.structured_content)
        self.assertEqual(saved.structured_content["decision"], "STORE")
        self.assertEqual(server.RememberResult.model_validate(saved.structured_content).evicted_memory_ids, [])

    def test_the_health_tool_never_carries_a_whole_long_memory(self) -> None:
        """Described as counts, called first by every assistant, and it returned the latest
        memories, the governance trail and the most used ones in full: 5,448 tokens with one
        10,000-character memory in the store."""
        long_note = "The Quillfeather handbook says: " + ("tide tables and gauges, " * 400)
        self.store.remember(long_note, "project")
        self.store.remember("I live in Montreal", "personal")
        self.store.remember("I live in Ottawa", "personal")
        self.store.find("Quillfeather handbook", 5)
        result = self.call("memorysafe_health", {})
        data = result.structured_content
        texts = [memory["content"] for memory in data["recent_memories"]]
        texts += [event[key] for event in data["governance_events"] for key in ("replaced", "kept")]
        texts += [used["content"] for used in data["recall"]["most_used"]]
        self.assertTrue(texts)
        self.assertLessEqual(max(len(text) for text in texts), server.HEALTH_TEXT_CHARS)
        self.assertTrue(any(text.endswith("…") for text in texts))
        self.assertLess(count_text_tokens(result.content[0].text), 3200)
        # The store itself is untouched: a search still returns the whole memory.
        self.assertEqual(len(self.store.find("Quillfeather handbook", 5, record=False)[0]["content"]), len(long_note.strip()))


if __name__ == "__main__":
    unittest.main()
