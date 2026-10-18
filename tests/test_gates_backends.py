"""The opt-in gates on any backend, not only the local SQLite file.

A server backend (one process serving many users from one database) differs from the
local file in three ways the gates used to assume away: every store there has the same
database_path (None), the engine cannot create tables or triggers in its database, and
it may not move a memory out of recall by itself. These tests pin how the gates handle
each, using SQLite stand-ins, plus two fixes that apply everywhere: a user's own table or
index named like a gate trigger no longer breaks gated writes, and a capacity gate with
no capacity of its own no longer switches MEMORYSAFE_MAX_ACTIVE off.
"""

from __future__ import annotations

import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_gates as base
import test_gates_r6 as r6
from test_gates_r13 import Case
from memorysafe_chatgpt import gate_flags, gates, gates_r6
from memorysafe_chatgpt.backends.sqlite import SqliteBackend
from memorysafe_chatgpt.storage import MemoryStore

T = gates.LOG_TABLE


class _RankOnly(SqliteBackend):
    """A backend that, like the hosted edition's, never lets a gate move memories out of recall."""

    gates_rank_only = True


class _Tenant(SqliteBackend):
    """A backend that names who a store is for, as a server backend does."""

    def __init__(self, path: Path, user: str, layer: str) -> None:
        super().__init__(path)
        self.user, self.layer, self._file = user, layer, path
        self.database_path = None   # what every store of a server backend reports

    def _connect(self) -> sqlite3.Connection:
        self.database_path = self._file
        try:
            return super()._connect()
        finally:
            self.database_path = None

    def gate_cache_identity(self) -> tuple[str, ...]:
        return ("test", self.user, self.layer)


class CacheKeyTests(base.GateCase):
    def test_a_local_store_keeps_its_file_as_the_key(self) -> None:
        self.assertEqual(gate_flags.cache_key(self.store), str(self.db))

    def test_stores_of_a_server_backend_are_keyed_by_user_and_layer_not_by_path(self) -> None:
        stores = [MemoryStore(backend=_Tenant(self.db, user, layer))
                  for user in ("u1", "u2") for layer in ("you", "project:alpha")]
        keys = [gate_flags.cache_key(store) for store in stores]
        self.assertEqual(len(set(keys)), 4)
        self.assertNotIn("None", keys)
        again = MemoryStore(backend=_Tenant(self.db, "u1", "you"))
        self.assertEqual(gate_flags.cache_key(again), keys[0])

    def test_a_store_with_neither_a_file_nor_an_identity_shares_nothing(self) -> None:
        class Anonymous:
            database_path = None

        first, second = Anonymous(), Anonymous()
        self.assertNotEqual(gate_flags.cache_key(first), gate_flags.cache_key(second))

    def test_counters_of_a_server_store_outlive_the_store_object(self) -> None:
        first = MemoryStore(backend=_Tenant(self.db, "u1", "you"))
        gate_flags.store_state(first)["_r6_writes"] = 7
        second = MemoryStore(backend=_Tenant(self.db, "u1", "you"))
        self.assertEqual(gate_flags.store_state(second)["_r6_writes"], 7)
        other = MemoryStore(backend=_Tenant(self.db, "u2", "you"))
        self.assertNotIn("_r6_writes", gate_flags.store_state(other))
        self.assertIs(gate_flags.store_state(self.store), self.store.__dict__)   # local: unchanged


class CapacityFallbackTests(base.GateCase):
    def _fill(self, store: MemoryStore, n: int = 8) -> None:
        for i in range(n):
            store.remember(base.routine(i), "task")

    def test_gate_capacity_zero_leaves_the_shipped_bound_in_force(self) -> None:
        """MEMORYSAFE_GATE_MAX_ACTIVE=0 with the capacity gate on used to disable MEMORYSAFE_MAX_ACTIVE:
        the store grew past the bound the user had set (62 active of 20 in the hosted rerun)."""
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="0", MEMORYSAFE_MAX_ACTIVE="5"):
            self._fill(self.store)
        self.assertEqual(self.store.health()["active_memories"], 5)

    def test_the_gate_capacity_still_wins_when_it_is_set(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="4", MEMORYSAFE_MAX_ACTIVE="6"):
            self._fill(self.store)
        self.assertEqual(self.store.health()["active_memories"], 4)

    def test_a_rank_only_backend_never_gets_a_gate_capacity(self) -> None:
        store = MemoryStore(backend=_RankOnly(self.db))
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_USEFUL="1", MEMORYSAFE_GATE_MAX_ACTIVE="3"):
            self._fill(store)
        self.assertEqual(store.health()["active_memories"], 8)

    def test_evicting_settings_are_named(self) -> None:
        for value, expected in (("40", ["MEMORYSAFE_GATE_MAX_ACTIVE"]), ("0", []), ("", []), ("x", [])):
            with self.subTest(value=value), self.flags(MEMORYSAFE_GATE_MAX_ACTIVE=value):
                self.assertEqual(gate_flags.evicting_gate_settings(), expected)


class RankOnlyExtinctionTests(r6.R6Case):
    def test_rank_only_weakens_but_never_extinguishes(self) -> None:
        # Gate flags and tuning come from the environment; this test sets exactly the ones it needs.
        tuning = patch.dict(os.environ)
        tuning.start()
        self.addCleanup(tuning.stop)
        for name in [n for n in os.environ if n.startswith("MEMORYSAFE_GATE_")]:
            del os.environ[name]
        self.store = MemoryStore(backend=_RankOnly(self.db))
        for i in range(12):
            self.store.remember(base.routine(i), "task")
        with self.flags(**r6.EXT):
            old = self.store.remember("Omar's line manager is Noor Delgado", "task")["memory_id"]
            self.store.remember("Omar has a new line manager, Chiara Haddad, since the reorg", "task")
            self.store.remember("We migrated the team: Omar's new line manager is Chiara Haddad", "task")
        self.assertEqual(self.st(old), "active")
        self.assertEqual(self.rel(old), 2)   # the weakening is kept: it ranks lower in find()
        self.assertFalse(self.events(old, "EXTINGUISH"))


class SurpriseWordingTests(base.GateCase):
    def test_a_surprise_protection_does_not_say_the_user_asked(self) -> None:
        """The reason ended "Kept because you asked for it directly" when the gate, not the person,
        protected the memory (the hosted edition writes every call as source manual)."""
        for i in range(12):
            self.store.remember(base.routine(i), "task")
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember("Quarterly audit samples are stored in the greenhouse cabinet", "other")
        self.assertTrue(result["protected"])
        self.assertIn("high surprise", result["reason"])
        self.assertNotIn("asked for it", result["reason"])
        self.assertIn("unlike anything already stored", result["reason"])
        plain = self.store.remember("Keep the spare key under the blue pot", "other")
        self.assertNotIn("unlike anything already stored", plain["reason"])


class ReservedNameTests(Case):
    """A user's own object named like one of the gate change log's objects. SQLite gives triggers a
    namespace of their own, so a table or index called memorysafe_gate_changes_ins can sit next to the
    trigger of that name; the log used to read it as a malformed trigger and every gated write raised."""

    def setUp(self) -> None:
        super().setUp()
        self.put("amber notebook")
        self.put("violet bicycle")

    def _objects(self) -> list:
        with closing(sqlite3.connect(self.db)) as c, c:
            return sorted(c.execute("SELECT type, name FROM sqlite_master WHERE name LIKE 'memorysafe_gate%'"))

    def test_an_index_named_like_a_trigger(self) -> None:
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(f"CREATE INDEX {T}_ins ON memories(category)")
        self.put("green lantern")
        self.put("ochre kettle")
        self.assertIn(("index", f"{T}_ins"), self._objects())
        with self.store._session() as c:
            self.assertTrue(gates.change_log_installed(c))

    def test_a_table_named_like_a_trigger(self) -> None:
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(f"CREATE TABLE {T}_upd (x)")
            c.execute(f"INSERT INTO {T}_upd VALUES (42)")
        self.put("green lantern")
        self.put("ochre kettle")
        with closing(sqlite3.connect(self.db)) as c, c:
            self.assertEqual(c.execute(f"SELECT x FROM {T}_upd").fetchone()[0], 42)

    def test_a_view_on_the_table_name_is_left_alone_and_writes_still_succeed(self) -> None:
        with self.store._session() as c:
            gates.remove_change_log(c)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(f"CREATE VIEW {T} AS SELECT 1 AS seq, 1 AS row_id")
        with self.store._session() as c:
            with self.assertRaises(RuntimeError):
                gates.ensure_change_log(c)
            self.assertFalse(gates.change_log_status(c)["installed"])
        self.put("green lantern")
        self.put("ochre kettle")
        self.assertIn(("view", T), self._objects())

    def test_a_text_seq_column_is_repaired_not_raised_on(self) -> None:
        with closing(sqlite3.connect(self.db)) as c, c:
            for name in gates._LOG_TRIGGERS:
                c.execute(f"DROP TRIGGER {name}")
            c.execute(f"DROP TABLE {T}")
            c.execute(f"CREATE TABLE {T} (seq TEXT, row_id INTEGER NOT NULL)")
            c.execute(f"INSERT INTO {T} VALUES ('abc', 1)")
        with self.store._session() as c:
            gates.ensure_change_log(c)
            self.assertTrue(gates.change_log_installed(c))

    def test_a_reader_rebuilds_after_the_log_is_replaced_by_lower_numbers(self) -> None:
        """A repaired log restarted at the old table's highest number + 2. A table rebuilt by hand with
        lower numbers could then restart exactly at a reader's position, and the reader missed every
        change in between (here a content edit that keeps the normalized text)."""
        mid = self.put("Goal: launch the amber notebook")
        fc = gates_r6._feature_cache(self.store)
        with self.store._session() as c:
            book = gates_r6._snapshot(c, fc, None)
            gates_r6._feats(fc, c, [mid], book.rows)
            cached = book.seq
        with closing(sqlite3.connect(self.db)) as c, c:
            for name in gates._LOG_TRIGGERS:
                c.execute(f"DROP TRIGGER {name}")
            c.execute(f"DROP TABLE {T}")
            c.execute(f"CREATE TABLE {T} (seq INTEGER PRIMARY KEY, row_id INTEGER NOT NULL, extra)")
            c.execute(f"INSERT INTO {T} VALUES (?, 1, NULL)", (cached - 2,))
            c.execute("UPDATE memories SET content = ? WHERE id = ?", ("Goal launch the amber notebook", mid))
        with self.store._session() as c:
            book = gates_r6._snapshot(c, fc, None)
            gates_r6._feats(fc, c, [mid], book.rows)
        self.assertEqual(fc.by_id[mid][1], gates_r6.features("Goal launch the amber notebook"))


if __name__ == "__main__":
    unittest.main()
