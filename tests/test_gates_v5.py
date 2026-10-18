"""Round-5 gate tests: colder no-file neighbour counts (bit packs, decision-identical) and
merge-copy state + prune. Same rules as test_gates.py: temp stores, every gate flag cleared
first, no live store, no port 8765.

    PYTHONPATH=src python -m pytest tests/test_gates_v5.py
    PYTHONPATH=src python -m unittest tests.test_gates_v5
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates

from test_gates import GateCase, routine
from test_gates_v4 import dump, text_for


class BitsetCountTests(GateCase):
    """Neighbour counts via bit packs must match the frozenset path exactly."""

    def test_near_count_matches_frozenset_on_random_store(self) -> None:
        rng = random.Random(5)
        for _ in range(80):
            self.store.remember(text_for(rng), rng.choice(["task", "other", "preference", "fact"]))
        with self.store._session() as connection:
            rows = gates._active_rows(connection)
        index = gates.ActiveIndex()
        index.sync(rows)
        sample = list(index.norm)[:: max(1, len(index.norm) // 40)]
        for memory_id in sample:
            true_near = len(index._near(memory_id))  # frozenset Jaccard path
            for limit in (1, 7, 63, 95):
                self.assertEqual(index._near_count(memory_id, limit), min(limit, true_near), (memory_id, limit))

    def test_select_lowest_identical_with_and_without_prebuilt_bits(self) -> None:
        rng = random.Random(9)
        for _ in range(60):
            self.store.remember(text_for(rng), "task")
        with self.store._session() as connection:
            rows = gates._active_rows(connection)
        a = gates.ActiveIndex()
        a.sync(rows)
        b = gates.ActiveIndex()
        b.sync(rows)
        b._bits = None
        b._bit_n.clear()
        chosen_a = gates.Book(rows).select(3, a)
        chosen_b = gates.Book(rows).select(3, b)
        self.assertEqual(
            [(c["id"], c["score"], c["similar"]) for c in chosen_a],
            [(c["id"], c["score"], c["similar"]) for c in chosen_b],
        )


class MergeCopyStateTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = 0
        self._time = patch("memorysafe_chatgpt.storage._now", side_effect=self.tick)
        self._time.start()
        for i in range(12):
            self.store.remember(routine(i), "task")
        self.kept = self.store.remember("Ana checked the oxygen cart in the east wing", "task")
        os.environ["MEMORYSAFE_GATE_SURPRISE"] = "1"

    def tearDown(self) -> None:
        self._time.stop()
        os.environ.pop("MEMORYSAFE_GATE_SURPRISE", None)
        super().tearDown()

    def tick(self) -> str:
        self.clock += 1
        return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.clock)).isoformat(timespec="seconds")

    def rows(self) -> dict[str, dict]:
        with self.store._session() as connection:
            return {r["id"]: dict(r) for r in connection.execute("SELECT * FROM memories")}

    def merge(self, text: str = "Ana checked oxygen cart east wing today") -> tuple[dict, str]:
        result = self.store.remember(text, "task")
        self.assertEqual(result["decision"], "MERGE", result["reason"])
        copy = next(i for i, r in self.rows().items() if r["state"] == gates.MERGE_COPY_STATE and r["content"] == text)
        return result, copy

    def test_merge_copy_uses_distinct_state_not_evicted(self) -> None:
        _result, copy_id = self.merge()
        health = self.store.health()
        self.assertEqual(self.rows()[copy_id]["state"], gates.MERGE_COPY_STATE)
        self.assertEqual(health["merge_copy_memories"], 1)
        self.assertEqual(health["evicted_memories"], 0)
        self.assertEqual(health["active_memories"], 13)

    def test_capacity_eviction_still_uses_evicted(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="10", MEMORYSAFE_GATE_SURPRISE="0"):
            os.environ.pop("MEMORYSAFE_GATE_SURPRISE", None)
            moved = self.store.remember("unique capacity eviction probe xyzzy", "other")
            self.assertTrue(moved.get("evicted_memory_ids"))
            for mid in moved["evicted_memory_ids"]:
                self.assertEqual(self.rows()[mid]["state"], "evicted")
            self.assertEqual(self.store.health()["merge_copy_memories"], 0)

    def test_prune_with_no_window_plans_nothing(self) -> None:
        self.merge()
        pre = dump(self.db)
        report = gates.prune_merge_copies(self.store, apply=False)
        self.assertEqual(report["plan"], [])
        self.assertIsNone(report["days"])
        self.assertIsNone(report["keep"])
        self.assertEqual(dump(self.db), pre)

    def test_dry_run_prune_changes_nothing_and_apply_matches_plan(self) -> None:
        # Three separate kept memories, each merged once → three merge_copy rows
        bases = [
            ("Bruno restocked the linen shelf in the north ward", "Bruno restocked linen shelf north ward today"),
            ("Chen sealed the crash cart in the annex wing", "Chen sealed crash cart annex wing today"),
            ("Dalia counted the medication cart in the south wing", "Dalia counted medication cart south wing today"),
        ]
        copies = []
        for long, short in bases:
            kept = self.store.remember(long, "task")
            self.assertIn(kept["decision"], ("STORE", "PROTECT", "MERGE"), kept["reason"])
            result = self.store.remember(short, "task")
            self.assertEqual(result["decision"], "MERGE", result["reason"])
            copy_id = next(
                i for i, r in self.rows().items()
                if r["state"] == gates.MERGE_COPY_STATE and r["content"] == short
            )
            copies.append(copy_id)
        pre = dump(self.db)
        dry = gates.prune_merge_copies(self.store, apply=False, keep=1)
        self.assertEqual(dump(self.db), pre)
        self.assertFalse(dry["applied"])
        self.assertEqual(len(dry["plan"]), 2)
        self.assertEqual([p["id"] for p in dry["plan"]], copies[:2])
        applied = gates.prune_merge_copies(self.store, apply=True, keep=1)
        self.assertEqual(applied["pruned"], [p["id"] for p in dry["plan"]])
        self.assertEqual([p["id"] for p in applied["plan"]], [p["id"] for p in dry["plan"]])
        after = self.rows()
        for mid in copies[:2]:
            self.assertEqual(after[mid]["state"], "deleted")
        self.assertEqual(after[copies[2]]["state"], gates.MERGE_COPY_STATE)

    def test_prune_by_age_respects_days_window(self) -> None:
        _r, copy_id = self.merge()
        # Age the copy far in the past
        with self.store._session() as connection:
            connection.execute(
                "UPDATE memories SET created_at = ? WHERE id = ?",
                ((datetime(2025, 1, 1, tzinfo=timezone.utc)).isoformat(timespec="seconds"), copy_id),
            )
        dry = gates.prune_merge_copies(self.store, apply=False, days=30)
        self.assertEqual([p["id"] for p in dry["plan"]], [copy_id])
        # A huge window leaves it alone
        none = gates.prune_merge_copies(self.store, apply=False, days=5000)
        self.assertEqual(none["plan"], [])

    def test_env_flag_sets_default_prune_window(self) -> None:
        _r, copy_id = self.merge()
        with self.store._session() as connection:
            connection.execute(
                "UPDATE memories SET created_at = ? WHERE id = ?",
                ((datetime(2025, 6, 1, tzinfo=timezone.utc)).isoformat(timespec="seconds"), copy_id),
            )
        with self.flags(MEMORYSAFE_GATE_MERGE_PRUNE_DAYS="30"):
            report = gates.prune_merge_copies(self.store, apply=False)
        self.assertEqual(report["days"], 30)
        self.assertEqual([p["id"] for p in report["plan"]], [copy_id])

    def test_restore_works_until_pruned_then_refuses(self) -> None:
        before = self.rows()
        _r, copy_id = self.merge()
        # Still restorable
        planned = self.store.restore(copy_id, confirm=False)
        self.assertTrue(planned["needs_confirmation"])
        self.assertTrue(planned["would_undo_merge"]["reverted"])
        # Prune it
        applied = gates.prune_merge_copies(self.store, apply=True, keep=0)
        self.assertEqual(applied["pruned"], [copy_id])
        self.assertEqual(self.rows()[copy_id]["state"], "deleted")
        refused = self.store.restore(copy_id, confirm=False)
        self.assertFalse(refused["restored"])
        self.assertTrue(refused.get("pruned_merge_copy"))
        self.assertFalse(refused.get("needs_confirmation"))
        self.assertNotIn("would_undo_merge", refused)
        refused_apply = self.store.restore(copy_id, confirm=True)
        self.assertFalse(refused_apply["restored"])
        self.assertTrue(refused_apply.get("pruned_merge_copy"))
        # Kept memory still as the merge left it (prune does not undo merges)
        self.assertNotEqual(self.rows()[self.kept["memory_id"]], before[self.kept["memory_id"]])

    def test_restore_after_prune_does_not_change_the_store(self) -> None:
        _r, copy_id = self.merge()
        gates.prune_merge_copies(self.store, apply=True, keep=0)
        pre = dump(self.db)
        self.store.restore(copy_id, confirm=True)
        self.assertEqual(dump(self.db), pre)
