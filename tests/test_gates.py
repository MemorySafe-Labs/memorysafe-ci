"""Tests for the opt-in gates prototype (src/memorysafe_chatgpt/gates.py).

Run from the repository root (unittest-style, so both runners work):
    PYTHONPATH=src python -m pytest tests/test_gates.py
    PYTHONPATH=src python -m unittest tests.test_gates
Every test uses its own temporary store and clears every gate flag first.

These are behaviour tests of a prototype. The benchmark test only checks that the
toy stream behaves as designed; it is not evidence about real memories.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates
from memorysafe_chatgpt.storage import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
GATE_FLAGS = ("MEMORYSAFE_GATE_CAPACITY", "MEMORYSAFE_GATE_SURPRISE", "MEMORYSAFE_GATE_CONSOLIDATE",
              "MEMORYSAFE_GATE_MAX_ACTIVE", "MEMORYSAFE_MAX_ACTIVE", "MEMORYSAFE_GATE_PROTECT_SHARE",
              "MEMORYSAFE_GATE_SURPRISE_LOW_SIM", "MEMORYSAFE_GATE_SURPRISE_MERGE_SIM",
              "MEMORYSAFE_GATE_SURPRISE_HIGH_NOVELTY", "MEMORYSAFE_GATE_SURPRISE_MIN_ACTIVE")

NAMES = ["Ana", "Bruno", "Chen", "Dalia", "Emre", "Farah", "Gil", "Hana", "Ivo", "Jun"]
WINGS = ["east", "west", "north", "south", "annex", "lobby", "ward", "dock"]


def routine(i: int) -> str:
    """Common, number-free items: the shipped numeric-template check cannot see a series."""
    return f"Routine round: {NAMES[i % 10]} checked the {WINGS[(i // 10) % 8]} cart in the {WINGS[(i * 3) % 8]} wing"


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "data" / "memorysafe.sqlite3"
        self.env = patch.dict(os.environ, {"MEMORYSAFE_DB_PATH": str(self.db),
                                           "MEMORYSAFE_STATE_DIR": str(Path(self.temp.name) / "state")})
        self.env.start()
        for flag in GATE_FLAGS:
            os.environ.pop(flag, None)
        self.store = MemoryStore(self.db)

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def flags(self, **flags: str):
        return patch.dict(os.environ, flags)

    def state(self, result: dict) -> str:
        return self.store.explain(result["memory_id"])["state"]

    def history(self, result: dict) -> list[dict]:
        return self.store.explain(result["memory_id"])["history"]

    def row_count(self) -> int:
        with self.store._session() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def event_count(self) -> int:
        with self.store._session() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0])

    def fill(self, n: int, category: str = "task") -> list[dict]:
        return [self.store.remember(routine(i), category) for i in range(n)]


class OffByDefaultTests(GateCase):
    def test_every_gate_is_off_without_its_flag(self) -> None:
        self.assertFalse(gates.capacity_gate_enabled())
        self.assertFalse(gates.surprise_gate_enabled())
        self.assertFalse(gates.consolidate_enabled())

    def test_falsey_values_keep_a_gate_off(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="0", MEMORYSAFE_GATE_SURPRISE="off", MEMORYSAFE_GATE_CONSOLIDATE=""):
            self.assertFalse(gates.capacity_gate_enabled() or gates.surprise_gate_enabled() or gates.consolidate_enabled())

    def test_shipped_capacity_path_is_unchanged_when_the_gate_is_off(self) -> None:
        """MEMORYSAFE_MAX_ACTIVE alone still uses the shipped eviction and its wording."""
        with self.flags(MEMORYSAFE_MAX_ACTIVE="5"):
            results = self.fill(10)
        events = [h for r in results for h in self.history(r) if h["decision"] == "EVICT"]
        self.assertEqual(len(events), 5)
        self.assertTrue(all("MEMORYSAFE_MAX_ACTIVE" in e["reason"] and e["source"] == "capacity" for e in events))
        self.assertFalse(any("gate" in e["reason"].lower() for e in events))

    def test_surprise_off_leaves_near_duplicates_to_the_shipped_merge(self) -> None:
        self.fill(12)
        result = self.store.remember(routine(0) + " today", "task")
        events = self.history(result)
        self.assertFalse(any("Surprise gate" in e["reason"] for e in events))
        self.assertFalse(any(e["source"].startswith("gate:") for e in events))


class CapacityGateTests(GateCase):
    def test_rare_beats_a_pile_of_common_items_even_in_the_same_category(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="12"):
            oddball = self.store.remember("The spare server room key is under the reception desk", "task")
            self.fill(40)
        self.assertEqual(self.state(oddball), "active")
        self.assertEqual(self.store.health()["active_memories"], 12)

    def test_protected_and_critical_are_kept_first(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="6"):
            pinned = self.store.remember("Tuesday standup notes go in the blue folder", "task")
            self.store.protect(pinned["memory_id"], True, "the user asked")
            safety = self.store.remember("Bed 12 patient is allergic to penicillin", "safety")
            decision = self.store.remember("We decided to freeze the schema until January", "decision")
            self.fill(30)
        for kept in (pinned, safety, decision):
            self.assertEqual(self.state(kept), "active")

    def test_an_unprotected_critical_memory_outranks_ordinary_ones(self) -> None:
        """safety/decision are auto-protected, so reach the critical tier by unprotecting."""
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="6"):
            critical = self.store.remember("Bed 12 patient is allergic to penicillin", "safety")
            self.store.protect(critical["memory_id"], False)
            self.fill(30)
        self.assertEqual(self.state(critical), "active")

    def test_move_is_logged_with_a_human_reason_and_nothing_is_deleted(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="5"):
            results = self.fill(12)
        self.assertEqual(self.row_count(), 12)
        moved = [r for r in results if self.state(r) == "evicted"]
        self.assertEqual(len(moved), 7)
        event = next(h for h in self.history(moved[0]) if h["decision"] == "EVICT")
        self.assertEqual(event["source"], "gate:capacity")
        for phrase in ("Capacity gate", "capacity of 5", "restorable", "keep score", "rarity", "Restore brings it back"):
            self.assertIn(phrase, event["reason"])

    def test_restore_brings_a_moved_memory_back(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="5"):
            results = self.fill(12)
        moved = next(r for r in results if self.state(r) == "evicted")
        self.store.restore(moved["memory_id"], confirm=True)
        self.assertEqual(self.state(moved), "active")
        self.assertIn("RESTORE", [h["decision"] for h in self.history(moved)])

    def test_the_memory_just_written_is_never_moved_by_its_own_write(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="3"):
            last = None
            for i in range(10):
                last = self.store.remember(routine(i), "task")
        self.assertEqual(self.state(last), "active")

    def test_a_recalled_memory_outranks_an_unrecalled_twin(self) -> None:
        rows = []
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="10"):
            first = self.store.remember(routine(0), "task")
            for _ in range(3):
                self.store.find("cart east wing", 50)
            for i in range(1, 14):
                self.store.remember(routine(i), "task")
        self.assertEqual(self.store.health()["active_memories"], 10)
        self.assertEqual(self.state(first), "active")

    def test_gate_capacity_falls_back_to_the_shipped_setting(self) -> None:
        with self.flags(MEMORYSAFE_MAX_ACTIVE="7"):
            self.assertEqual(gates.gate_capacity(), 7)
        with self.flags(MEMORYSAFE_MAX_ACTIVE="7", MEMORYSAFE_GATE_MAX_ACTIVE="4"):
            self.assertEqual(gates.gate_capacity(), 4)
        self.assertIsNone(gates.gate_capacity())

    def test_no_capacity_means_the_gate_moves_nothing(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1"):
            self.fill(20)
        self.assertEqual(self.store.health()["active_memories"], 20)

    def test_capacity_gate_works_without_the_surprise_gate(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="8"):
            results = self.fill(20)
        all_events = [h for r in results for h in self.history(r)]
        self.assertFalse(any("Surprise gate" in h["reason"] for h in all_events))


class SurpriseGateTests(GateCase):
    def seed(self, n: int = 12) -> None:
        self.fill(n)

    def test_low_surprise_reworded_duplicate_is_merged_and_the_other_wording_is_kept(self) -> None:
        """Same content words in the same order, filler dropped: the shipped merge
        (character similarity < 0.88) misses it; the gate's content-word overlap catches it.
        (Changed in the order-sensitivity commit: this used to be a passive-voice reorder,
        "...was checked by Ana", which the gate now deliberately leaves unmerged.)"""
        self.seed()
        first = self.store.remember("Ana checked the oxygen cart in the east wing", "task")
        reworded = "Ana checked oxygen cart east wing today"
        shipped = self.store.remember(reworded, "task")
        self.assertEqual(shipped["decision"], "STORE")  # shipped behaviour: kept as a separate entry
        self.store.forget(shipped["memory_id"], confirm=True)
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember(reworded, "task")
        self.assertEqual(result["decision"], "MERGE")
        self.assertEqual(result["memory_id"], first["memory_id"])
        event = next(h for h in self.history(result) if "Surprise gate" in h["reason"])
        for phrase in ("low surprise", "novelty", "similarity", "Previous text"):
            self.assertIn(phrase, event["reason"])
        self.assertEqual(self.store.health()["active_memories"], 13)

    def test_unique_content_is_boosted_and_protected_with_a_reason(self) -> None:
        self.seed()
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember("Quarterly audit samples are stored in the greenhouse cabinet", "other")
        self.assertEqual(result["decision"], "PROTECT")
        self.assertTrue(result["protected"])
        self.assertEqual(result["importance"], 0.7)  # other 0.60 + 0.1
        self.assertIn("high surprise", self.history(result)[0]["reason"])
        self.assertIn("importance was raised to 0.70 and it was protected", self.history(result)[0]["reason"])

    def test_protection_is_budgeted_so_a_novel_stream_cannot_protect_everything(self) -> None:
        self.seed()
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1", MEMORYSAFE_GATE_MAX_ACTIVE="20", MEMORYSAFE_GATE_PROTECT_SHARE="0.1"):
            facts = ["Greenhouse cabinet holds quarterly audit samples", "Boiler pump hums whenever humidity peaks",
                     "Archive ladder belongs to Kestrel Optics", "Courier badge expires every midsummer",
                     "Tulip contract renews through Rotterdam broker"]
            results = [self.store.remember(f, "other") for f in facts]
        protected = [r for r in results if r["protected"]]
        self.assertEqual(len(protected), 2)  # 10% of 20, strict "<"
        refused = next(r for r in results if not r["protected"])
        self.assertIn("protected share of the store is full", self.history(refused)[0]["reason"])

    def test_changed_fact_is_left_to_the_lifecycle_check_not_merged(self) -> None:
        self.seed()
        self.store.remember("Dana lives in Montreal", "personal")
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember("Dana lives in Toronto", "personal")
        self.assertNotEqual(result["decision"], "MERGE")
        self.assertEqual(result["lifecycle"], "superseded_previous")

    def test_negation_is_not_merged_by_the_gate(self) -> None:
        """Unit-level: a pair that differs by "not" is passed on to the lifecycle check.
        (Note, outside the gate: the shipped character-similarity merge, at 0.88, does
        merge "has been checked" with "has not been checked". See README_GATES known limits.)"""
        from memorysafe_chatgpt.storage import _normalize

        text = "The east wing cart in the lobby has been checked by Ana"
        negated = "The east wing cart in the lobby has not been checked by Ana"
        rows = [{"id": "M0", "content": text, "normalized_content": _normalize(text)}] + [
            {"id": f"M{i}", "content": routine(i), "normalized_content": _normalize(routine(i))} for i in range(1, 12)]
        verdict = gates.assess_surprise(_normalize(negated), "task", negated, 0.7, rows)
        self.assertEqual(verdict["action"], "none")
        self.assertIn("changed fact", verdict["why"])
        plain = gates.assess_surprise(_normalize(text + " today"), "task", text + " today", 0.7, rows)
        self.assertEqual(plain["action"], "merge")

    def test_safety_and_decision_are_never_merged_by_the_gate(self) -> None:
        self.seed()
        self.store.remember("Bed 12 patient is allergic to penicillin and latex gloves", "safety")
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember("Bed 12 patient is allergic to penicillin and latex gloves too", "safety")
        self.assertFalse(any("Surprise gate" in h["reason"] and "merged" in h["reason"] for h in self.history(result)))

    def test_a_changed_version_or_date_is_never_downweighted(self) -> None:
        self.seed()
        self.store.remember("The payments service runs on release 2.3.1 in the east cluster", "project")
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember("The payments service runs on release 2.4.0 in the east cluster", "project")
        self.assertFalse(any("Surprise gate" in h["reason"] for h in self.history(result)))
        # Port to 0.4.19: shipped lifecycle now sends a version change without change
        # language to review (reviewer A review guards); either way it is left to lifecycle.
        self.assertIn(result["lifecycle"], {"superseded_previous", "needs_review"})

    def test_different_numbers_are_downweighted_not_merged(self) -> None:
        self.seed()
        self.store.remember("Ana checked the oxygen cart in the east wing on shift 4", "task")
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember("Ana checked the oxygen cart in the east wing on shift 9", "task")
        self.assertNotEqual(result["decision"], "MERGE")
        self.assertLessEqual(result["importance"], 0.5)
        self.assertFalse(result["protected"])
        self.assertIn("lower-importance", self.history(result)[0]["reason"])

    def test_an_explicit_importance_is_never_lowered(self) -> None:
        self.seed()
        self.store.remember("Ana checked the oxygen cart in the east wing on shift 4", "task")
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember("Ana checked the oxygen cart in the east wing on shift 9", "task", importance=0.9)
        self.assertEqual(result["importance"], 0.9)

    def test_a_young_store_is_left_alone(self) -> None:
        self.fill(4)
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            result = self.store.remember("A completely unrelated sentence about tulips and contracts", "other")
        self.assertFalse(any("Surprise gate" in h["reason"] for h in self.history(result)))

    def test_surprise_gate_works_without_the_capacity_gate(self) -> None:
        self.seed()
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            self.store.remember(routine(3) + " again", "task")
        self.assertEqual(self.store.health()["decision_counts"].get("EVICT", 0), 0)

    def test_assess_surprise_reports_evidence(self) -> None:
        rows = [{"id": f"M{i}", "content": routine(i), "normalized_content": MemoryStore.__module__ and
                 __import__("memorysafe_chatgpt.storage", fromlist=["_normalize"])._normalize(routine(i))} for i in range(12)]
        verdict = gates.assess_surprise("zebra quantum ledger", "other", "zebra quantum ledger", 0.6, rows)
        self.assertEqual(verdict["action"], "boost")
        self.assertEqual(verdict["novelty"], 1.0)


class ConsolidateTests(GateCase):
    def build(self, common: int = 25) -> dict:
        """A pinned item, a safety item, two rare items (one recalled), many common ones."""
        made = {
            "pinned": self.store.remember("Tuesday standup notes go in the blue folder", "task"),
            "safety": self.store.remember("Bed 12 patient is allergic to penicillin", "safety"),
            "rare": self.store.remember("The spare server room key is under the reception desk", "task"),
            "rare_recalled": self.store.remember("Vendor Kestrel Optics invoices through a founder's personal email", "task"),
        }
        self.store.protect(made["pinned"]["memory_id"], True, "test")
        for _ in range(2):
            self.store.find("Kestrel Optics invoices", 3)
        made["common"] = self.fill(common)
        return made

    def test_dry_run_changes_nothing_at_all(self) -> None:
        made = self.build()
        before_events, before_active = self.event_count(), self.store.health()["active_memories"]
        with self.flags(MEMORYSAFE_GATE_CONSOLIDATE="1"):
            report = gates.consolidate(self.store, apply=False, capacity=10, propose_merges=True)
        self.assertFalse(report["applied"])
        self.assertEqual(report["active_before"], before_active)
        self.assertEqual(report["active_after"], 10)
        self.assertEqual(len(report["move"]), before_active - 10)
        self.assertEqual(self.event_count(), before_events)
        self.assertEqual(self.store.health()["active_memories"], before_active)
        self.assertEqual(self.state(made["common"][0]), "active")

    def test_apply_keeps_priority_items_and_logs_each_move(self) -> None:
        made = self.build()
        with self.flags(MEMORYSAFE_GATE_CONSOLIDATE="1"):
            report = gates.consolidate(self.store, apply=True, capacity=10)
        self.assertEqual(self.store.health()["active_memories"], 10)
        for key in ("pinned", "safety", "rare", "rare_recalled"):
            self.assertEqual(self.state(made[key]), "active", key)
        moved_ids = {m["id"] for m in report["move"]}
        event = next(h for h in self.store.explain(next(iter(moved_ids)))["history"] if h["decision"] == "EVICT")
        self.assertEqual(event["source"], "gate:consolidate")
        self.assertIn("Consolidation", event["reason"])
        self.assertIn("Restore brings it back", event["reason"])
        self.assertEqual(self.row_count(), 29)

    def test_apply_is_reversible_with_restore(self) -> None:
        self.build()
        with self.flags(MEMORYSAFE_GATE_CONSOLIDATE="1"):
            report = gates.consolidate(self.store, apply=True, capacity=10)
        moved = report["move"][0]["id"]
        self.assertEqual(self.store.explain(moved)["state"], "evicted")
        self.store.restore(moved, confirm=True)
        self.assertEqual(self.store.explain(moved)["state"], "active")

    def test_replay_order_puts_protected_then_critical_then_the_rest_first(self) -> None:
        made = self.build()
        report = gates.consolidate(self.store, capacity=10)
        order = [item["id"] for item in report["keep"]]
        # Protected first (safety is auto-protected, so both of these are tier 0).
        self.assertEqual({made["pinned"]["memory_id"], made["safety"]["memory_id"]}, set(order[:2]))
        tiers = [item["tier"] for item in report["keep"]]
        self.assertEqual(tiers, sorted(tiers, reverse=True))  # highest keep tier first
        self.assertEqual(tiers[:2], [2, 2])
        self.assertEqual({made["rare"]["memory_id"], made["rare_recalled"]["memory_id"]}, set(order[2:4]))
        self.assertEqual(order[2], made["rare_recalled"]["memory_id"])  # recalled outranks unrecalled

    def test_protected_memories_are_kept_even_over_capacity(self) -> None:
        pinned = self.fill(6)
        for r in pinned:
            self.store.protect(r["memory_id"], True, "test")
        report = gates.consolidate(self.store, apply=True, capacity=3)
        self.assertTrue(report["protected_over_capacity"])
        self.assertEqual(report["move"], [])
        self.assertEqual(self.store.health()["active_memories"], 6)

    def test_merge_proposals_are_proposals_only(self) -> None:
        for text in ("Ana checked the oxygen cart in the east wing", "Ana checked the oxygen cart in the east wing today",
                     "Bruno checked the linen cart in the west wing"):
            self.store.remember(text, "task")
        self.fill(8)
        before_active = self.store.health()["active_memories"]
        report = gates.consolidate(self.store, apply=True, capacity=None, propose_merges=True)
        self.assertTrue(report["merge_proposals"])
        proposal = report["merge_proposals"][0]
        self.assertEqual(self.store.health()["active_memories"], before_active)  # nothing merged or moved
        events = self.store.explain(proposal["merge_in"])["history"]
        logged = [h for h in events if h["decision"] == "PROPOSE_MERGE"]
        self.assertEqual(len(logged), 1)
        self.assertIn("Nothing was changed", logged[0]["reason"])
        gates.consolidate(self.store, apply=True, capacity=None, propose_merges=True)  # idempotent
        events = self.store.explain(proposal["merge_in"])["history"]
        self.assertEqual(len([h for h in events if h["decision"] == "PROPOSE_MERGE"]), 1)

    def test_no_proposals_unless_asked(self) -> None:
        self.build()
        self.assertEqual(gates.consolidate(self.store, capacity=10)["merge_proposals"], [])

    def test_empty_store_is_fine(self) -> None:
        report = gates.consolidate(self.store, apply=True, capacity=5)
        self.assertEqual((report["active_before"], report["active_after"], report["move"]), (0, 0, []))


class ConsolidateCliTests(GateCase):
    def cli(self, *args: str) -> tuple[int, str]:
        from memorysafe_chatgpt.cli import main

        out, code = io.StringIO(), 0
        with patch("sys.argv", ["memorysafe", *args]), redirect_stdout(out):
            try:
                main()
            except SystemExit as exit_:
                code = int(exit_.code or 0)
        return code, out.getvalue()

    def test_command_is_refused_while_the_flag_is_off(self) -> None:
        self.fill(12)
        code, out = self.cli("consolidate", "--capacity", "5", "--apply")
        self.assertEqual(code, 2)
        self.assertIn("MEMORYSAFE_GATE_CONSOLIDATE=1", out)
        self.assertEqual(self.store.health()["active_memories"], 12)

    def test_dry_run_by_default_and_apply_acts(self) -> None:
        self.fill(12)
        with self.flags(MEMORYSAFE_GATE_CONSOLIDATE="1"):
            code, out = self.cli("consolidate", "--capacity", "5", "--propose-merges")
            self.assertEqual(code, 0)
            self.assertIn("dry run: nothing changed", out)
            self.assertEqual(self.store.health()["active_memories"], 12)
            code, out = self.cli("consolidate", "--capacity", "5", "--apply")
            self.assertIn("(applied)", out)
            self.assertEqual(self.store.health()["active_memories"], 5)
            code, out = self.cli("consolidate", "--capacity", "5", "--json")
            self.assertIn('"applied": false', out)


class ToyBenchmarkTests(unittest.TestCase):
    """The benchmark's own contract on a small stream. Not evidence about real memories."""

    def test_the_gates_keep_oddballs_that_a_fifo_window_loses(self) -> None:
        sys.path.insert(0, str(ROOT / "tests" / "bench"))
        try:
            import gates_toy_benchmark as bench
        finally:
            sys.path.pop(0)
        stream = bench.build_stream("worded", 150, 6, seed=1, odd_category="task")
        fifo = bench.run_fifo(stream, 20)
        gated = bench.run_store(stream, 20, {"MEMORYSAFE_GATE_CAPACITY": "1"})
        self.assertEqual(fifo["retained_oddballs"], 0)
        self.assertGreaterEqual(gated["retained_oddballs"], 5)
        self.assertEqual(gated["active"], 20)


if __name__ == "__main__":
    unittest.main()
