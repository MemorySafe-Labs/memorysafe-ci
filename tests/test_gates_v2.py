"""Second batch of gate tests: order/meaning checks, rare-vs-important ranking, the
incremental index, and scale. Same rules as test_gates.py: temp stores, every gate flag
cleared first, no live store, no port 8765.

    PYTHONPATH=src python -m pytest tests/test_gates_v2.py
    PYTHONPATH=src python -m unittest tests.test_gates_v2
"""

from __future__ import annotations

import random
import sqlite3
import time
import unittest

from memorysafe_chatgpt import gates
from memorysafe_chatgpt.storage import _normalize

from test_gates import GateCase, routine  # noqa: E402  (tests dir is on sys.path under both runners)


def pool(extra: list[tuple[str, str]] = ()) -> list[dict]:
    """Twelve routine rows plus `extra` (id, text), shaped like the surprise gate's input."""
    rows = [{"id": f"R{i}", "content": routine(i), "normalized_content": _normalize(routine(i))} for i in range(12)]
    rows += [{"id": i, "content": t, "normalized_content": _normalize(t)} for i, t in extra]
    return rows


def assess(text: str, rows: list[dict], category: str = "task") -> dict:
    return gates.assess_surprise(_normalize(text), category, text, 0.7, rows)


def ranked_row(i: int, text: str, category: str = "task", importance: float = 0.7, recalls: int = 0,
               protected: int = 0) -> dict:
    return {"id": f"M{i}", "rowid": i, "normalized_content": _normalize(text), "category": category,
            "importance": importance, "protected": protected, "recall_count": recalls}


class OrderAndMeaningUnitTests(unittest.TestCase):
    def test_order_differs_for_swapped_actors_and_reversed_relations(self) -> None:
        for left, right in (
            ("Ana thanked Bruno", "Bruno thanked Ana"),
            ("Lindqvist trained Okonkwo on the new intake form", "Okonkwo trained Lindqvist on the new intake form"),
            ("Invoices go to finance before they go to the vendor", "Invoices go to the vendor before they go to finance"),
            ("Radiology refers patients to Cardiology", "Cardiology refers patients to Radiology"),
        ):
            self.assertTrue(gates.order_differs(_normalize(left), _normalize(right)), (left, right))

    def test_order_does_not_differ_for_the_same_order_with_filler_or_one_word_changed(self) -> None:
        base = _normalize("Ana checked the oxygen cart in the east wing")
        for other in ("Ana checked the oxygen cart in the east wing today", "Ana checked oxygen cart east wing",
                      "Ana checked the oxygen cart in the west wing", "Ana logged the oxygen cart in the east wing"):
            self.assertFalse(gates.order_differs(base, _normalize(other)), other)

    def test_negation_including_contractions_is_a_meaning_flip(self) -> None:
        self.assertTrue(gates.meaning_flipped(_normalize("The clinic accepts walk-in couriers"),
                                              _normalize("The clinic doesn't accept walk-in couriers")))
        self.assertTrue(gates.meaning_flipped(_normalize("Ana can sign orders"), _normalize("Ana can't sign orders")))
        self.assertTrue(gates.meaning_flipped(_normalize("It was checked"), _normalize("It was never checked")))
        self.assertFalse(gates.meaning_flipped(_normalize("It was checked"), _normalize("It was checked today")))
        self.assertFalse(gates.meaning_flipped(_normalize("It isn't checked"), _normalize("It isn't checked today")))

    def test_listed_opposites_are_a_meaning_flip(self) -> None:
        for left, right in (("The gate stays open at night", "The gate stays closed at night"),
                            ("Invoices go out before the audit", "Invoices go out after the audit"),
                            ("The alarm is enabled on weekends", "The alarm is disabled on weekends"),
                            ("The gate stays locked", "The gate stays unlocked")):
            self.assertTrue(gates.meaning_flipped(_normalize(left), _normalize(right)), (left, right))
        # a paraphrase opposite that is not in the list is a known gap
        self.assertFalse(gates.meaning_flipped(_normalize("The gate stays shut"), _normalize("The gate stays ajar")))


class SurpriseOrderTests(GateCase):
    def test_gate_does_not_merge_swapped_actors(self) -> None:
        verdict = assess("Okonkwo trained Lindqvist on the new intake form",
                         pool([("X", "Lindqvist trained Okonkwo on the new intake form")]))
        self.assertEqual(verdict["action"], "none")
        self.assertIn("different order", verdict["why"])

    def test_gate_does_not_merge_a_reversed_relation(self) -> None:
        verdict = assess("Invoices go to the vendor before they go to finance",
                         pool([("X", "Invoices go to finance before they go to the vendor")]))
        self.assertEqual(verdict["action"], "none")

    def test_gate_does_not_merge_a_contraction_negation(self) -> None:
        text = "The harbourview clinic accepts walk-in sample couriers from the northern district"
        verdict = assess(text.replace("accepts", "doesn't accept"), pool([("X", text)]))
        self.assertEqual(verdict["action"], "none")

    def test_gate_does_not_merge_a_listed_opposite(self) -> None:
        text = "The loading dock gate is open during the evening shift at the annex"
        verdict = assess(text.replace("open", "closed"), pool([("X", text)]))
        self.assertEqual(verdict["action"], "none")

    def test_a_same_order_reword_still_merges(self) -> None:
        text = "Ana checked the oxygen cart in the east wing"
        verdict = assess("Ana checked oxygen cart east wing today", pool([("X", text)]))
        self.assertEqual(verdict["action"], "merge")

    def test_a_passive_reorder_of_the_same_fact_is_no_longer_merged(self) -> None:
        """The cost of the order check, stated: same meaning in passive voice also differs
        in order, so it is stored as its own entry rather than folded in."""
        verdict = assess("In the east wing, the oxygen cart was checked by Ana, and logged",
                         pool([("X", "Ana checked the oxygen cart in the east wing")]))
        self.assertEqual(verdict["action"], "none")

    def test_end_to_end_swapped_actors_both_stay_active(self) -> None:
        self.fill(12)
        first = self.store.remember("Okonkwo trained Lindqvist on the new intake form", "task")
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
            second = self.store.remember("Lindqvist trained Okonkwo on the new intake form", "task")
        self.assertNotEqual(second["decision"], "MERGE")
        self.assertNotEqual(second["memory_id"], first["memory_id"])
        self.assertEqual(self.state(first), "active")
        self.assertEqual(self.state(second), "active")

    def test_gate_vetoes_the_shipped_near_duplicate_merge_for_flipped_meaning(self) -> None:
        """The shipped merge (character similarity) folds these pairs together; with the
        surprise gate on, the order / negation check vetoes it and both stay active."""
        pairs = [
            ("Lindqvist trained Okonkwo on the new intake form", "Okonkwo trained Lindqvist on the new intake form"),
            ("Harbourview clinic accepts walk-in sample couriers", "Harbourview clinic never accepts walk-in sample couriers"),
        ]
        for original, flipped in pairs:
            with self.subTest(original=original):
                self.fill(12)
                first = self.store.remember(original, "task")
                with self.flags(MEMORYSAFE_GATE_SURPRISE="1"):
                    second = self.store.remember(flipped, "task")
                self.assertNotEqual(second["decision"], "MERGE")
                self.assertNotEqual(second["memory_id"], first["memory_id"])
                self.assertEqual(self.state(first), "active")
                self.assertEqual(self.state(second), "active")
                # Port to 0.4.19: shipped merge_compatible() no longer folds these
                # pairs, so the gate's veto may not be needed (no "not merged" note).

    def test_without_the_flag_the_shipped_merge_is_unchanged(self) -> None:
        # Port to 0.4.19: shipped now keeps negation flips apart by itself
        # (lifecycle.merge_compatible). Gate off must equal that shipped behaviour.
        self.fill(12)
        first = self.store.remember("Harbourview clinic accepts walk-in sample couriers", "task")
        second = self.store.remember("Harbourview clinic never accepts walk-in sample couriers", "task")
        self.assertNotEqual(second["decision"], "MERGE")
        self.assertNotEqual(second["memory_id"], first["memory_id"])

    def test_checks_are_inert_when_the_gate_is_off(self) -> None:
        self.fill(12)
        first = self.store.remember("Okonkwo trained Lindqvist on the new intake form", "task")
        second = self.store.remember("Lindqvist trained Okonkwo on the new intake form", "task")
        self.assertFalse(any("Surprise gate" in h["reason"] for h in self.history(second)))
        self.assertEqual(self.state(first), "active")


class RareVersusImportantTests(GateCase):
    def test_a_repeatedly_recalled_fact_outranks_a_rarer_throwaway(self) -> None:
        rows = [ranked_row(i, routine(i)) for i in range(20)]
        rows.append(ranked_row(20, "Zebra quantum ledger sparkles", category="other", importance=0.6))  # throwaway
        # a useful fact that looks like 12 others (so it is common) but has been recalled 3 times
        rows += [ranked_row(21 + i, f"Weekly stock count on shelf {i % 3} done by Ana") for i in range(12)]
        rows[21] = ranked_row(21, "Weekly stock count on shelf 0 done by Ana", recalls=3)
        order = [item["id"] for item in gates.rank_rows(rows)]
        self.assertLess(order.index("M20"), order.index("M21"))  # throwaway is lower keep-priority
        ranked = {item["id"]: item for item in gates.rank_rows(rows)}
        self.assertEqual(ranked["M21"]["tier"], 1)
        self.assertEqual(ranked["M20"]["tier"], 0)
        self.assertGreater(ranked["M20"]["rarity"], ranked["M21"]["rarity"])  # rarer, yet ranked lower

    def test_with_equal_rarity_a_durable_category_outranks_other(self) -> None:
        text_a, text_b = "Quarterly samples sit in the greenhouse cabinet", "Amber harbour lantern flickers nightly"
        rows = [ranked_row(i, routine(i)) for i in range(10)]
        rows += [ranked_row(10, text_a, category="project", importance=0.7), ranked_row(11, text_b, category="other", importance=0.7)]
        ranked = {item["id"]: item for item in gates.rank_rows(rows)}
        self.assertEqual(ranked["M10"]["rarity"], ranked["M11"]["rarity"])
        self.assertGreater(ranked["M10"]["score"], ranked["M11"]["score"])

    def test_recency_breaks_a_tie_and_protected_is_always_first(self) -> None:
        rows = [ranked_row(i, routine(i)) for i in range(10)]
        rows[0] = ranked_row(0, routine(0), protected=1, importance=0.1)
        order = gates.rank_rows(rows)
        self.assertEqual(order[-1]["id"], "M0")  # highest keep priority, whatever its other numbers
        ordinary = [item for item in order if item["tier"] == 0]
        self.assertLess(ordinary[0]["rowid"], ordinary[-1]["rowid"])  # near-identical rows: oldest goes first

    def test_a_recalled_rare_fact_is_not_demoted_behind_commons(self) -> None:
        rows = [ranked_row(i, routine(i)) for i in range(30)]
        rows.append(ranked_row(30, "The spare server room key is under the reception desk", recalls=1))
        self.assertEqual(gates.rank_rows(rows)[-1]["id"], "M30")

    def test_age_fade_is_off_by_default_and_fades_only_the_unrecalled(self) -> None:
        rows = [ranked_row(0, "The spare server room key is under the reception desk"),
                ranked_row(1, "Vendor Kestrel invoices through a founder's personal email", recalls=1)]
        rows += [ranked_row(i, routine(i)) for i in range(2, 22)]
        rows += [ranked_row(100 + i, routine(i + 40)) for i in range(60)]  # many newer writes
        before = {i["id"]: i["score"] for i in gates.rank_rows(rows)}
        again = {i["id"]: i["score"] for i in gates.rank_rows(rows)}
        self.assertEqual(before, again)
        with self.flags(MEMORYSAFE_GATE_AGE_GRACE="0.5"):
            faded = {i["id"]: i["score"] for i in gates.rank_rows(rows)}
        self.assertLess(faded["M0"], before["M0"])  # never recalled, old: bonus gone
        self.assertEqual(faded["M1"], before["M1"])  # recalled: keeps it
        self.assertEqual(faded["M159"], before["M159"])  # the newest row is unaffected

    def test_end_to_end_consolidation_keeps_the_repeated_fact_ahead_of_the_throwaway(self) -> None:
        throwaway = self.store.remember("Zebra quantum ledger sparkles over the harbour", "other")
        useful = [self.store.remember(f"Weekly stock count on shelf {i % 3} done by Ana on round {i}", "task") for i in range(8)]
        for _ in range(3):
            top = self.store.find("Weekly stock count shelf 0 done by Ana", 1)[0]["memory_id"]
        self.fill(14)
        report = gates.consolidate(self.store, capacity=2)
        self.assertEqual(report["keep"][0]["id"], top)
        self.assertGreaterEqual(report["keep"][0]["recalls"], gates.REPEAT_RECALLS)
        kept = [item["id"] for item in report["keep"]]
        self.assertNotIn(throwaway["memory_id"], kept[:1])
        self.assertIn(top, {r["memory_id"] for r in useful})

    def test_surprise_protection_can_be_switched_off_without_losing_the_importance_boost(self) -> None:
        self.fill(12)
        with self.flags(MEMORYSAFE_GATE_SURPRISE="1", MEMORYSAFE_GATE_SURPRISE_PROTECT="0"):
            result = self.store.remember("Quarterly audit samples are stored in the greenhouse cabinet", "other")
        self.assertEqual(result["decision"], "STORE")
        self.assertFalse(result["protected"])
        self.assertEqual(result["importance"], 0.7)
        self.assertIn("protection by novelty is switched off", self.history(result)[0]["reason"])


class IndexAndSelectionTests(unittest.TestCase):
    @staticmethod
    def random_rows(seed: int, n: int) -> list[dict]:
        rng = random.Random(seed)
        vocab = [f"w{i}" for i in range(14)] + ["ana", "cart", "wing", "oxygen", "check"]
        rows = []
        for i in range(n):
            text = " ".join(rng.choices(vocab, k=rng.randint(1, 8)))
            rows.append(ranked_row(i * 3 + 1, text, category=rng.choice(["task", "other", "project", "safety", "preference"]),
                                   importance=round(rng.uniform(0.3, 0.95), 2), recalls=rng.choice([0, 0, 0, 1, 2, 5]),
                                   protected=int(rng.random() < 0.1)))
        return rows

    def test_neighbour_counts_equal_brute_force(self) -> None:
        for seed in range(6):
            rows = self.random_rows(seed, 60)
            index = gates.ActiveIndex()
            index.sync(rows)
            for row in rows:
                mine = index.terms[row["id"]]
                expected = sum(1 for other in rows if other["id"] != row["id"]
                               and gates.jaccard(mine, gates.terms_of(other["normalized_content"])) >= gates.CLUSTER_SIM)
                self.assertEqual(index.neighbours(row["id"]), expected, (seed, row["id"]))

    def test_lazy_selection_equals_ranking_everything(self) -> None:
        for seed in range(12):
            rows = self.random_rows(seed, 50)
            full = [item for item in gates.rank_rows(rows) if item["tier"] < gates.PROTECTED_TIER]
            for count in (1, 3, 7):
                for exclude in (None, full[0]["id"]):
                    wanted = [item["id"] for item in full if item["id"] != exclude][:count]
                    got = [item["id"] for item in gates.select_lowest(rows, count, gates.ActiveIndex(), exclude)]
                    self.assertEqual(got, wanted, (seed, count, exclude))

    def test_lazy_selection_handles_more_requested_than_available_and_all_protected(self) -> None:
        rows = self.random_rows(3, 10)
        for row in rows:
            row["protected"] = 1
        self.assertEqual(gates.select_lowest(rows, 4, gates.ActiveIndex()), [])
        rows = self.random_rows(4, 6)
        self.assertEqual(len(gates.select_lowest(rows, 99, gates.ActiveIndex())), len([r for r in rows if not r["protected"]]))

    def test_index_follows_changes_by_diffing_and_matches_a_fresh_one(self) -> None:
        rows = self.random_rows(7, 40)
        index = gates.ActiveIndex()
        index.sync(rows)
        for row in rows[:25]:
            index.neighbours(row["id"])  # fill the remembered counts, which must then follow the changes
        changed = [dict(r) for r in rows[5:]]  # five rows gone
        changed[0]["normalized_content"] = "ana cart wing oxygen"  # one reworded in place
        changed.append(ranked_row(999, "brand new entry about lanterns"))
        index.sync(changed)
        fresh = gates.ActiveIndex()
        fresh.sync(changed)
        self.assertEqual(index.norm, fresh.norm)
        self.assertEqual(index.postings, fresh.postings)
        self.assertEqual(+index.templates, +fresh.templates)
        for row in changed:
            self.assertEqual(index.neighbours(row["id"]), fresh.neighbours(row["id"]))
        self.assertEqual({k: v for k, v in index.memo.items()}, {k: fresh.neighbours(k) for k in index.memo})

    def test_indexes_are_kept_per_database_and_bounded(self) -> None:
        class Fake:
            def __init__(self, path: str):
                self.database_path = path

        first = gates.index_for(Fake("/tmp/a.sqlite3"))
        self.assertIs(first, gates.index_for(Fake("/tmp/a.sqlite3")))
        for i in range(10):
            gates.index_for(Fake(f"/tmp/other{i}.sqlite3"))
        self.assertLessEqual(len(gates._INDEXES), gates._INDEX_KEEP)


class StoreCoherenceTests(GateCase):
    def test_the_cached_index_survives_changes_made_behind_its_back(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="15"):
            made = self.fill(25)
            # another process: forgets one memory, evicts another by hand, rewords a third
            with self.store._session() as connection:
                connection.execute("UPDATE memories SET state = 'evicted' WHERE id = ?", (made[24]["memory_id"],))
                connection.execute("DELETE FROM memories WHERE id = ?", (made[23]["memory_id"],))
            more = [self.store.remember(f"Lantern inventory entry {chr(97 + i)} for the harbour office", "task") for i in range(6)]
        self.assertEqual(self.store.health()["active_memories"], 15)
        self.assertTrue(all(self.state(r) in {"active", "evicted"} for r in more))


    def test_a_text_edited_in_place_is_noticed_from_the_database_rows(self) -> None:
        made = self.fill(14)
        index = gates.ActiveIndex()
        with self.store._session() as connection:
            index.sync(gates._active_rows(connection))
            target = made[3]["memory_id"]
            connection.execute("UPDATE memories SET normalized_content = ? WHERE id = ?",
                               ("lantern harbour jetty fog", target))
            index.sync(gates._active_rows(connection))
            fresh = gates.ActiveIndex()
            fresh.sync(gates._active_rows(connection))
        self.assertEqual(index.norm[target], "lantern harbour jetty fog")
        self.assertIn(target, index.postings["lantern"])
        self.assertEqual(index.postings, fresh.postings)

    def test_protected_rows_are_never_moved_when_protection_changes_behind_the_index(self) -> None:
        made = self.fill(14)
        with self.flags(MEMORYSAFE_GATE_CONSOLIDATE="1"):
            gates.consolidate(self.store, capacity=5)  # dry run: builds the cached index
        self.store.protect(made[0]["memory_id"], True, "test")  # protection changes, the text does not
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="10"):
            for i in range(8):
                self.store.remember(f"Lantern inventory entry {i} for the harbour office", "task")
        self.assertEqual(self.state(made[0]), "active")


class ScaleTests(GateCase):
    """Behaviour and time with thousands of memories, rows inserted directly (fast)."""

    def load(self, n: int) -> None:
        rng = random.Random(5)
        names, things, wings, verbs = (["Ana", "Bruno", "Chen", "Dalia", "Emre", "Farah"], ["oxygen", "linen", "supply", "crash"],
                                       ["east", "west", "north", "south", "annex"], ["checked", "restocked", "counted", "audited"])
        with self.store._session() as connection:
            for i in range(n):
                if i % 50 == 7:
                    text = f"Unique remark {i} about lantern harbour {rng.randrange(10**6)} jetty"
                else:
                    text = f"Routine round: {rng.choice(names)} {rng.choice(verbs)} the {rng.choice(things)} cart in the {rng.choice(wings)} wing {i}"
                normalized = _normalize(text)
                connection.execute(
                    """INSERT INTO memories(id, content, normalized_content, category, importance, confidence,
                           protected, state, created_at, updated_at, source, recall_count)
                       VALUES (?, ?, ?, 'task', 0.7, 0.9, ?, 'active', '2026-01-01T00:00:00+00:00',
                           ?, 'manual', ?)""",
                    (f"MS-T-{i:06d}", text, normalized, int(i % 400 == 3), f"2026-01-01T00:{i // 3600 % 60:02d}:{i % 60:02d}+00:00",
                     3 if i % 97 == 0 else 0),
                )

    def test_capacity_gate_at_3000_memories_is_exact_and_not_slow(self) -> None:
        self.load(3000)
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="3000"):
            started = time.perf_counter()
            for i in range(5):
                self.store.remember(f"New harbour lantern remark number {i} about the jetty fog", "task")
            per_write = (time.perf_counter() - started) / 5
        self.assertEqual(self.store.health()["active_memories"], 3000)
        self.assertLess(per_write, 1.5, "generous bound; the benchmark reports the real figure")
        with self.store._session() as connection:
            protected_moved = connection.execute(
                "SELECT COUNT(*) FROM memories WHERE protected = 1 AND state != 'active'").fetchone()[0]
            moved = connection.execute("SELECT COUNT(*) FROM memories WHERE state = 'evicted'").fetchone()[0]
        self.assertEqual(protected_moved, 0)
        self.assertEqual(moved, 5)

    def test_selection_at_3000_equals_full_ranking(self) -> None:
        self.load(3000)
        with self.store._session() as connection:
            rows = gates._active_rows(connection)
            full = [i["id"] for i in gates.rank_rows(rows) if i["tier"] < gates.PROTECTED_TIER][:4]
            got = [i["id"] for i in gates.select_lowest(rows, 4, gates.ActiveIndex())]
        self.assertEqual(got, full)


if __name__ == "__main__":
    unittest.main()
