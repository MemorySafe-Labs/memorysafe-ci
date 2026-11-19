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

    def test_an_import_holds_the_store_open_once_and_leaves_it_closed(self):
        """Each save opens and closes the file. With nothing else holding it, every close
        folded the write-ahead log away and the next save made it again, which on Windows
        took a few hundred memories past the dashboard's request. The import keeps one
        connection of its own for its length, and no more than that after it."""
        import sqlite3

        log = self.dir / "target.sqlite3-wal"
        seen = []
        remember = self.target.remember

        def watched(*args, **kwargs):
            seen.append(log.exists())
            return remember(*args, **kwargs)

        opened, closed = [], []

        class Tracked(sqlite3.Connection):
            def close(self):
                closed.append(self)
                super().close()

        real = sqlite3.connect

        def connect(*args, **kwargs):
            connection = real(*args, factory=Tracked, **kwargs)
            opened.append(connection)
            return connection

        memories = [{"content": f"The pier inspection number {n} is booked", "category": "project"} for n in range(1, 13)]
        with mock.patch.object(self.target, "remember", watched), mock.patch.object(sqlite3, "connect", connect):
            result = transfer.import_memories(self.target, memories)
        self.assertEqual((result["stored"], result["failed"]), (12, 0))
        # From the second save on, the log the first one wrote is still there.
        self.assertEqual(seen[1:], [True] * 11)
        self.assertEqual(len(opened), len(closed))
        self.assertEqual({id(c) for c in opened}, {id(c) for c in closed})

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

    def test_search_words_and_use_survive_the_trip(self):
        """Use decides which memories a session carries and which of two equal matches comes
        first; a moved store used to arrive with every memory looking equally unused."""
        saved = self.source.remember("Save system uses JSON files in user://saves", "project",
                                     keywords=["savegame", "persistence"])
        self.later(30)
        used_at = self.clock.isoformat(timespec="seconds")
        self.source.find("save system", 5)
        self.source.find("save system JSON", 5)
        self.source.remember("Save system uses JSON files in user://saves", "project")
        self.later(10)
        transfer.import_memories(self.target, transfer.export_active(self.source)["memories"])
        hit = self.target.find("persistence", 5, record=False)
        self.assertEqual(len(hit), 1)
        detail = self.target.explain(hit[0]["memory_id"])
        self.assertEqual(detail["keywords"], ["savegame", "persistence"])
        self.assertEqual((detail["recall_count"], detail["restated_count"]), (2, 1))
        self.assertEqual(detail["last_used_at"], used_at)
        self.assertNotEqual(saved["memory_id"], hit[0]["memory_id"])

    def test_a_file_from_before_these_fields_imports_as_it_always_did(self):
        result = transfer.import_memories(self.target, [
            {"content": "The tide tables are in the blue binder", "category": "other",
             "created_at": "2026-03-01T00:00:00+00:00", "updated_at": "2026-03-01T00:00:00+00:00"},
            {"content": "The pump manual is on the top shelf", "category": "other",
             "keywords": "not a list", "recall_count": "many", "restated_count": -4, "last_used_at": "garbage"},
        ])
        self.assertEqual((result["stored"], result["failed"]), (2, 0))
        for hit in self.target.find("", 5, record=False):
            detail = self.target.explain(hit["memory_id"])
            self.assertEqual((detail["recall_count"], detail["restated_count"], detail["keywords"]), (0, 0, []))

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
