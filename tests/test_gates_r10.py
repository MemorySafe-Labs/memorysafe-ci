"""Round 10 (MEMORYSAFE_GATE_STALE): narrowed supersession evidence, keep-both respected,
"possibly stale" flags for status claims only. Prototype, default off."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates, gates_r6, gates_stale, storage
from memorysafe_chatgpt.storage import MemoryStore

ON = {"MEMORYSAFE_GATE_STALE": "1", "MEMORYSAFE_GATE_EXTINCTION": "1"}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now.isoformat(timespec="seconds")


class R10Base(unittest.TestCase):
    env: dict = ON

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = {"MEMORYSAFE_DB_PATH": str(Path(self.temp.name) / "m.sqlite3"),
                "MEMORYSAFE_STATE_DIR": str(Path(self.temp.name) / "s")}
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
        self.clock = Clock()
        self.p_env = patch.dict(os.environ, {**clean, **base, **self.env}, clear=True)
        self.p_now = patch.object(storage, "_now", self.clock)
        self.p_env.start()
        self.p_now.start()
        gates._INDEXES.clear()
        self.store = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"]))

    def tearDown(self):
        self.p_now.stop()
        self.p_env.stop()
        self.temp.cleanup()

    def later(self, days):
        self.clock.now += timedelta(days=days)

    def relations(self, decision):
        with self.store._session() as c:
            return [tuple(r) for r in c.execute(
                "SELECT old_memory_id, new_memory_id, status FROM memory_relations WHERE decision = ?", (decision,))]

    def filler(self, n=3):
        for i in range(n):
            self.store.remember(f"Lunch note number {i}: the soup at the food hall was fine", "other")


class DurableFactsNeverAgeTests(R10Base):
    def test_durable_fact_is_never_flagged_for_age(self):
        fact = self.store.remember("Maya is allergic to penicillin and must never be given it", "personal")["memory_id"]
        birthday = self.store.remember("Jonas Okonkwo's birthday is 14 March", "personal")["memory_id"]
        for _ in range(8):
            self.later(60)
            self.filler(1)
        with self.store._session() as c:
            flagged = gates_stale.stale_ids(c)
        self.assertNotIn(fact, flagged)
        self.assertNotIn(birthday, flagged)
        with self.store._session() as c:
            self.assertFalse({fact, birthday} & gates_stale.stale_ids(c))
        top = self.store.find("is Maya allergic to penicillin", 3)
        self.assertEqual(top[0]["memory_id"], fact)
        self.assertFalse(top[0]["possibly_stale"])

    def test_status_claim_is_flagged_after_stale_days_and_demoted_only(self):
        status = self.store.remember("The Quillfeather contract is still unsigned as of today", "project")["memory_id"]
        self.later(45)
        self.filler(1)
        with self.store._session() as c:
            self.assertIn(status, gates_stale.stale_ids(c))
            state = c.execute("SELECT state FROM memories WHERE id = ?", (status,)).fetchone()[0]
            reason = c.execute("SELECT reason FROM decision_events WHERE decision = 'STALE_FLAG' AND memory_id = ?",
                               (status,)).fetchone()[0]
        self.assertEqual(state, "active")             # flagged, not extinguished or deleted
        self.assertIn("not reconfirmed", reason)      # explainable
        hit = self.store.find("Quillfeather contract signed", 3)
        self.assertTrue(hit and hit[0]["possibly_stale"])

    def test_reconfirmation_voids_the_flag(self):
        text = "The Quillfeather contract is still unsigned as of today"
        status = self.store.remember(text, "project")["memory_id"]
        self.later(45)
        self.filler(1)
        self.later(1)
        self.store.remember(text, "project")   # said again: exact duplicate refreshes it
        with self.store._session() as c:
            self.assertNotIn(status, gates_stale.stale_ids(c))


class KeepBothRespectedTests(R10Base):
    def test_keep_both_pair_is_never_treated_as_superseding(self):
        from memorysafe_chatgpt.lifecycle import record_relation

        a = self.store.remember("Brightwater delivery for the Atlas migration is pending", "project")["memory_id"]
        self.later(1)
        b = self.store.remember("Atlas migration now uses Brightwater for the backup delivery instead", "project")["memory_id"]
        with self.store._session() as c:
            c.execute("UPDATE memory_relations SET status = 'undone' WHERE old_memory_id = ?", (a,))
            record_relation(c, old_memory_id=a, new_memory_id=b, decision="KEEP_BOTH", status="resolved",
                            reason="test", evidence="test", confidence=1.0, source="manual", created_at=self.clock())
            result = gates_r6._apply(self.store, c, a, b, [("a2 replacement", "test")], self.clock())
        self.assertFalse(result["applied"])
        self.assertTrue(result.get("keep_both"))
        self.later(20)
        with self.store._session() as c:
            others = c.execute("SELECT id, content, confidence FROM memories WHERE state = 'active'").fetchall()
            gates_stale.stale_after_write(self.store, c, b, "Atlas migration now uses Brightwater for the backup delivery",
                                       others, self.clock())
        flags = [(o, n) for o, n, _s in self.relations("STALE_FLAG")]
        self.assertNotIn((a, b), flags)
        weak = [(o, n) for o, n, s in self.relations("WEAKEN") if s == "weakened"]
        self.assertNotIn((a, b), weak)


class NarrowedTriggerTests(R10Base):
    def test_shared_dates_are_not_a_subject(self):
        old = "Peyton Moxam sent the mutual NDA on 2 September and Dana has not signed it yet"
        new = "Hugo Sereys rescheduled the GTM call from 2 September and Dana will pick a new slot in September"
        self.assertIsNone(gates_r6.replacement_evidence(new, old))

    def test_real_replacement_with_entity_match_still_counts(self):
        old = "Noor Haddad leads the Atlas migration team"
        new = "Atlas migration team is now led by Kofi Brennan instead of Noor Haddad"
        self.assertIsNotNone(gates_r6.replacement_evidence(new, old))


class DefaultOffTests(R10Base):
    env = {}

    def test_off_means_no_flags_and_no_new_key(self):
        self.store.remember("The Quillfeather contract is still unsigned as of today", "project")
        self.later(90)
        self.filler(1)
        self.assertEqual(self.relations("STALE_FLAG"), [])
        hit = self.store.find("Quillfeather contract", 3)
        self.assertNotIn("possibly_stale", hit[0])


if __name__ == "__main__":
    unittest.main()


class MoreNarrowingTests(R10Base):
    def test_a_plan_is_not_a_replacement(self):
        old = "The Harrowgate cluster is currently running version 4.2"
        new = "Plan to evaluate upgrading the Harrowgate cluster to version 5.0 instead next quarter"
        self.assertIsNone(gates_r6.replacement_evidence(new, old))

    def test_same_note_on_another_date_is_a_separate_occurrence(self):
        self.assertTrue(gates_r6.dates_only_differ("Coffee with Delia on 9 March was nice", "Coffee with Delia on 8 February was nice"))
        self.assertFalse(gates_r6.dates_only_differ("Coffee with Delia on 9 March", "Lunch with Delia on 9 March"))

    def test_common_names_are_not_a_shared_subject(self):
        old = "Dana has not signed the Quillfeather NDA yet"
        new = "Dana moved the pricing call to Thursday instead"
        self.assertFalse(gates_r6.subject_match(new, old, frozenset({"dana"})))

    def test_restore_clears_a_flag(self):
        status = self.store.remember("The Quillfeather contract is still unsigned as of today", "project")["memory_id"]
        self.later(45)
        self.filler(1)
        self.store.restore(status, confirm=True) if "confirm" in self.store.restore.__code__.co_varnames else self.store.restore(status)
        with self.store._session() as c:
            self.assertNotIn(status, gates_stale.stale_ids(c))
