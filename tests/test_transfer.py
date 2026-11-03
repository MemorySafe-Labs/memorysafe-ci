"""Export and import: moving memories between MemorySafe stores.

Before this, moving a store meant copying the database file, which carries every forgotten
memory and the whole audit trail with it. These tests pin what travels (active memories,
their dates and protection) and what does not (forgotten, superseded), and that the
receiving store governs what arrives instead of trusting it.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from memorysafe_chatgpt import cli, storage, transfer
from memorysafe_chatgpt.storage import MemoryStore


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clock = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
        patcher = mock.patch.object(storage, "_now", side_effect=lambda: self.clock.isoformat(timespec="seconds"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = MemoryStore(self.dir / "source.sqlite3")
        self.target = MemoryStore(self.dir / "target.sqlite3")

    def later(self, days):
        self.clock += timedelta(days=days)

    def active(self, store):
        with store._session() as connection:
            return {row["content"]: dict(row) for row in connection.execute(
                "SELECT content, category, protected, created_at, updated_at FROM memories WHERE state = 'active'")}

    def test_only_active_memories_travel(self):
        kept = self.source.remember("The Cedar permit is pending.", "project")
        gone = self.source.remember("My locker code is the old one.", "other")
        self.source.forget(gone["memory_id"], confirm=True)
        self.source.remember("Dana lives in Toronto", "personal")
        self.later(1)
        self.source.remember("Dana lives in Montreal now", "personal")  # supersedes Toronto
        exported = transfer.export_active(self.source)
        texts = [m["content"] for m in exported["memories"]]
        self.assertIn("The Cedar permit is pending.", texts)
        self.assertIn("Dana lives in Montreal now", texts)
        self.assertNotIn("My locker code is the old one.", texts)
        self.assertNotIn("Dana lives in Toronto", texts)
        self.assertEqual(exported["memories"][0]["id"], kept["memory_id"])

    def test_dates_and_protection_survive_the_trip(self):
        note = self.source.remember("The Cedar permit is pending.", "project")
        self.source.protect(note["memory_id"], True)
        march = self.active(self.source)["The Cedar permit is pending."]
        self.later(200)
        result = transfer.import_memories(self.target, transfer.export_active(self.source)["memories"])
        self.assertEqual(result["stored"], 1)
        arrived = self.active(self.target)["The Cedar permit is pending."]
        self.assertEqual(arrived["created_at"], march["created_at"])
        self.assertEqual(arrived["updated_at"], march["updated_at"])
        self.assertTrue(arrived["protected"])

    def test_duplicates_merge_into_what_is_already_there(self):
        self.target.remember("I prefer short emails.", "preference")
        self.source.remember("I prefer short emails.", "preference")
        self.source.remember("The Q4 deck is in Finance.", "project")
        result = transfer.import_memories(self.target, transfer.export_active(self.source)["memories"])
        self.assertEqual((result["stored"], result["merged"]), (1, 1))
        self.assertEqual(len(self.active(self.target)), 2)

    def test_the_receiving_store_decides_conflicts_and_says_so(self):
        records = [
            {"content": "Dana lives in Toronto", "category": "personal", "created_at": "2026-01-01T00:00:00+00:00"},
            {"content": "Dana lives in Montreal now", "category": "personal", "created_at": "2026-02-01T00:00:00+00:00"},
        ]
        # Given newest first on purpose: import runs oldest first, so the newer fact wins.
        result = transfer.import_memories(self.target, list(reversed(records)))
        self.assertEqual(result["replaced_older"], 1)
        self.assertIn("Dana lives in Montreal now", self.active(self.target))
        self.assertNotIn("Dana lives in Toronto", self.active(self.target))

    def test_bad_records_are_reported_by_position_without_their_text(self):
        records = [
            {"content": "Fine fact about the Orion budget.", "category": "project"},
            {"content": "!!! ???", "category": "nonsense-category"},
        ]
        result = transfer.import_memories(self.target, records)
        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"][0]["position"], 2)
        self.assertNotIn("???", json.dumps(result))

    def test_an_unknown_category_becomes_other(self):
        transfer.import_memories(self.target, [{"content": "Orion ships in May.", "category": "weird"}])
        self.assertEqual(self.active(self.target)["Orion ships in May."]["category"], "other")

    def test_future_or_garbage_dates_become_now(self):
        transfer.import_memories(self.target, [
            {"content": "Orion ships in May.", "category": "project", "created_at": "2099-01-01T00:00:00+00:00"},
            {"content": "Atlas ships in June.", "category": "project", "created_at": "yesterday-ish"},
        ])
        now = self.clock.isoformat(timespec="seconds")
        for text in ("Orion ships in May.", "Atlas ships in June."):
            self.assertEqual(self.active(self.target)[text]["created_at"], now)

    def test_the_file_is_owner_only_and_never_overwritten(self):
        self.source.remember("I prefer short emails.", "preference")
        target = self.dir / "out.json"
        written = transfer.write_export(self.source, target)
        self.assertEqual(written["count"], 1)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            transfer.write_export(self.source, target)
        self.assertEqual(len(transfer.read_export(target)), 1)

    def test_files_that_are_not_exports_are_refused(self):
        for data, message in (
            ({"memories": []}, "not a MemorySafe export"),
            ({"format": "memorysafe-export", "format_version": 99, "memories": []}, "format version 99"),
            ({"format": "memorysafe-export", "format_version": 1, "memories": [{"content": " "}]}, "Memory 1"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                transfer.parse_export(data)

    def test_the_commands_round_trip(self):
        self.source.remember("I prefer short emails.", "preference")
        out = self.dir / "cli.json"
        with mock.patch.object(cli, "_resolve_database", return_value=self.source.database_path), \
                mock.patch.object(sys, "argv", ["memorysafe", "export", "--output", str(out)]), \
                redirect_stdout(io.StringIO()) as printed:
            cli.main()
        self.assertIn("Exported 1 active memories", printed.getvalue())
        with mock.patch.object(cli, "_resolve_database", return_value=self.target.database_path), \
                mock.patch.object(sys, "argv", ["memorysafe", "import", str(out), "--json"]), \
                redirect_stdout(io.StringIO()) as printed:
            cli.main()
        self.assertEqual(json.loads(printed.getvalue())["stored"], 1)


if __name__ == "__main__":
    unittest.main()
