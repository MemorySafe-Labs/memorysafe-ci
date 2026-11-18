"""How use is recorded on a memory, and the one number computed from it."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import retention, storage
from memorysafe_chatgpt.storage import MemoryStore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def days_ago(count: float) -> str:
    return (NOW - timedelta(days=count)).isoformat(timespec="seconds")


class ActivationTests(unittest.TestCase):
    def test_an_unused_memory_fades_with_age_and_never_reaches_zero_use(self) -> None:
        values = [retention.activation(days_ago(age), None, 1, NOW) for age in (1, 7, 30, 180, 720)]
        self.assertEqual(values, sorted(values, reverse=True))
        # A power law, not an exponential: two years old is lower than six months old,
        # by less than six months old is lower than one month old.
        self.assertLess(values[3] - values[4], values[2] - values[3])

    def test_use_outweighs_age(self) -> None:
        old_and_used = retention.activation(days_ago(180), days_ago(2), 10, NOW)
        new_and_unused = retention.activation(days_ago(20), None, 1, NOW)
        self.assertGreater(old_and_used, new_and_unused)

    def test_more_uses_and_more_recent_use_both_raise_it(self) -> None:
        base = retention.activation(days_ago(90), days_ago(30), 3, NOW)
        self.assertGreater(retention.activation(days_ago(90), days_ago(30), 6, NOW), base)
        self.assertGreater(retention.activation(days_ago(90), days_ago(3), 3, NOW), base)

    def test_a_use_this_minute_and_a_use_this_hour_are_the_same(self) -> None:
        minute = retention.activation(days_ago(30), (NOW - timedelta(minutes=1)).isoformat(), 2, NOW)
        hour = retention.activation(days_ago(30), (NOW - timedelta(minutes=59)).isoformat(), 2, NOW)
        self.assertEqual(minute, hour)

    def test_unreadable_and_future_dates_do_not_raise(self) -> None:
        for first, last in (("not a date", None), (None, None), (days_ago(-5), days_ago(-9)), ("2026-01-01T00:00:00", None)):
            self.assertIsInstance(retention.activation(first, last, 3, NOW), float)

    def test_a_row_from_before_the_use_columns_reads_as_never_used_since_saved(self) -> None:
        row = {"created_at": days_ago(40), "importance": 0.85}
        self.assertEqual(retention.uses_of(row), 1)
        self.assertEqual(retention.row_activation(row, NOW), retention.activation(days_ago(40), None, 1, NOW))

    def test_kind_moves_the_rank_and_use_can_overtake_it(self) -> None:
        safety = {"created_at": days_ago(60), "importance": 0.95}
        note = {"created_at": days_ago(60), "importance": 0.60}
        self.assertGreater(retention.retention(safety, NOW), retention.retention(note, NOW))
        used_note = {**note, "recall_count": 12, "last_used_at": days_ago(1)}
        self.assertGreater(retention.retention(used_note, NOW), retention.retention(safety, NOW))


class UseIsRecordedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MemoryStore(Path(self.temporary.name) / "memorysafe.sqlite3")

    def row(self, memory_id: str):
        with self.store._session() as connection:
            return connection.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()

    def test_a_new_memory_has_no_use_yet(self) -> None:
        memory_id = self.store.remember("The harbour survey is scheduled for spring", "project")["memory_id"]
        row = self.row(memory_id)
        self.assertIsNone(row["last_used_at"])
        self.assertEqual((row["recall_count"], row["restated_count"]), (0, 0))

    def test_a_recall_stamps_when(self) -> None:
        memory_id = self.store.remember("The harbour survey is scheduled for spring", "project")["memory_id"]
        with patch.object(storage, "_now", lambda: "2027-01-05T09:00:00+00:00"):
            self.store.find("harbour survey", 5)
        row = self.row(memory_id)
        self.assertEqual((row["recall_count"], row["last_used_at"]), (1, "2027-01-05T09:00:00+00:00"))

    def test_browsing_without_recording_is_not_a_use(self) -> None:
        memory_id = self.store.remember("The harbour survey is scheduled for spring", "project")["memory_id"]
        self.store.find("harbour survey", 5, record=False)
        self.assertIsNone(self.row(memory_id)["last_used_at"])

    def test_saying_it_again_is_a_use(self) -> None:
        memory_id = self.store.remember("Casey prefers straight talk over cheerleading", "preference")["memory_id"]
        with patch.object(storage, "_now", lambda: "2027-01-05T09:00:00+00:00"):
            again = self.store.remember("Casey prefers straight talk over cheerleading", "preference")
            near = self.store.remember("Casey prefers straight talk over cheerleading.", "preference")
        self.assertEqual((again["decision"], near["decision"]), ("MERGE", "MERGE"))
        row = self.row(memory_id)
        self.assertEqual((row["restated_count"], row["last_used_at"]), (2, "2027-01-05T09:00:00+00:00"))

    def test_an_older_store_gains_the_columns_on_open(self) -> None:
        path = Path(self.temporary.name) / "old.sqlite3"
        import sqlite3
        connection = sqlite3.connect(path)
        connection.executescript(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, normalized_content TEXT NOT NULL, "
            "category TEXT NOT NULL, importance REAL NOT NULL, confidence REAL NOT NULL, protected INTEGER NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'active', source TEXT NOT NULL DEFAULT 'chatgpt', created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, deleted_at TEXT, recall_count INTEGER NOT NULL DEFAULT 0);"
            "INSERT INTO memories VALUES ('MS-OLD', 'Old fact about tides', 'old fact about tides', 'project', 0.75, "
            "0.95, 0, 'active', 'manual', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', NULL, 4);"
        )
        connection.commit()
        connection.close()
        store = MemoryStore(path)
        with store._session() as opened:
            row = opened.execute("SELECT * FROM memories WHERE id = 'MS-OLD'").fetchone()
        self.assertEqual((row["recall_count"], row["restated_count"], row["last_used_at"]), (4, 0, None))
        self.assertEqual(len(store.find("tides", 5)), 1)


if __name__ == "__main__":
    unittest.main()
