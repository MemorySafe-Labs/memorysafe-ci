"""Round-3 gate tests: the grouped selection book, its database check, the lazily corrected
neighbour counts and their cap, and the vocabulary caches. Same rules as test_gates.py: temp
stores, every gate flag cleared first, no live store, no port 8765.

    PYTHONPATH=src python -m pytest tests/test_gates_v3.py
    PYTHONPATH=src python -m unittest tests.test_gates_v3
"""

from __future__ import annotations

import random
import unittest
from unittest.mock import patch

from memorysafe_chatgpt import gates, storage
from memorysafe_chatgpt.storage import _normalize

from test_gates import GateCase, routine  # noqa: E402  (tests dir is on sys.path under both runners)
from test_gates_v2 import ranked_row  # noqa: E402


def random_rows(seed: int, n: int, start: int = 1) -> list[dict]:
    rng = random.Random(seed)
    vocab = [f"w{i}" for i in range(10)] + ["ana", "cart", "wing", "oxygen", "check"]
    return [
        ranked_row(start + i * 3, " ".join(rng.choices(vocab, k=rng.randint(1, 7))),
                   category=rng.choice(["task", "other", "project", "safety", "preference"]),
                   importance=rng.choice([0.5, 0.7, 0.85]), recalls=rng.choice([0, 0, 0, 1, 2, 7]),
                   protected=int(rng.random() < 0.1))
        for i in range(n)
    ]


def wanted(rows: list[dict], count: int, exclude: str | None = None) -> list[str]:
    full = [item for item in gates.rank_rows(rows) if item["tier"] < gates.PROTECTED_TIER and item["id"] != exclude]
    return [item["id"] for item in full][:count]


class BookTests(unittest.TestCase):
    def test_book_selection_equals_ranking_everything(self) -> None:
        for seed in range(15):
            rows = random_rows(seed, 60)
            tuples = gates._tuples(rows)
            book = gates.Book(tuples)
            index = gates.ActiveIndex()
            index.sync(tuples)
            for count in (1, 2, 5, 9):
                for exclude in (None, wanted(rows, 1)[0]):
                    got = [i["id"] for i in book.select(count, index, exclude)]
                    self.assertEqual(got, wanted(rows, count, exclude), (seed, count, exclude))

    def test_book_follows_adds_and_removes_and_matches_a_fresh_one(self) -> None:
        rng = random.Random(5)
        rows = random_rows(1, 50)
        tuples = gates._tuples(rows)
        book, index = gates.Book(tuples), gates.ActiveIndex()
        index.sync(tuples)
        live = {row[1]: row for row in tuples}
        newest = max(r[0] for r in tuples)
        for step in range(60):
            if rng.random() < 0.5 and len(live) > 8:
                gone = rng.choice(sorted(live))
                book.remove(gone)
                index.apply([], [gone])
                del live[gone]
            else:
                newest += rng.randint(1, 4)
                extra = ranked_row(newest, " ".join(rng.choices(["ana", "cart", "w1", "w2", "oxygen"], k=4)))
                row = gates._tuples([extra])[0]
                book.add(row)
                index.apply([row], [])
                live[row[1]] = row
            fresh = gates.Book(list(live.values()))
            self.assertEqual(book.fingerprint(), fresh.fingerprint(), step)
            self.assertEqual(book.rowids, fresh.rowids)
            fresh_index = gates.ActiveIndex()
            fresh_index.sync(list(live.values()))
            self.assertEqual(index.norm, fresh_index.norm)
            self.assertEqual(index.postings, fresh_index.postings)
            got = [i["id"] for i in book.select(3, index)]
            self.assertEqual(got, [i["id"] for i in fresh.select(3, fresh_index)], step)

    def test_fingerprint_predicts_rows_added_later(self) -> None:
        tuples = gates._tuples(random_rows(2, 20))
        newer = gates._tuples([ranked_row(1000, "lantern harbour"), ranked_row(1003, "jetty fog", recalls=3)])
        book = gates.Book(tuples)
        predicted = book.fingerprint(newer)
        for row in newer:
            book.add(row)
        self.assertEqual(predicted, book.fingerprint())

    def test_out_of_order_add_keeps_age_order(self) -> None:
        tuples = gates._tuples(random_rows(3, 12))
        book = gates.Book(tuples)
        book.add(gates._tuples([ranked_row(5, "restored older memory")])[0])
        self.assertEqual(book.rowids, sorted(book.rowids))


class DatabaseCheckTests(GateCase):
    def reads(self):
        """Count the full reads of the active rows made by the capacity gate."""
        original = gates._active_rows
        calls = []

        def counting(connection):
            calls.append(1)
            return original(connection)

        return patch.object(gates, "_active_rows", counting), calls

    def test_unchanged_store_is_not_read_again(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="12"):
            self.fill(14)  # builds the book on the first over-cap write
            patcher, calls = self.reads()
            with patcher:
                for i in range(14, 24):
                    self.store.remember(routine(i), "task")
            self.assertEqual(calls, [])
            self.assertEqual(self.store.health()["active_memories"], 12)

    def test_recall_protect_and_restore_force_a_full_read_and_give_the_same_answer(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="12"):
            made = self.fill(14)
            patcher, calls = self.reads()
            with patcher:
                self.store.find(routine(13), 2)           # recall count changes
                self.store.remember(routine(30), "task")
                after_recall = len(calls)
                self.store.protect(made[13]["memory_id"], True, "test")  # protection changes
                self.store.remember(routine(31), "task")
                after_protect = len(calls)
                evicted = next(m for m in made if self.state(m) == "evicted")
                self.store.restore(evicted["memory_id"], confirm=True)  # a row comes back with an old rowid
                self.store.remember(routine(32), "task")
            # round 13: the database change log re-reads exactly the changed rows, so no full read is needed
            self.assertEqual(len(calls), 0)
            with self.store._session() as connection:
                fresh = gates.Book(gates._active_rows(connection))
            book = gates.index_for(self.store).book
            self.assertEqual(book.rows, fresh.rows)
            self.assertEqual(book.rowids, fresh.rowids)
            self.assertLessEqual(self.store.health()["active_memories"], 13)
            # whatever the path, the answer is the full ranking's
            with self.store._session() as connection:
                rows = gates._active_rows(connection)
            self.assertEqual(
                [i["id"] for i in gates.select_lowest(rows, 3, gates.ActiveIndex())],
                [i["id"] for i in gates.rank_rows(rows) if i["tier"] < gates.PROTECTED_TIER][:3],
            )

    def test_a_forgotten_row_or_a_row_edited_to_another_length_is_noticed(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="12"):
            made = self.fill(14)
            patcher, calls = self.reads()
            with patcher, self.store._session() as connection:
                connection.execute("UPDATE memories SET normalized_content = ? WHERE id = ?",
                                   ("lantern harbour jetty fog", made[13]["memory_id"]))
            with patcher:
                self.store.remember(routine(40), "task")
            self.assertEqual(len(calls), 0)  # round 13: noticed through the change log, no full read
            self.assertIn(made[13]["memory_id"], gates.index_for(self.store).postings["lantern"])

    def test_fast_path_and_forced_full_reads_end_in_the_same_state(self) -> None:
        """A mixed script (writes, recalls, protects) run twice: once as it is, once with the
        snapshot thrown away before every write. Both must leave the same memories active."""

        def run(force_full: bool) -> set[tuple[str, str]]:
            import os, tempfile
            from pathlib import Path
            from memorysafe_chatgpt.storage import MemoryStore

            with tempfile.TemporaryDirectory() as temp:
                env = {"MEMORYSAFE_DB_PATH": str(Path(temp) / "d" / "m.sqlite3"), "MEMORYSAFE_STATE_DIR": str(Path(temp) / "s"),
                       "MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "15"}
                with patch.dict(os.environ, env):
                    gates._INDEXES.clear()
                    store = MemoryStore(Path(env["MEMORYSAFE_DB_PATH"]))
                    original = gates._current_book

                    def maybe_full(connection, index, *rest):
                        if force_full:
                            index.book = None
                        return original(connection, index, *rest)

                    rng = random.Random(4)
                    with patch.object(gates, "_current_book", maybe_full):
                        for i in range(70):
                            cat = rng.choice(["task", "task", "other", "preference"])
                            store.remember(routine(i) if i % 3 else f"Fact number {i} about harbour lantern {chr(97 + i % 20)}", cat)
                            if i % 7 == 0:
                                store.find(routine(i - 1), 2)
                    with store._session() as connection:
                        return {(r[0], r[1]) for r in connection.execute("SELECT content, state FROM memories")}

        self.assertEqual(run(False), run(True))

    def test_capacity_gate_moves_the_same_memories_as_a_cold_ranking(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="10"):
            results = self.fill(25)
        moved = {r["memory_id"] for r in results if self.state(r) == "evicted"}
        self.assertEqual(len(moved), 15)
        with self.flags(MEMORYSAFE_GATE_CONSOLIDATE="1"):
            gates._INDEXES.clear()
            plan = gates.consolidate(self.store, capacity=10)
        self.assertEqual({m["id"] for m in plan["move"]}, set())  # already at 10: nothing more to move
        self.assertEqual(plan["active_after"], 10)


class NeighbourCountTests(unittest.TestCase):
    def brute(self, index: gates.ActiveIndex, memory_id: str) -> int:
        mine = index.terms[memory_id]
        return sum(1 for other, theirs in index.terms.items()
                   if other != memory_id and gates.jaccard(mine, theirs) >= gates.CLUSTER_SIM)

    def test_counts_stop_at_the_cap_and_are_exact_below_it(self) -> None:
        rows = [ranked_row(i, f"routine round cart wing {w}") for i, w in enumerate(["a1", "b1", "c1", "d1"] * 30)]
        # words a1.. have a digit and are dropped, so all 120 rows share one term set
        index = gates.ActiveIndex()
        index.sync(rows)
        self.assertEqual(self.brute(index, "M0"), 119)
        self.assertEqual(index.neighbours("M0"), gates.CLUSTER_CAP - 1)
        small = gates.ActiveIndex()
        small.sync(rows[:20])
        self.assertEqual(small.neighbours("M0"), 19)
        item = gates.rank_rows(rows[:20], small)[0]
        self.assertEqual(item["similar"], 19)

    def test_rarity_never_goes_below_one_over_the_cap(self) -> None:
        rows = [ranked_row(i, "routine round cart wing") for i in range(100)]
        ranked = gates.rank_rows(rows)
        self.assertTrue(all(item["rarity"] >= round(1 / gates.CLUSTER_CAP, 3) - 1e-9 for item in ranked))
        self.assertEqual(ranked[0]["similar"], gates.CLUSTER_CAP - 1)

    def test_counts_corrected_from_the_change_log_equal_fresh_counts(self) -> None:
        rng = random.Random(9)
        words = ["ana", "cart", "wing", "oxygen", "lantern", "harbour", "jetty", "fog", "linen", "ward"]
        live = {f"M{i}": ranked_row(i, " ".join(rng.sample(words, rng.randint(3, 6)))) for i in range(80)}
        index = gates.ActiveIndex()
        index.sync(list(live.values()))
        for memory_id in live:
            index.neighbours(memory_id)
        nxt = 1000
        for step in range(40):
            for _ in range(rng.randint(1, 5)):  # add / drop / reword some, then ask for some counts
                roll = rng.random()
                if roll < 0.4 and len(live) > 20:
                    del live[rng.choice(sorted(live))]
                elif roll < 0.8:
                    nxt += 1
                    live[f"M{nxt}"] = ranked_row(nxt, " ".join(rng.sample(words, rng.randint(3, 6))))
                else:
                    key = rng.choice(sorted(live))
                    live[key] = ranked_row(live[key]["rowid"], " ".join(rng.sample(words, rng.randint(3, 6))))
                    live[key]["id"] = key
            index.sync(list(live.values()))
            for memory_id in rng.sample(sorted(live), 15):
                self.assertEqual(index.neighbours(memory_id), self.brute(index, memory_id), (step, memory_id))

    def test_a_count_older_than_the_change_log_is_recounted(self) -> None:
        index = gates.ActiveIndex()
        rows = [ranked_row(i, "lantern harbour jetty fog") for i in range(5)]
        index.sync(rows)
        self.assertEqual(index.neighbours("M0"), 4)
        more = rows + [ranked_row(100 + i, "lantern harbour jetty fog") for i in range(index.LOG_KEEP + 20)]
        index.sync(more)
        self.assertEqual(index.neighbours("M0"), min(gates.CLUSTER_CAP - 1, len(more) - 1))

    def test_a_full_count_that_loses_a_neighbour_is_recounted_not_decremented(self) -> None:
        rows = [ranked_row(i, "lantern harbour jetty fog") for i in range(gates.CLUSTER_CAP + 10)]
        index = gates.ActiveIndex()
        index.sync(rows)
        self.assertEqual(index.neighbours("M0"), gates.CLUSTER_CAP - 1)
        index.sync(rows[: gates.CLUSTER_CAP + 5])  # five gone, still far above the cap
        self.assertEqual(index.neighbours("M0"), gates.CLUSTER_CAP - 1)
        index.sync(rows[:10])
        self.assertEqual(index.neighbours("M0"), 9)


class VocabularyCacheTests(unittest.TestCase):
    def test_terms_and_templates_match_the_definitions_they_replace(self) -> None:
        samples = ["routine item 7 checked cart in room 27", "a b² c3 x", "the and of is", "submissao coracao incorporated patents",
                   "version 2 0 1 released", "ana thanked bruno on 3 march"]
        for text in samples:
            expected_terms = frozenset(
                storage._stem(t) for t in text.split()
                if t not in storage._STOPWORDS and len(t) > 1 and not any(c.isdigit() for c in t))
            self.assertEqual(gates.terms_of(text), expected_terms, text)
            self.assertEqual(" ".join(map(gates._TEMPLATE_MAP.__getitem__, text.split())), storage._template(text), text)


if __name__ == "__main__":
    unittest.main()
