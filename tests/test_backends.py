"""The seam between the memory engine and wherever its rows live.

The hosted edition is only the same product if the engine that decides what to
merge, supersede and recall is the same code on every backend. These tests pin
the seam itself; the engine's behaviour is pinned by test_storage and
test_lifecycle, which run against both backends.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from memorysafe_chatgpt.backends import insert_returning_id
from memorysafe_chatgpt.backends.sqlite import SqliteBackend
from memorysafe_chatgpt.storage import MemoryStore


class SqliteBackendSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "seam.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_an_explicit_sqlite_backend_is_the_same_store_as_a_path(self) -> None:
        MemoryStore(self.path).remember("The office is in Longueuil.", "project")

        store = MemoryStore(backend=SqliteBackend(self.path))

        hits = store.find("office Longueuil", 5, record=False)
        self.assertEqual(hits[0]["content"], "The office is in Longueuil.")
        self.assertEqual(store.database_path, self.path)

    def test_a_store_needs_somewhere_to_live(self) -> None:
        with self.assertRaises(TypeError):
            MemoryStore()

    def test_health_still_names_the_file_it_read(self) -> None:
        store = MemoryStore(self.path)
        store.remember("Health reads this file.", "project")

        health = store.health()

        self.assertTrue(health["database_path"].endswith("seam.sqlite3"))
        self.assertGreater(health["database_bytes"], 0)

    def test_relation_ids_come_back_from_sqlite(self) -> None:
        store = MemoryStore(self.path)
        store.remember("I live in Montreal", "personal")

        moved = store.remember("I live in Toronto", "personal")

        self.assertEqual(moved["lifecycle"], "superseded_previous")
        self.assertIsInstance(moved["conflict_id"], int)

    def test_the_sqlite_schema_has_every_index_the_engine_relies_on(self) -> None:
        """The schema moved out of storage.py in M1, and main added an index to the old
        copy afterwards. A merge that takes the moved schema drops it without a conflict
        marker anywhere near, and nothing fails: writes only get slower as a store grows."""

        MemoryStore(self.path)

        with closing(sqlite3.connect(self.path)) as connection:
            indexes = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
        self.assertLessEqual(
            {
                "memories_active_category",
                "memories_normalized",
                "memories_recent_by_category",
                "memories_recent_active",
                "decision_events_decision",
                "memory_relations_status",
                "memory_relations_old",
            },
            indexes,
        )

    def test_a_session_connection_carries_the_pragmas_that_make_sharing_safe(self) -> None:
        """Claude, Codex and ChatGPT write one file at the same time. WAL lets readers
        and a writer coexist, busy_timeout makes contention a wait instead of "database
        is locked", wal_autocheckpoint keeps the main file a truthful copy of the store,
        and foreign_keys is off by default in SQLite. Nothing else would notice if an
        edit to the backend dropped one: the store would work, until two assistants wrote."""

        backend = SqliteBackend(self.path)

        with backend.session() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            autocheckpoint = connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]

        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(busy_timeout, 30000)
        self.assertEqual(autocheckpoint, 200)
        self.assertEqual(foreign_keys, 1)

    def test_a_store_made_before_a_column_existed_is_migrated_in_place(self) -> None:
        """There is no migration framework: initialize() adds columns with ALTER TABLE
        after a PRAGMA table_info check. A user's file from before recall_count and
        decision_events.source must open without losing a row, start the new columns at
        their honest defaults, and survive being opened again by the next assistant."""

        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE memories (
                        id TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        normalized_content TEXT NOT NULL,
                        category TEXT NOT NULL,
                        importance REAL NOT NULL,
                        confidence REAL NOT NULL,
                        protected INTEGER NOT NULL,
                        state TEXT NOT NULL DEFAULT 'active',
                        source TEXT NOT NULL DEFAULT 'chatgpt',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        deleted_at TEXT
                    );
                    CREATE TABLE decision_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision TEXT NOT NULL,
                        memory_id TEXT,
                        reason TEXT NOT NULL,
                        content_bytes INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    );
                    INSERT INTO memories VALUES
                        ('m1', 'Old fact.', 'old fact.', 'project', 0.5, 0.6, 0,
                         'active', 'claude', '2026-01-01T00:00:00+00:00',
                         '2026-01-02T00:00:00+00:00', NULL);
                    INSERT INTO decision_events
                        (decision, memory_id, reason, content_bytes, created_at)
                        VALUES ('STORE', 'm1', 'old reason', 9, '2026-01-01T00:00:00+00:00');
                    """
                )

        def read() -> tuple[list[str], list[str], tuple, tuple]:
            with closing(sqlite3.connect(self.path)) as connection:
                memory_columns = [r[1] for r in connection.execute("PRAGMA table_info(memories)")]
                event_columns = [
                    r[1] for r in connection.execute("PRAGMA table_info(decision_events)")
                ]
                memory = connection.execute(
                    "SELECT id, content, category, source, recall_count FROM memories"
                ).fetchone()
                event = connection.execute(
                    "SELECT decision, memory_id, reason, content_bytes, source FROM decision_events"
                ).fetchone()
            return memory_columns, event_columns, memory, event

        MemoryStore(self.path)
        memory_columns, event_columns, memory, event = read()

        self.assertIn("recall_count", memory_columns)
        self.assertIn("source", event_columns)
        self.assertEqual(memory, ("m1", "Old fact.", "project", "claude", 0))
        self.assertEqual(event, ("STORE", "m1", "old reason", 9, "manual"))

        SqliteBackend(self.path).initialize()
        self.assertEqual(read(), (memory_columns, event_columns, memory, event))

    def test_snapshot_keeps_only_the_newest_few_and_they_open(self) -> None:
        """One bad write used to be total loss, so the store takes consistent copies, but
        snapshots that accumulate for ever are their own problem. The retained set must
        be the newest ones (names are microsecond timestamps), and the newest must be a
        database someone could actually restore from."""

        store = MemoryStore(self.path)
        store.remember("Snapshots must hold this memory.", "project")

        taken = [store.snapshot(keep=2) for _ in range(4)]

        self.assertTrue(all(path is not None for path in taken))
        remaining = sorted((self.path.parent / "snapshots").glob("memorysafe-*.sqlite3"))
        self.assertEqual(remaining, sorted(taken)[-2:])
        with closing(sqlite3.connect(remaining[-1])) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            contents = [r[0] for r in connection.execute("SELECT content FROM memories")]
        self.assertEqual(contents, ["Snapshots must hold this memory."])

    def test_checkpoint_folds_the_log_back_into_the_main_file(self) -> None:
        """Until the write-ahead log is folded in, the newest memories live only in the
        -wal sidecar, and copying the main file alone brings back an older store without
        saying so. checkpoint() is what doctor and migrate call before copying."""

        store = MemoryStore(self.path)
        store.remember("First fact for the log.", "project")
        store.remember("Second fact for the log.", "project")

        result = store.checkpoint()

        self.assertEqual(set(result), {"busy", "pages_written", "pages_checkpointed"})
        self.assertEqual(result["busy"], 0)
        wal = self.path.with_name(self.path.name + "-wal")
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)

    def test_every_operation_gets_a_fresh_connection_and_leaves_none_open(self) -> None:
        """Several assistant processes share this file, so nothing may hold a connection
        between operations: a pooled one would pin the file (on Windows blocking the
        support-bundle copy and migrate) and sit on locks other processes wait for."""

        backend = SqliteBackend(self.path)

        with backend.session() as first:
            first.execute("SELECT 1")
        with backend.session() as second:
            second.execute("SELECT 1")

        self.assertIsNot(first, second)
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            second.execute("SELECT 1")

    def test_insert_returning_id_uses_lastrowid_on_sqlite(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
            first = insert_returning_id(connection, "INSERT INTO t(v) VALUES (?)", ("a",))
            second = insert_returning_id(connection, "INSERT INTO t(v) VALUES (?)", ("b",))
        self.assertEqual((first, second), (1, 2))


if __name__ == "__main__":
    unittest.main()
