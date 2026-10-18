"""Round-4 gate tests: the sidecar file that lets a fresh process skip re-indexing every active
memory, and the surprise-gate MERGE that `restore` can now undo. Same rules as test_gates.py:
temp stores, every gate flag cleared first, no live store, no port 8765.

    PYTHONPATH=src python -m pytest tests/test_gates_v4.py
    PYTHONPATH=src python -m unittest tests.test_gates_v4
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates

from test_gates import GateCase, routine  # noqa: E402  (tests dir is on sys.path under both runners)

WORDS = ("amber basil cedar dune ember fjord glade harbor iris juniper kettle lagoon meadow nectar orchard "
         "pebble quartz ridge saffron thicket umber valley willow yarrow zephyr anchor beacon canyon delta").split()


def text_for(rng: random.Random) -> str:
    if rng.random() < 0.6:
        return routine(rng.randrange(80))
    return " ".join(rng.sample(WORDS, rng.randint(5, 8))).capitalize()


def dump(db: Path) -> dict[str, str]:
    """sha256 of every table's rows (what the acceptance script compares)."""
    connection = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        names = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: hashlib.sha256(json.dumps(connection.execute(f'SELECT * FROM "{n}" ORDER BY rowid').fetchall(),
                                             default=str).encode()).hexdigest() for n in names}
    finally:
        connection.close()


class SidecarCase(GateCase):
    """A store with the capacity gate on, helpers to behave like a fresh process."""

    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("MEMORYSAFE_GATE_PERSIST", None)
        self.sidecar = Path(str(self.db) + ".gates-index")

    def restart(self, save: bool = True) -> None:
        """What the end of one CLI call and the start of the next does."""
        if save:
            gates.save_indexes(force=True, prefetch=False)
        gates._INDEXES.clear()

    def populate(self, n: int = 60, seed: int = 1) -> None:
        rng = random.Random(seed)
        for _ in range(n):
            self.store.remember(text_for(rng), rng.choice(["task", "other", "preference"]))


class SidecarFileTests(SidecarCase):
    """Round 13: the sidecar file was removed (reviewer A P1 / reviewer B). Its round-4 tests are gone; the
    no-file guarantees are tested in test_gates_r13.py."""

    def test_no_file_after_a_gated_session(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="50"):
            self.populate(70)
            self.restart()
        self.assertFalse(self.sidecar.exists())


class CappedCountTests(unittest.TestCase):
    """The capped neighbour count stays exact when members of a big cluster come and go,
    live and across a save/load (floor + slack, round 4). A small cap makes the cap bite."""

    def brute(self, index: gates.ActiveIndex, memory_id: str) -> int:
        mine = index.terms[memory_id]
        true = sum(1 for other, theirs in index.terms.items()
                   if other != memory_id and gates.jaccard(mine, theirs) >= gates.CLUSTER_SIM)
        return min(true, gates.CLUSTER_CAP - 1)

    def test_counts_equal_brute_force_through_random_arrivals_and_departures(self) -> None:
        from test_gates_v2 import ranked_row

        words = ["ana", "cart", "wing", "oxygen", "lantern", "harbour"]
        for cap, slack, seed in ((6, 3, 1), (6, 3, 2), (10, 4, 3), (5, 0, 4), (8, 12, 5)):
            with patch.object(gates, "CLUSTER_CAP", cap), patch.object(gates, "CLUSTER_SLACK", slack):
                rng = random.Random(seed)
                live = {f"M{i}": ranked_row(i, " ".join(rng.sample(words, rng.randint(3, 4)))) for i in range(60)}
                index = gates.ActiveIndex()
                index.sync(list(live.values()))
                nxt = 100
                reached = False
                for step in range(120):
                    for _ in range(rng.randint(1, 4)):
                        if live and rng.random() < 0.55:  # mostly departures, as an evicting store has
                            live.pop(rng.choice(sorted(live)))
                        else:
                            live[f"M{nxt}"] = ranked_row(nxt, " ".join(rng.sample(words, rng.randint(3, 4))))
                            nxt += 1
                    if rng.random() < 0.15 and live:  # reworded in place
                        victim = rng.choice(sorted(live))
                        live[victim] = ranked_row(int(victim[1:]), " ".join(rng.sample(words, 4)))
                    index.sync(list(live.values()))
                    for memory_id in rng.sample(sorted(live), min(len(live), 12)):
                        self.assertEqual(index.neighbours(memory_id), self.brute(index, memory_id), (cap, slack, seed, step, memory_id))
                    reached |= bool(index.floor)
                    self.assertTrue(all(index.memo[i] == cap - 1 and index.floor[i] >= cap - 1 for i in index.floor))
                self.assertTrue(reached, (cap, slack, seed))  # clusters did reach the cap, so the floor path ran

    def test_a_departure_from_a_big_cluster_needs_no_recount_until_the_floor_runs_out(self) -> None:
        from test_gates_v2 import ranked_row

        with patch.object(gates, "CLUSTER_CAP", 6), patch.object(gates, "CLUSTER_SLACK", 4):
            rows = [ranked_row(i, "routine round cart wing") for i in range(20)]  # one cluster of 20
            index = gates.ActiveIndex()
            index.sync(rows)
            self.assertEqual(index.neighbours("M0"), 5)
            self.assertEqual(index.recounted, 1)
            alive = list(rows)
            for dropped in range(1, 4):  # three leave: floor 5 -> 2, below the cap -> recount (deeper)
                alive = [r for r in alive if r["id"] != f"M{dropped}"]
                index.sync(alive)
            self.assertEqual(index.neighbours("M0"), 5)
            self.assertEqual(index.recounted, 2)
            self.assertEqual(index.floor["M0"], 5 + 4)  # looked 4 deeper than the cap
            for dropped in range(4, 8):  # four more leave: still >= the cap, absorbed with no recount
                alive = [r for r in alive if r["id"] != f"M{dropped}"]
                index.sync(alive)
                self.assertEqual(index.neighbours("M0"), 5)
            self.assertEqual(index.recounted, 2)

    # round 13: test_floor_survives_a_save_and_load was removed with the sidecar file


class SurpriseMergeUndoTests(GateCase):
    """The surprise gate's MERGE is reversible by `restore` (store-and-link)."""

    def setUp(self) -> None:
        super().setUp()
        # timestamps have one-second resolution: give every store call its own second
        self.clock = 0
        clock = patch("memorysafe_chatgpt.storage._now", side_effect=self.tick)
        clock.start()
        self.addCleanup(clock.stop)
        self.fill(12)
        self.kept = self.store.remember("Ana checked the oxygen cart in the east wing", "task")
        os.environ["MEMORYSAFE_GATE_SURPRISE"] = "1"

    def tick(self) -> str:
        self.clock += 1
        return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.clock)).isoformat(timespec="seconds")

    def rows(self) -> dict[str, dict]:
        with self.store._session() as connection:
            return {r["id"]: dict(r) for r in connection.execute("SELECT * FROM memories")}

    def relations(self) -> list[dict]:
        with self.store._session() as connection:
            return [dict(r) for r in connection.execute("SELECT * FROM memory_relations ORDER BY id")]

    def merge(self, text: str = "Ana checked oxygen cart east wing today") -> tuple[dict, str]:
        result = self.store.remember(text, "task")
        self.assertEqual(result["decision"], "MERGE", result["reason"])
        copy = next(i for i, r in self.rows().items() if r["state"] == "merge_copy" and r["content"] == text)
        return result, copy

    def test_the_merge_still_does_what_it_did_and_keeps_the_write_as_a_hidden_copy(self) -> None:
        before = self.rows()
        result, copy_id = self.merge()
        after = self.rows()
        self.assertEqual(result["memory_id"], self.kept["memory_id"])
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(self.store.health()["active_memories"], 13)  # same active set as in round 3
        kept = after[self.kept["memory_id"]]
        self.assertEqual(kept["content"], "Ana checked oxygen cart east wing today" if len("Ana checked oxygen cart east wing today") >= len(before[self.kept["memory_id"]]["content"]) else before[self.kept["memory_id"]]["content"])
        copy = after[copy_id]
        self.assertEqual((copy["state"], copy["content"], copy["category"], copy["protected"]),
                         ("merge_copy", "Ana checked oxygen cart east wing today", "task", 0))
        link = self.relations()[-1]
        self.assertEqual((link["old_memory_id"], link["new_memory_id"], link["decision"], link["status"], link["source"]),
                         (self.kept["memory_id"], copy_id, "MERGE", "merged", "gate:surprise"))
        self.assertIn(copy_id, result["reason"])
        self.assertIn(f"restore {copy_id}", result["reason"])
        self.assertIn("Previous text", result["reason"] + " ".join(h["reason"] for h in self.store.explain(result["memory_id"])["history"]))

    def test_hidden_copy_is_out_of_recall_and_does_not_double_count_merges(self) -> None:
        _result, copy_id = self.merge()
        self.assertNotIn(copy_id, [h["memory_id"] for h in self.store.find("oxygen cart east wing", 20)])
        history = self.store.explain(copy_id)["history"]
        self.assertEqual([h["decision"] for h in history], ["MERGE_COPY"])
        with self.store._session() as connection:
            merges = connection.execute("SELECT COUNT(*) FROM decision_events WHERE decision='MERGE'").fetchone()[0]
        self.assertEqual(merges, 1)  # health counts MERGE events as merges saved: still one per merge

    def test_restore_round_trip_returns_the_kept_memory_to_its_prior_state(self) -> None:
        before = self.rows()
        _result, copy_id = self.merge()
        merged = self.rows()
        self.assertNotEqual(merged[self.kept["memory_id"]], before[self.kept["memory_id"]])  # the merge did change it
        reply = self.store.restore(copy_id, confirm=True)
        after = self.rows()
        self.assertTrue(reply["restored"])
        self.assertTrue(reply["merge_undone"]["reverted"])
        # every row that existed before is byte-identical to before (updated_at included)
        for memory_id, row in before.items():
            self.assertEqual(after[memory_id], row, memory_id)
        # the write itself is an ordinary active memory again
        self.assertEqual(set(after) - set(before), {copy_id})
        self.assertEqual(after[copy_id]["state"], "active")
        self.assertEqual(after[copy_id]["content"], "Ana checked oxygen cart east wing today")
        self.assertEqual(self.store.health()["active_memories"], 14)
        self.assertEqual(self.relations()[-1]["status"], "undone")
        self.assertIn("Merge undone", self.store.explain(copy_id)["history"][-1]["reason"])

    def test_round_trip_when_the_incoming_wording_is_the_longer_one(self) -> None:
        shorter = self.store.remember("Bruno restocked linen shelf north ward today", "task")
        self.assertIn(shorter["decision"], ("STORE", "PROTECT"))
        before = self.rows()
        longer = "Bruno restocked the linen shelf in the north ward"
        result = self.store.remember(longer, "task")
        self.assertEqual(result["decision"], "MERGE", result["reason"])
        self.assertEqual(result["content"], longer)  # the longer wording (the incoming one) is what is kept
        self.assertEqual(self.rows()[shorter["memory_id"]]["content"], longer)
        copy_id = next(i for i, r in self.rows().items() if r["state"] == "merge_copy")
        self.assertEqual(self.rows()[copy_id]["content"], longer)
        reply = self.store.restore(copy_id, confirm=True)
        self.assertTrue(reply["merge_undone"]["reverted"])
        self.assertIn("content", reply["merge_undone"]["columns"])
        after = self.rows()
        self.assertEqual(after[shorter["memory_id"]], before[shorter["memory_id"]])  # old wording is back
        self.assertEqual(after[shorter["memory_id"]]["content"], "Bruno restocked linen shelf north ward today")
        self.assertEqual({i: r for i, r in after.items() if i != copy_id}, before)

    def test_dry_run_changes_nothing_and_the_plan_equals_what_apply_does(self) -> None:
        _result, copy_id = self.merge()
        pre = dump(self.db)
        rows_pre = self.rows()
        planned = self.store.restore(copy_id, confirm=False)
        self.assertEqual(dump(self.db), pre)  # dry-run checksum unchanged
        self.assertFalse(planned["restored"])
        self.assertTrue(planned["needs_confirmation"])
        plan = planned["would_undo_merge"]
        self.assertTrue(plan["reverted"])
        self.assertEqual(plan["kept_memory_id"], self.kept["memory_id"])
        applied = self.store.restore(copy_id, confirm=True)
        self.assertEqual({k: v for k, v in applied["merge_undone"].items()}, plan)  # apply == plan
        rows_post = self.rows()
        changed = {c for c in rows_pre[self.kept["memory_id"]] if rows_pre[self.kept["memory_id"]][c] != rows_post[self.kept["memory_id"]][c]}
        self.assertEqual(changed, set(plan["columns"]))  # exactly the planned columns changed on the kept memory
        self.assertEqual({i for i in rows_pre if i not in (copy_id, self.kept["memory_id"]) and rows_pre[i] != rows_post[i]}, set())

    def test_restoring_an_ordinary_evicted_memory_is_unchanged(self) -> None:
        self.store.forget(self.kept["memory_id"], confirm=True)
        reply = self.store.restore(self.kept["memory_id"], confirm=True)
        self.assertTrue(reply["restored"])
        self.assertNotIn("merge_undone", reply)
        self.assertNotIn("would_undo_merge", self.store.restore(self.kept["memory_id"], confirm=False))

    def test_a_kept_memory_changed_since_the_merge_is_left_alone(self) -> None:
        _result, copy_id = self.merge()
        self.store.protect(self.kept["memory_id"], True, "user asked")
        mid = self.rows()[self.kept["memory_id"]]
        planned = self.store.restore(copy_id, confirm=False)
        self.assertFalse(planned["would_undo_merge"]["reverted"])
        reply = self.store.restore(copy_id, confirm=True)
        self.assertTrue(reply["restored"])
        self.assertFalse(reply["merge_undone"]["reverted"])
        self.assertIn("changed since the merge", reply["reason"])
        self.assertEqual(self.rows()[self.kept["memory_id"]], mid)  # not touched
        self.assertEqual(self.rows()[copy_id]["state"], "active")
        self.assertEqual(self.relations()[-1]["status"], "detached")

    def test_forgotten_kept_memory_stays_forgotten(self) -> None:
        _result, copy_id = self.merge()
        self.store.forget(self.kept["memory_id"], confirm=True)
        reply = self.store.restore(copy_id, confirm=True)
        self.assertTrue(reply["restored"])
        self.assertFalse(reply["merge_undone"]["reverted"])
        self.assertEqual(self.rows()[self.kept["memory_id"]]["state"], "deleted")

    def test_two_merges_into_one_memory_undo_exactly_newest_first(self) -> None:
        before = self.rows()[self.kept["memory_id"]]
        _a, first = self.merge("Ana checked oxygen cart east wing today")
        second_text = "Ana checked oxygen cart east wing early"
        second = self.store.remember(second_text, "task")
        if second["decision"] != "MERGE":
            self.skipTest("second fixture not merged: " + second["reason"])
        second_copy = next(i for i, r in self.rows().items() if r["state"] == "merge_copy" and r["content"] == second_text)
        self.assertTrue(self.store.restore(second_copy, confirm=True)["merge_undone"]["reverted"])
        self.assertTrue(self.store.restore(first, confirm=True)["merge_undone"]["reverted"])
        self.assertEqual(self.rows()[self.kept["memory_id"]], before)

    def test_oldest_first_restores_both_copies_and_never_edits_a_changed_memory(self) -> None:
        _a, first = self.merge("Ana checked oxygen cart east wing today")
        second_text = "Ana checked oxygen cart east wing early"
        if self.store.remember(second_text, "task")["decision"] != "MERGE":
            self.skipTest("second fixture not merged")
        second_copy = next(i for i, r in self.rows().items() if r["state"] == "merge_copy" and r["content"] == second_text)
        reply = self.store.restore(first, confirm=True)
        self.assertFalse(reply["merge_undone"]["reverted"])  # the kept memory was changed by the second merge
        self.assertEqual(self.rows()[first]["state"], "active")
        self.assertEqual(self.rows()[second_copy]["state"], "merge_copy")

    def test_protected_incoming_write_keeps_its_protection_in_the_copy(self) -> None:
        before = self.rows()[self.kept["memory_id"]]
        result = self.store.remember("Ana checked oxygen cart east wing today", "task", 0.9)
        self.assertEqual(result["decision"], "MERGE")
        copy_id = next(i for i, r in self.rows().items() if r["state"] == "merge_copy")
        copy = self.rows()[copy_id]
        self.assertEqual(copy["importance"], 0.9)
        self.store.restore(copy_id, confirm=True)
        self.assertEqual(self.rows()[self.kept["memory_id"]], before)

    def test_forgetting_a_hidden_copy_does_not_touch_the_kept_memory(self) -> None:
        _result, copy_id = self.merge()
        merged = self.rows()[self.kept["memory_id"]]
        self.store.forget(copy_id, confirm=True)
        self.assertEqual(self.rows()[copy_id]["state"], "deleted")
        self.assertEqual(self.rows()[self.kept["memory_id"]], merged)

    def test_capacity_gate_ignores_hidden_copies(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="13"):
            _result, copy_id = self.merge()
            self.assertEqual(self.store.health()["active_memories"], 13)
            self.assertEqual(self.rows()[copy_id]["state"], "merge_copy")
            result = self.store.remember("Quarterly audit samples are stored in the greenhouse cabinet", "other")
            self.assertTrue(result["evicted_memory_ids"])
            self.assertNotIn(copy_id, result["evicted_memory_ids"])

    def test_cli_restore_reports_the_merge(self) -> None:
        import io
        from contextlib import redirect_stdout

        from memorysafe_chatgpt.cli import main

        _result, copy_id = self.merge()

        def cli(*args: str) -> dict:
            out = io.StringIO()
            with patch("sys.argv", ["memorysafe", *args]), redirect_stdout(out):
                main()
            return json.loads(out.getvalue())

        pre = dump(self.db)
        planned = cli("restore", copy_id, "--json")  # no --confirm: a dry run
        self.assertEqual(dump(self.db), pre)
        self.assertTrue(planned["would_undo_merge"]["reverted"])
        self.assertTrue(cli("restore", copy_id, "--confirm", "--json")["merge_undone"]["reverted"])


if __name__ == "__main__":
    unittest.main()
