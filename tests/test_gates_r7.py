"""Round-7 gate tests (all flags off by default; temp stores only; no live store, no port 8765).

Covers: the lazy usefulness ranking gives exactly the reference answer; hazard words and the
per-goal goal test (including the passing-remark and common-word exclusions); the extinction
ordering rule for PROTECTED memories (nothing about the protected memory changes; only the
newer evidence is listed first; withdrawn by keep_both / revert / restore); the surprise gate
no longer cancels extinction; no action on age alone; and that round 7 writes no file.

    PYTHONPATH=src python -m pytest tests/test_gates_r7.py
"""

from __future__ import annotations

import os
import random
from datetime import timedelta
from pathlib import Path

from memorysafe_chatgpt import gates, gates_r6

from test_gates import routine
from test_gates_r6 import R6Case

USEFUL = {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_USEFUL": "1"}
EXT = {"MEMORYSAFE_GATE_EXTINCTION": "1"}


class R7Case(R6Case):
    def setUp(self) -> None:
        super().setUp()
        gates_r6._CACHES.clear()

    def protected(self, memory_id: str) -> tuple:
        with self.store._session() as c:
            r = c.execute("SELECT state, protected, importance, confidence, content FROM memories WHERE id = ?",
                          (memory_id,)).fetchone()
            return tuple(r)


class LazyRankingTests(R7Case):
    TEXTS = [
        "{n} lives in Porto", "{n} moved to Leeds", "{n} works as a data analyst", "{n} works at Contoso Labs",
        "The Atlas deadline is March 3 2027", "Goal: {n} wants to run the Lisbon marathon in October",
        "Registration for the Lisbon marathon closes on June 12", "{n} read an article about the Lisbon marathon",
        "{n} is severely allergic to peanuts", "{n} said it has been a long week", "Reviewed pull request {k} for the atlas repo",
        "Our primary database is MongoDB", "We migrated the primary database from MongoDB to Postgres",
        "{n} prefers window seats", "{n} laughed at the pun in the draft", "The {k} invoice was sent",
    ]

    def test_lazy_equals_reference_on_random_stores(self) -> None:
        rng = random.Random(7)
        names = ["Maya", "Jonas", "Priya", "Omar"]
        for trial in range(3):
            with self.flags(**USEFUL, **EXT, MEMORYSAFE_GATE_MAX_ACTIVE="40"):
                for i in range(90):
                    text = rng.choice(self.TEXTS).format(n=rng.choice(names), k=rng.randint(100, 999))
                    self.store.remember(text, rng.choice(["task", "other", "personal", "project"]))
                    if i % 7 == 0:
                        self.store.find(rng.choice(["where does Maya live", "the Lisbon marathon", "database"]), 3)
                    if i % 10 == 9:
                        with self.store._session() as c:
                            lazy = [x["id"] for x in gates_r6.rank_useful(self.store, c, 8, 40)]
                            full = gates_r6.rank_useful_full(self.store, c, 8, 40)
                        self.assertEqual(lazy, full, f"trial {trial} write {i}")


class HazardAndGoalTests(R7Case):
    def feat(self, text: str) -> tuple:
        from memorysafe_chatgpt import storage

        return gates_r6.features(text, storage._normalize(text))

    def test_hazard_words(self) -> None:
        self.assertTrue(self.feat("Aiko is severely allergic to bee stings and carries an epinephrine pen")[4])
        self.assertTrue(self.feat("Never give Omar ibuprofen, it is contraindicated with his medication")[4])
        self.assertFalse(self.feat("Aiko read an article about how peanuts are grown")[4])

    def test_hazard_is_a_sign_of_use(self) -> None:
        p = gates_r6.params()
        row = (1, "x", "", "other", 0.6, 0, 0)
        plain = gates_r6._usefulness(p, row, False, self.feat("Aiko had a sandwich"), False, 0)
        hazard = gates_r6._usefulness(p, row, False, self.feat("Aiko is allergic to sesame"), False, 0)
        self.assertFalse(plain[1])
        self.assertTrue(hazard[1])
        self.assertGreater(hazard[0], plain[0])

    def test_goal_relevance_is_per_goal_and_skips_remarks(self) -> None:
        p = {**gates_r6.params(), "GOAL_MIN_SHARED": 2.0, "GOAL_INCIDENTAL": 1.0}
        goal = self.feat("Goal: Maya wants to run the Lisbon marathon in October")[3] - gates.terms_of("goal wants")
        profiles = [goal]
        self.assertTrue(gates_r6._goal_related(self.feat("Registration for the Lisbon marathon closes on June 12"), profiles, p))
        self.assertFalse(gates_r6._goal_related(self.feat("Maya read an article about the Lisbon marathon"), profiles, p))
        self.assertFalse(gates_r6._goal_related(self.feat("Maya found an old photo from Lisbon"), profiles, p))
        off = {**p, "GOAL_INCIDENTAL": 0.0}
        self.assertTrue(gates_r6._goal_related(self.feat("Maya read an article about the Lisbon marathon"), profiles, off))
        # two goals' words do not combine (round 6 pooled every goal's words)
        two = [gates.terms_of("lisbon marathon october"), gates.terms_of("japanese visa tokyo")]
        self.assertFalse(gates_r6._goal_related(self.feat("A Tokyo marathon poster"), two, p))

    def test_common_words_do_not_count(self) -> None:
        with self.flags(**USEFUL, MEMORYSAFE_GATE_MAX_ACTIVE="500"):
            for i in range(30):
                self.store.remember(f"Maya said the weather was nice on day {i} of the trip", "other")
            g = self.store.remember("Goal: Maya wants to run the Lisbon marathon in October", "personal")["memory_id"]
            with self.store._session() as c:
                fc = gates_r6._feature_cache(self.store)
                book = gates_r6._snapshot(c, fc, None)
                profiles = gates_r6._goal_profiles(fc, c, book.rows, gates_r6.params())
        self.assertEqual(len(profiles), 1)
        self.assertFalse(gates.terms_of("maya") & profiles[0])
        self.assertTrue(gates.terms_of("lisbon marathon") <= profiles[0])
        self.assertTrue(g)


class ProtectedOrderingTests(R7Case):
    OLD = "Our primary database is MongoDB"
    NEW = "We migrated the primary database from MongoDB to Postgres"
    Q = "which database is the primary one"

    def seed(self) -> None:
        for i in range(12):
            self.store.remember(routine(i), "task")

    def test_protected_memory_unchanged_but_newer_evidence_listed_first(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember(self.OLD, "task")["memory_id"]
            self.store.protect(old)
            before = self.protected(old)
            new = self.store.remember(self.NEW, "task")["memory_id"]
            self.assertEqual(self.protected(old), before)          # state, protection, importance, text: unchanged
            self.assertEqual(self.rel(old, "weakened"), 0)
            self.assertEqual(self.rel(old, "proposed"), 1)
            self.assertTrue(self.events(old, "PROPOSE_WEAKEN"))
            self.assertLess(self.rank(self.Q, new), self.rank(self.Q, old))
            self.assertIsNotNone(self.rank(self.Q, old))           # still returned
            with self.store._session() as c:
                self.assertNotIn(old, gates_r6.weak_strengths(c))  # keep order untouched too
        with self.flags(MEMORYSAFE_GATE_EXTINCTION="0"):
            self.assertLess(self.rank(self.Q, old), self.rank(self.Q, new))  # flag off: shipped order

    def test_order_rule_off_by_parameter(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember(self.OLD, "task")["memory_id"]
            self.store.protect(old)
            new = self.store.remember(self.NEW, "task")["memory_id"]
            with self.flags(MEMORYSAFE_GATE_R6_ORDER_RULE="0"):
                self.assertLess(self.rank(self.Q, old), self.rank(self.Q, new))

    def test_restore_withdraws_the_proposal(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember(self.OLD, "task")["memory_id"]
            self.store.protect(old)
            new = self.store.remember(self.NEW, "task")["memory_id"]
            self.store.restore(old, confirm=True)
            self.assertEqual(self.rel(old, "proposed"), 0)
            self.assertEqual(self.rel(old, "undone"), 1)
            self.assertLess(self.rank(self.Q, old), self.rank(self.Q, new))

    def test_lower_confidence_update_does_not_reorder(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember(self.OLD, "task")["memory_id"]
            self.store.protect(old)
            with self.store._session() as c:
                c.execute("UPDATE memories SET confidence = 0.99 WHERE id = ?", (old,))
            new = self.store.remember(self.NEW, "task")["memory_id"]
            self.assertEqual(self.rel(old, "proposed"), 0)   # evidence needs equal or higher confidence
            self.assertLess(self.rank(self.Q, old), self.rank(self.Q, new))


class SurpriseInteractionTests(R7Case):
    def test_novelty_protected_stale_fact_ranks_below_its_replacement(self) -> None:
        flags = {**EXT, "MEMORYSAFE_GATE_SURPRISE": "1", "MEMORYSAFE_GATE_SURPRISE_MIN_ACTIVE": "5",
                 "MEMORYSAFE_GATE_PROTECT_SHARE": "0.9"}
        with self.flags(**flags):
            for i in range(12):
                self.store.remember(routine(i), "task")
            old = self.store.remember("Our primary database is MongoDB", "project")["memory_id"]
            new = self.store.remember("We migrated the primary database from MongoDB to Postgres", "project")["memory_id"]
            self.assertEqual(self.protected(old)[1], 1)   # protected by the surprise gate for novelty (the r6 failure case)
            self.assertEqual(self.rel(old, "proposed"), 1)
            self.assertLess(self.rank("which database is the primary one", new),
                            self.rank("which database is the primary one", old))


class NoAgeAndNoFileTests(R7Case):
    def test_no_action_on_age_alone_with_every_flag(self) -> None:
        flags = {**USEFUL, **EXT, "MEMORYSAFE_GATE_SURPRISE": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "500"}
        with self.flags(**flags):
            old = self.store.remember("Our primary database is MongoDB", "project")["memory_id"]
            self.t += timedelta(days=4000)
            for i in range(40):
                self.store.remember(routine(i), "task")
                self.t += timedelta(days=30)
        self.assertEqual(self.rel(old, "weakened") + self.rel(old, "proposed"), 0)
        self.assertEqual(self.st(old), "active")

    def test_round7_writes_no_file(self) -> None:
        flags = {**USEFUL, **EXT, "MEMORYSAFE_GATE_MAX_ACTIVE": "20", "MEMORYSAFE_GATE_PERSIST": "0"}
        with self.flags(**flags):
            for i in range(60):
                self.store.remember(routine(i) + f" note {i % 7}", "task")
                self.store.remember(f"Goal: finish the audit number {i % 3}", "other")
            gates.save_indexes(force=True)
        files = {p.name for p in Path(self.db).parent.iterdir()}
        allowed = {self.db.name, self.db.name + "-wal", self.db.name + "-shm", self.db.name + "-journal"}
        self.assertTrue(files <= allowed, files - allowed)
