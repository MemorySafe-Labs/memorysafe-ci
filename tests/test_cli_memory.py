"""find and remember over the command line.

These exist because MCP over stdio is not reachable from every host. One chat assistant runs its
MCP servers inside its own container, where this machine's paths do not exist, so the
only interface left is a command. Anything that regresses here puts those hosts back to
having no supported way to read or write a memory.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt.cli import main as cli_main
from memorysafe_chatgpt.storage import MemoryStore


class CommandLineMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "MemorySafe"
        self.database = self.root / "data" / "memorysafe.sqlite3"
        self.database.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, *arguments: str) -> str:
        output = StringIO()
        argv = ["memorysafe", "--install-root", str(self.root), *arguments]
        with patch("sys.argv", argv), redirect_stdout(output):
            cli_main()
        return output.getvalue()

    def test_remember_then_find_round_trips(self) -> None:
        self._run("remember", "Primo-adoptants closes 4 November 2026", "--category", "project")
        payload = json.loads(self._run("find", "primo-adoptants deadline", "--json"))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["memories"][0]["content"], "Primo-adoptants closes 4 November 2026"
        )

    def test_written_memory_is_the_same_store_the_server_reads(self) -> None:
        # The whole point is one file. A CLI that wrote somewhere else would look
        # correct in both directions and still be useless.
        self._run("remember", "The store is shared", "--category", "project")
        matches = MemoryStore(self.database).find("the store is shared", 5)
        self.assertEqual(len(matches), 1)

    def test_install_root_resolves_the_same_database_as_the_server(self) -> None:
        self._run("remember", "Resolved by install root", "--category", "project")
        self.assertTrue(self.database.exists())

    def test_agent_written_memories_score_lower_confidence_than_asked_for_ones(self) -> None:
        asked = json.loads(self._run("remember", "Dana asked for this", "--json"))
        volunteered = json.loads(
            self._run("remember", "An agent volunteered this", "--json", "--source", "grok-bot")
        )
        self.assertGreater(asked["confidence"], volunteered["confidence"])

    def test_supersession_is_reported_and_not_silent(self) -> None:
        # A caller who is told STORE, when what actually happened is that their earlier
        # memory stopped being active, has been misinformed by us.
        self._run("remember", "The Gama deadline is 28 September", "--category", "project")
        second = json.loads(
            self._run("remember", "The Gama deadline is 26 September", "--category", "project", "--json")
        )
        self.assertIn("lifecycle", second)
        self.assertNotEqual(second.get("lifecycle"), "none")
        human = self._run("find", "gama deadline")
        self.assertIn("MS-", human)

    def test_json_output_carries_every_key_storage_returned(self) -> None:
        # Three data-loss bugs in this codebase came from a result model quietly
        # dropping keys it had not been taught. The CLI prints storage's own dict.
        store = MemoryStore(self.database)
        expected = set(store.remember("Direct write for key comparison", "project").keys())
        printed = set(json.loads(self._run("remember", "Printed write for key comparison", "--json")))
        self.assertEqual(expected - printed, set())

    def test_find_with_no_match_says_so_without_failing(self) -> None:
        output = self._run("find", "nothing was ever stored about this")
        self.assertIn("Nothing stored matches", output)

    def test_find_respects_limit(self) -> None:
        for index in range(4):
            self._run("remember", f"Tester number {index} filed a report", "--category", "project")
        payload = json.loads(self._run("find", "tester report", "--limit", "2", "--json"))
        self.assertEqual(payload["count"], 2)

    def test_category_is_validated_rather_than_stored_as_typed(self) -> None:
        with self.assertRaises(SystemExit):
            self._run("remember", "A fact", "--category", "porject")

    def test_empty_content_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._run("remember", "   ")


if __name__ == "__main__":
    unittest.main()
