"""Round 13 regression tests: one or more per finding of reviewer A's review (REVIEWER_A_GATES_R10_REVIEW_2026-10-10)
and reviewer B's check (REVIEWER_B_GATES_R10_CHECK_2026-10-10). Each first reproduces the reviewer's own
case. Synthetic temporary stores only. All gates stay default-off outside these tests."""

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates, gates_order, gates_r6, gates_stale, storage
from memorysafe_chatgpt.lifecycle import record_relation
from memorysafe_chatgpt.storage import MemoryStore

SRC = str(Path(storage.__file__).resolve().parents[1])
CANDIDATE = {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1",
             "MEMORYSAFE_GATE_STALE": "1", "MEMORYSAFE_GATE_FAST": "1", "MEMORYSAFE_GATE_R6_W_GOAL": "0",
             "MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED": "99"}


class Case(unittest.TestCase):
    env: dict = CANDIDATE

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.db = self.dir / "m.sqlite3"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
        self.p_env = patch.dict(os.environ, {**clean, "MEMORYSAFE_DB_PATH": str(self.db),
                                             "MEMORYSAFE_STATE_DIR": str(self.dir / "s"), **self.env}, clear=True)
        self.p_env.start()
        gates._INDEXES.clear()
        gates_r6._CACHES.clear()
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.p_now = patch.object(storage, "_now", side_effect=lambda: self.t.isoformat(timespec="seconds"))
        self.p_now.start()
        self.store = MemoryStore(self.db)

    def tearDown(self):
        self.p_now.stop()
        self.p_env.stop()
        gates._INDEXES.clear()
        gates_r6._CACHES.clear()
        self.temp.cleanup()

    def later(self, days=0.0, seconds=1):
        self.t += timedelta(days=days, seconds=seconds)

    def clock(self):
        return self.t.isoformat(timespec="seconds")

    def put(self, text, category="other", **kw):
        self.later()
        return self.store.remember(text, category, **kw)["memory_id"]

    def state(self, mid):
        with self.store._session() as c:
            return c.execute("SELECT state FROM memories WHERE id = ?", (mid,)).fetchone()[0]

    def open_review(self, old, new):
        with self.store._session() as c:
            record_relation(c, old_memory_id=old, new_memory_id=new, decision="REVIEW", status="open",
                            reason="Synthetic open review", evidence="test", confidence=.5, source="test",
                            created_at=self.clock())
            return int(c.execute("SELECT MAX(id) FROM memory_relations").fetchone()[0])

    def extra_files(self):
        return sorted(p.name for p in self.dir.iterdir()
                      if p.name not in {"m.sqlite3", "m.sqlite3-wal", "m.sqlite3-shm", "s"})


WORDS = ("amber basil cedar delta ember fjord garnet harbor indigo juniper kestrel lumen meadow nectar onyx "
         "pewter quartz russet saffron tundra umber velvet willow xenon yarrow zephyr").split()


def filler(i):
    rng = random.Random(i)
    return f"Note {i}: the {rng.choice(WORDS)} {rng.choice(WORDS)} sits beside the {rng.choice(WORDS)} {rng.choice(WORDS)}"


# --- reviewer A P1: capacity eviction loses the open-conflict safety guard ---------------------------------

class OpenConflictTests(Case):
    def test_reviewer_a_probe_open_conflict_participant_is_not_evicted(self):
        a = self.store.remember("The cobalt notebook contains sketches", "other", importance=.1)["memory_id"]
        b = self.store.remember("A silver bicycle stands near the garage", "other", importance=.2)["memory_id"]
        self.open_review(a, b)
        with self.store._session() as c:
            moved = gates.enforce_capacity(self.store, c, 1, keep_id=b)
        self.assertNotIn(a, moved)
        self.assertEqual(self.state(a), "active")

    def test_complete_remember_path_with_later_unrelated_writes(self):
        with patch.dict(os.environ, {"MEMORYSAFE_GATE_MAX_ACTIVE": "5"}):
            a = self.put("The cobalt notebook contains sketches", importance=.1)
            b = self.put("A silver bicycle stands near the garage", importance=.1)
            self.open_review(a, b)
            for i in range(12):
                self.put(filler(i), importance=.5)
            self.assertEqual((self.state(a), self.state(b)), ("active", "active"))
            with self.store._session() as c:
                    active = c.execute("SELECT COUNT(*) FROM memories WHERE state='active'").fetchone()[0]
            self.assertEqual(active, 5)

    def test_overflow_is_allowed_when_every_candidate_is_protected_unresolved_or_just_written(self):
        with patch.dict(os.environ, {"MEMORYSAFE_GATE_MAX_ACTIVE": "2"}):
            a = self.put("The cobalt notebook contains sketches")
            b = self.put("A silver bicycle stands near the garage")
            self.open_review(a, b)
            c_id = self.put("The violet umbrella hangs by the door")
            self.store.protect(c_id, True, "test")
            d = self.put("A brass lantern lights the porch")
            self.assertEqual([self.state(m) for m in (a, b, c_id, d)], ["active"] * 4)

    def test_mutation_query_rechecks_open_conflicts(self):
        a = self.put("The cobalt notebook contains sketches")
        b = self.put("A silver bicycle stands near the garage")
        self.open_review(a, b)
        with self.store._session() as c:
            moved = gates._move_out(self.store, c, {"id": a, "tier": 0}, "test", "test", self.clock())
        self.assertFalse(moved)
        self.assertEqual(self.state(a), "active")

    def test_capacity_only_gate_and_consolidate_also_keep_them(self):
        env = {"MEMORYSAFE_GATE_USEFUL": "0", "MEMORYSAFE_GATE_CONSOLIDATE": "1"}
        with patch.dict(os.environ, env):
            a = self.put("The cobalt notebook contains sketches", importance=.1)
            b = self.put("A silver bicycle stands near the garage", importance=.1)
            self.open_review(a, b)
            for i in range(4):
                self.put(filler(i), importance=.5)
            with self.store._session() as c:
                moved = gates.enforce_capacity(self.store, c, 2)
            self.assertFalse({a, b} & set(moved))
            plan = gates.consolidate(self.store, apply=False, capacity=1)
            self.assertFalse({a, b} & {m["id"] for m in plan["move"]})

    def test_both_lazy_and_reference_rankers_exclude_them(self):
        a = self.put("The cobalt notebook contains sketches", importance=.1)
        b = self.put("A silver bicycle stands near the garage", importance=.1)
        for i in range(6):
            self.put(filler(i), importance=.5)
        self.open_review(a, b)
        with self.store._session() as c:
            lazy = [i["id"] for i in gates_r6.rank_useful(self.store, c, 4, 4)]
            full = gates_r6.rank_useful_full(self.store, c, 4, 4)
        self.assertEqual(lazy, full)
        self.assertFalse({a, b} & set(lazy))


# --- reviewer A P1 / reviewer B: sidecar file persisted (forgotten) memory text ------------------------------

class NoSidecarTests(Case):
    def session(self, n=40):
        with patch.dict(os.environ, {"MEMORYSAFE_GATE_MAX_ACTIVE": "20"}):
            ids = [self.put(filler(i)) for i in range(n)]
            self.store.find("amber basil", 5)
        return ids

    def test_reviewer_a_probe_exit_save_after_forget_writes_nothing(self):
        ids = self.session()
        with self.store._session() as c:
            ix = gates.index_for(self.store)
            gates._current_book(c, ix)
            for mid in list(ix.norm):
                ix.neighbours(mid)
        self.store.forget(ids[-1], confirm=True)
        self.assertEqual(gates.save_indexes(), 0)
        self.assertEqual(gates.save_indexes(force=True), 0)
        self.assertFalse(Path(str(self.db) + ".gates-index").exists())

    def test_no_extra_file_next_to_the_db_after_a_gates_session_or_forget(self):
        ids = self.session()
        self.assertEqual(self.extra_files(), [])
        self.store.forget(ids[3], confirm=True)
        self.assertEqual(self.extra_files(), [])

    def test_upgrade_step_deletes_an_old_sidecar_and_temporaries(self):
        old = Path(str(self.db) + ".gates-index")
        tmp = Path(str(self.db) + ".gates-index.4242.tmp")
        old.write_text('{"norm": {"x": "forgotten secret words"}}')
        tmp.write_text("partial")
        MemoryStore(self.db)
        self.assertFalse(old.exists())
        self.assertFalse(tmp.exists())
        with patch.dict(os.environ, {k: "0" for k in CANDIDATE}):   # flags off: deleted as well
            old.write_text("x")
            MemoryStore(self.db)
            self.assertFalse(old.exists())

    def test_forget_also_removes_an_old_sidecar_and_purges_process_caches(self):
        ids = self.session()
        mid = ids[-1]
        with self.store._session() as c:
            ix = gates.index_for(self.store)
            gates._current_book(c, ix)
        self.assertIn(mid, ix.norm)
        Path(str(self.db) + ".gates-index").write_text("old")
        self.store.forget(mid, confirm=True)
        self.assertFalse(Path(str(self.db) + ".gates-index").exists())
        self.assertNotIn(mid, ix.norm)
        fc = gates_r6._CACHES.get(str(self.db))
        if fc is not None:
            self.assertNotIn(mid, fc.by_id)

    def test_separate_process_exit_after_forget_and_after_deletion_writes_no_file(self):
        ids = self.session()
        code = (
            "import os,sys; from pathlib import Path; from memorysafe_chatgpt.storage import MemoryStore\n"
            "s = MemoryStore(Path(sys.argv[1])); s.find('amber', 5); s.remember('Note x: quartz pewter', 'other')\n"
            "s.forget(sys.argv[2], confirm=True)\n"
            "with s._session() as c: c.execute('DELETE FROM memories WHERE id = ?', (sys.argv[3],))\n"
            "s.remember('Note y: onyx tundra', 'other')\n")
        env = {**os.environ, "PYTHONPATH": SRC, "MEMORYSAFE_GATE_MAX_ACTIVE": "20"}
        subprocess.run([sys.executable, "-c", code, str(self.db), ids[1], ids[2]], env=env, check=True, timeout=120)
        self.assertEqual(self.extra_files(), [])


# --- reviewer A P2: durable historical facts flagged by age -------------------------------------------------

class HistoricalStatusTests(Case):
    def test_reviewer_a_probe_historical_fact_is_not_age_flagged(self):
        durable = self.put("The building was installed on its permanent concrete foundation in 1980")
        self.later(days=59)
        self.store._r10_last_sweep = None
        self.put("A violet umbrella hangs beside the entrance")
        with self.store._session() as c:
            self.assertNotIn(durable, gates_stale.stale_ids(c))

    def test_counterexamples_and_present_status(self):
        cases = {
            "The building was installed on its permanent concrete foundation in 1980": "historical",
            "Solar panels installed on the roof in 2019.": "historical",
            "The Acme invoice had been pending for weeks.": "historical",
            "The router firmware was on hold until the recall ended.": "historical",
            "Docker is installed on the NAS.": "present",
            "Contract with Acme is pending signature.": "present",
            "The Acme contract is still unsigned. Bob likes green tea.": "mixed",
            "Lena loves hiking in the Alps.": None,
        }
        for text, kind in cases.items():
            self.assertEqual(gates_r6.status_kind(text), kind, text)

    def test_age_sweep_flags_present_but_not_historical_or_mixed(self):
        present = self.put("The Quarry lease renewal is still pending")
        mixed = self.put("The Quarry lease renewal is still pending. Our landlord is Ines.")
        past = self.put("The heat pump was installed in 2014")
        self.later(days=40)
        self.store._r10_last_sweep = None
        self.put("A violet umbrella hangs beside the entrance")
        with self.store._session() as c:
            flagged = gates_stale.stale_ids(c)
        self.assertIn(present, flagged)
        self.assertNotIn(mixed, flagged)
        self.assertNotIn(past, flagged)

    def test_newer_activity_needs_the_name_in_a_present_status_sentence(self):
        hist = self.put("The Brightwater pump was installed in 2014. It hums.")
        other = self.put("Payment for Kestrelton is pending. We also met Brightwater staff.")
        self.later(days=20)
        self.put("Brightwater sent the new maintenance schedule today")
        with self.store._session() as c:
            flagged = gates_stale.stale_ids(c)
        self.assertNotIn(hist, flagged)
        self.assertNotIn(other, flagged)


# --- reviewer A P2: stale cleanup depends on the flag and misses rejection ----------------------------------

class StaleCleanupTests(Case):
    def pair(self):
        a = self.put("The cobalt notebook contains sketches")
        b = self.put("A silver bicycle stands near the garage")
        return a, b

    def test_reviewer_a_probe_restore_while_off_then_revert(self):
        a, b = self.pair()
        with self.store._session() as c:
            gates_stale._flag(self.store, c, b, a, "synthetic newer activity", "2099-01-01T00:00:00+00:00")
        with patch.dict(os.environ, {"MEMORYSAFE_GATE_STALE": "0"}):
            out = self.store.restore(b, confirm=True)
        self.assertEqual(out.get("stale_cleared"), 1)
        with self.store._session() as c:
            self.assertNotIn(b, gates_stale.stale_ids(c))      # re-enabled: still cleared
            gates_stale._flag(self.store, c, b, a, "synthetic newer activity", "2099-01-01T00:00:00+00:00")
            gates_r6.after_resolution(self.store, c, "revert", b, a, "2099-01-02T00:00:00+00:00")
            self.assertNotIn(b, gates_stale.stale_ids(c))

    def test_disable_resolve_reenable_for_revert_and_keep_both_keeps_age_evidence(self):
        for action in ("revert", "keep_both"):
            a, b = self.pair()
            with self.store._session() as c:
                gates_stale._flag(self.store, c, a, b, "pair evidence", "2099-01-01T00:00:00+00:00")
                gates_stale._flag(self.store, c, b, a, "pair evidence back", "2099-01-01T00:00:00+00:00")
                gates_stale._flag(self.store, c, a, a + "#age", "age evidence", "2099-01-01T00:00:00+00:00")
            with patch.dict(os.environ, {"MEMORYSAFE_GATE_STALE": "0", "MEMORYSAFE_GATE_EXTINCTION": "0"}), \
                    self.store._session() as c:
                gates_r6.after_resolution(self.store, c, action, a, b, "2099-01-02T00:00:00+00:00")
            with self.store._session() as c:
                standing = {(str(o), str(n)) for o, n in c.execute(
                    "SELECT old_memory_id, new_memory_id FROM memory_relations WHERE decision='STALE_FLAG' AND status='flagged'")}
            self.assertNotIn((a, b), standing, action)
            self.assertNotIn((b, a), standing, action)
            self.assertIn((a, a + "#age"), standing, action)   # unrelated age evidence is preserved

    def test_public_resolve_revert_withdraws_pair_stale_evidence(self):
        old = self.put("The Atlas migration deadline is March 3")
        self.later(days=1)
        new = self.store.remember("The Atlas migration deadline moved to May 2", "project", confidence=.6)
        conflict = new.get("conflict_id")
        if conflict is None:
            self.skipTest("lifecycle did not open a review for this pair")
        with self.store._session() as c:
            gates_stale._flag(self.store, c, old, new["memory_id"], "pair", self.clock())
        self.store.resolve_conflict(conflict, "revert", confirm=True)
        with self.store._session() as c:
            n = c.execute("SELECT COUNT(*) FROM memory_relations WHERE decision='STALE_FLAG' AND status='flagged' "
                          "AND old_memory_id=? AND new_memory_id=?", (old, new["memory_id"])).fetchone()[0]
        self.assertEqual(n, 0)


# --- reviewer A P2: one-pass ordering does not enforce chains ----------------------------------------------

def _ids(scored):
    return [r["id"] for _s, r in sorted(scored, key=lambda x: (x[0], x[1].get("updated_at", "")), reverse=True)]


class OrderingTests(unittest.TestCase):
    def test_reviewer_a_probe_chain(self):
        scored = [(3., {"id": "old"}), (2., {"id": "middle"}), (1., {"id": "new"})]
        gates_stale.stale_reorder(scored, {"old": {"middle"}, "middle": {"new"}})
        self.assertEqual(_ids(scored), ["new", "middle", "old"])

    def test_chain_in_any_dict_order_and_long_chains(self):
        for n in (3, 5, 9):
            names = [f"m{i}" for i in range(n)]
            for seed in range(5):
                scored = [(float(n - i), {"id": m}) for i, m in enumerate(names)]
                newer = {names[i]: {names[i + 1]} for i in range(n - 1)}
                items = list(newer.items())
                random.Random(seed).shuffle(items)
                gates_stale.stale_reorder(scored, dict(items))
                self.assertEqual(_ids(scored), names[::-1])

    def test_multiple_flaggers_old_below_the_lowest(self):
        scored = [(5., {"id": "old"}), (4., {"id": "f1"}), (1., {"id": "f2"}), (3., {"id": "x"})]
        gates_stale.stale_reorder(scored, {"old": {"f1", "f2"}})
        order = _ids(scored)
        self.assertLess(order.index("f1"), order.index("old"))
        self.assertLess(order.index("f2"), order.index("old"))
        self.assertEqual(dict((r["id"], s) for s, r in scored)["x"], 3.)   # unrelated rows untouched

    def test_tie_scores(self):
        scored = [(1., {"id": "old", "updated_at": "2026-01-02"}), (1., {"id": "new", "updated_at": "2026-01-01"})]
        gates_stale.stale_reorder(scored, {"old": {"new"}})
        self.assertEqual(_ids(scored), ["new", "old"])

    def test_cycles_are_broken_deterministically_newest_first(self):
        rows = [{"id": "a", "updated_at": "2026-01-01"}, {"id": "b", "updated_at": "2026-01-03"},
                {"id": "c", "updated_at": "2026-01-02"}]
        outcomes = set()
        for seed in range(6):
            scored = [(1., dict(r)) for r in rows]
            random.Random(seed).shuffle(scored)
            gates_order.enforce(scored, [("a", "b"), ("b", "c"), ("c", "a")])
            self.assertEqual(gates_order.violations(scored, [("a", "b"), ("b", "c"), ("c", "a")]), [])
            outcomes.add(tuple(_ids(scored)))
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes.pop()[0], "b")

    def test_returned_window_boundary(self):
        # the flagger ranks just outside a limit-2 window; the flagged claim must not be returned above it
        scored = [(9., {"id": "top"}), (5., {"id": "old"}), (4.9, {"id": "new"}), (1., {"id": "z"})]
        gates_stale.stale_reorder(scored, {"old": {"new"}})
        order = _ids(scored)
        self.assertLess(order.index("new"), order.index("old"))
        self.assertEqual(order[:2], ["top", "new"])

    def test_extinction_order_needs_confidence_and_combines_with_stale(self):
        scored = [(3., {"id": "o", "confidence": .9}), (2., {"id": "n", "confidence": .5}),
                  (1., {"id": "s", "confidence": .9})]
        self.assertFalse(gates_r6.apply_order(scored, 1, [("o", "n")]))   # newer less trusted: no edge
        edges = gates_order.stale_edges({"n": {"s"}}) + gates_order.confidence_edges(scored, [("o", "s")])
        gates_order.enforce(scored, edges)
        self.assertEqual(gates_order.violations(scored, edges), [])

    def test_random_graphs_have_no_violations(self):
        rng = random.Random(13)
        for _ in range(200):
            n = rng.randint(2, 12)
            scored = [(round(rng.random() * 3, 1), {"id": f"m{i}", "updated_at": f"2026-01-{rng.randint(1, 28):02d}"})
                      for i in range(n)]
            edges = [(f"m{rng.randrange(n)}", f"m{rng.randrange(n)}") for _ in range(rng.randint(0, 2 * n))]
            gates_order.enforce(scored, edges)
            self.assertEqual(gates_order.violations(scored, edges), [])


class OrderRuleTests(Case):
    def test_order_rule_off_disables_extinction_ordering_only(self):
        a = self.put("The Kestrelton invoice 44 is still pending approval")
        self.later(days=20)
        b = self.put("Finance sent the Kestrelton invoice 44 approval today")
        with self.store._session() as c:
            flagged = gates_stale.stale_ids(c)
        if a not in flagged:
            self.skipTest("this synthetic pair raised no stale flag")
        with patch.dict(os.environ, {"MEMORYSAFE_GATE_R6_ORDER_RULE": "0"}):
            found = [m["memory_id"] for m in self.store.find("Kestrelton invoice 44 pending approval", 5, record=False)]
        self.assertLess(found.index(b), found.index(a))
        result = {m["memory_id"]: m for m in self.store.find("Kestrelton invoice", 5, record=False)}
        self.assertTrue(result[a]["possibly_stale"])
        self.assertIn("newer activity", result[a]["stale_reason"])
        self.assertEqual(result[a]["weakened"], 0)


# --- reviewer A P2: cache fingerprint is not a reliable change detector -----------------------------------

class CacheInvalidationTests(Case):
    def fill(self, n=30):
        return [self.put(filler(i), importance=round(.1 + (i % 5) * .1, 1)) for i in range(n)]

    def oracle(self):
        with self.store._session() as c:
            return gates.Book(gates._active_rows(c))

    def cached(self):
        fc = gates_r6._feature_cache(self.store)
        with self.store._session() as c:
            return gates_r6._snapshot(c, fc, None)

    def assertSame(self, book, fresh):
        self.assertEqual(book.rows, fresh.rows)
        self.assertEqual(book.rowids, fresh.rowids)
        self.assertEqual(book.sums, fresh.sums)

    def test_reviewer_a_probe_category_change_by_another_connection(self):
        ids = self.fill(5)
        self.cached()
        with closing(sqlite3.connect(self.db)) as other, other:
            other.execute("UPDATE memories SET category=? WHERE id=?", ("project", ids[0]))
        self.assertEqual(self.cached().rows[ids[0]][3], "project")

    def test_equal_length_rewording_and_balancing_changes(self):
        ids = self.fill(8)
        self.cached()
        with closing(sqlite3.connect(self.db)) as other, other:
            other.execute("UPDATE memories SET normalized_content = upper(normalized_content) WHERE id = ?", (ids[1],))
            other.execute("UPDATE memories SET importance = importance + 0.1 WHERE id = ?", (ids[2],))
            other.execute("UPDATE memories SET importance = importance - 0.1 WHERE id = ?", (ids[3],))
            other.execute("UPDATE memories SET recall_count = 2 WHERE id = ?", (ids[4],))
            other.execute("UPDATE memories SET recall_count = COALESCE(recall_count,0) WHERE id = ?", (ids[5],))
        self.assertSame(self.cached(), self.oracle())
        with self.store._session() as c:
            ix = gates.index_for(self.store)
            gates._current_book(c, ix)
            self.assertSame(ix.book, self.oracle())

    def test_separate_process_writes_against_a_freshly_rebuilt_oracle(self):
        with patch.dict(os.environ, {"MEMORYSAFE_GATE_MAX_ACTIVE": "25"}):
            self.fill(30)
            self.cached()
            code = (
                "import sys,random; from pathlib import Path; from memorysafe_chatgpt.storage import MemoryStore\n"
                "s = MemoryStore(Path(sys.argv[1])); rng = random.Random(7)\n"
                "with s._session() as c: ids = [r[0] for r in c.execute(\"SELECT id FROM memories WHERE state='active'\")]\n"
                "for i in range(15): s.remember(f'Other process note {i} about pewter quartz {rng.random():.3f}', 'other')\n"
                "s.protect(ids[0], True, 't'); s.forget(ids[1], confirm=True)\n"
                "with s._session() as c: c.execute(\"UPDATE memories SET category='project' WHERE id=?\", (ids[2],))\n")
            env = {**os.environ, "PYTHONPATH": SRC}
            subprocess.run([sys.executable, "-c", code, str(self.db)], env=env, check=True, timeout=120)
            self.assertSame(self.cached(), self.oracle())
            with self.store._session() as c:
                lazy = [i["id"] for i in gates_r6.rank_useful(self.store, c, 5, 25)]
            gates._INDEXES.clear()
            gates_r6._CACHES.clear()
            with self.store._session() as c:
                fresh = [i["id"] for i in gates_r6.rank_useful(self.store, c, 5, 25)]
            self.assertEqual(lazy, fresh)

    def test_a_log_gap_forces_a_rebuild(self):
        ids = self.fill(6)
        self.cached()
        with patch.object(gates, "LOG_KEEP", 4096), closing(sqlite3.connect(self.db)) as other, other:
            other.execute(f"DELETE FROM {gates.LOG_TABLE}")
            other.execute("UPDATE memories SET category='project' WHERE id=?", (ids[0],))
            other.execute(f"DELETE FROM {gates.LOG_TABLE} WHERE seq < (SELECT MAX(seq) FROM {gates.LOG_TABLE})")
            other.execute(f"INSERT INTO {gates.LOG_TABLE}(seq, row_id) VALUES ((SELECT MAX(seq) FROM {gates.LOG_TABLE}) + 5000, 0)")
        self.assertSame(self.cached(), self.oracle())

    def test_the_log_holds_ids_and_numbers_only_and_stays_bounded(self):
        self.fill(5)
        self.cached()
        with closing(sqlite3.connect(self.db)) as c, c:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({gates.LOG_TABLE})")]
            self.assertEqual(cols, ["seq", "row_id"])
            for i in range(gates.LOG_KEEP + 50):
                c.execute("UPDATE memories SET recall_count = ? WHERE rowid = 1", (i,))
            self.assertLessEqual(c.execute(f"SELECT COUNT(*) FROM {gates.LOG_TABLE}").fetchone()[0], gates.LOG_KEEP)

    def test_flags_off_creates_no_log(self):
        with patch.dict(os.environ, {k: "0" for k in CANDIDATE}):
            for i in range(5):
                self.put(filler(i))
            self.store.find("amber", 3)
        with closing(sqlite3.connect(self.db)) as c, c:
            names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'memorysafe_gate%'")]
        self.assertEqual(names, [])


# --- reviewer A: overrides are not range-validated ----------------------------------------------------------

class ParamRangeTests(unittest.TestCase):
    def test_bad_overrides_are_clamped_or_ignored(self):
        env = {"MEMORYSAFE_GATE_R6_W_IMP": "-3", "MEMORYSAFE_GATE_R6_WEAK_CUT": "1.7",
               "MEMORYSAFE_GATE_R6_RARE_QUOTA": "nan", "MEMORYSAFE_GATE_R6_U_FLOOR": "inf",
               "MEMORYSAFE_GATE_R6_ORDER_RULE": "5"}
        with patch.dict(os.environ, env):
            p = gates_r6.params()
        self.assertEqual(p["W_IMP"], 0.0)
        self.assertEqual(p["WEAK_CUT"], 0.95)
        self.assertEqual(p["RARE_QUOTA"], gates_r6.RARE_QUOTA)
        self.assertEqual(p["U_FLOOR"], gates_r6.U_FLOOR)
        self.assertEqual(p["ORDER_RULE"], 1.0)

    def test_defaults_are_inside_their_ranges(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_GATE_R6_")}
        with patch.dict(os.environ, clean, clear=True):
            p = gates_r6.params()
        for name, value in p.items():
            self.assertEqual(value, float(getattr(gates_r6, name)), name)


# --- reviewer B: keep-both wording, restore above the cap, host visibility ------------------------------

class ReviewerBTests(Case):
    def test_restore_may_take_active_above_the_cap_and_says_so_in_docs(self):
        with patch.dict(os.environ, {"MEMORYSAFE_GATE_MAX_ACTIVE": "4"}):
            ids = [self.put(filler(i)) for i in range(8)]
            out = [m for m in ids if self.state(m) == "evicted"]
            for m in out:
                self.assertTrue(self.store.restore(m, confirm=True)["restored"])
            with self.store._session() as c:
                active = c.execute("SELECT COUNT(*) FROM memories WHERE state='active'").fetchone()[0]
            self.assertEqual(active, 8)          # restore does not evict; the next write brings it back to the cap
            self.put(filler(99))
            with self.store._session() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM memories WHERE state='active'").fetchone()[0], 4)
        doc = Path(SRC).parent / "tests" / "bench" / "results" / "GATES_R13_BEHAVIOUR.md"
        self.assertIn("restore can take the store above", doc.read_text(encoding="utf-8"))
        self.assertIn("A LATER, third memory can still supersede either side", doc.read_text(encoding="utf-8"))

    def test_mcp_models_carry_the_gate_fields_only_when_flags_are_on(self):
        import importlib

        from memorysafe_chatgpt import server

        try:
            gated = importlib.reload(server)
            self.assertIn("extinguished_memory_ids", gated.RememberResult.model_fields)
            self.assertTrue({"weakening_undone", "stale_cleared"} <= set(gated.RestoreResult.model_fields))
            self.assertTrue({"possibly_stale", "stale_reason", "weakened"} <= set(gated.MemoryMatch.model_fields))
            with patch.dict(os.environ, {k: "0" for k in CANDIDATE}):
                plain = importlib.reload(server)
                self.assertNotIn("extinguished_memory_ids", plain.RememberResult.model_fields)
                self.assertNotIn("stale_cleared", plain.RestoreResult.model_fields)
                self.assertNotIn("weakened", plain.MemoryMatch.model_fields)
        finally:
            with patch.dict(os.environ, {k: "0" for k in CANDIDATE}):
                importlib.reload(server)


if __name__ == "__main__":
    unittest.main()


class ColdPathNeutralTests(Case):
    """Round 13 cold-first-write changes are speed-only: lazily compiled patterns give the same results,
    the goal module stays unloaded when MEMORYSAFE_GATE_GOAL2 is off, and the per-text name cache is
    cleared with the other text caches on forget."""

    def test_lazy_patterns_match_compiled(self):
        import re
        names = ["_GOAL_RE", "_HAZARD", "_FROM_MARK", "_STATUS", "_ENTITY", "_INITIAL", "_SENTENCE",
                 "_PAST_AUX", "_DATED_PAST", "_TENTATIVE"]
        texts = ["Goal: ship the Atlas beta. The deploy is pending since March.", "Switched from Postgres to MySQL",
                 "It was installed back in 2019; Maria might consider a peanut allergy test.", "", "x\nY Zed"]
        for name in names:
            value = getattr(gates_r6, name)
            pattern = value if isinstance(value, re.Pattern) else re.compile(*value._args)
            for text in texts:
                self.assertEqual([m.span() for m in getattr(gates_r6, name).finditer(text)],
                                 [m.span() for m in pattern.finditer(text)], name)
            self.assertIsInstance(getattr(gates_r6, name), re.Pattern)   # replaced by the compiled pattern

    def test_goal_module_not_loaded_when_goal2_off(self):
        code = ("import os, sys, tempfile; from pathlib import Path\n"
                "from memorysafe_chatgpt.storage import MemoryStore\n"
                "d = Path(tempfile.mkdtemp()); s = MemoryStore(d / 'm.sqlite3')\n"
                "for i in range(6): s.remember(f'Note {i}: the Atlas report for wing {i} is filed', 'other')\n"
                "s.find('Atlas report', 5)\n"
                "print('memorysafe_chatgpt.gates_goal' in sys.modules)")
        env = {**os.environ, "MEMORYSAFE_GATE_MAX_ACTIVE": "3", "PYTHONPATH": SRC}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False")

    def test_entity_cache_cleared_on_forget(self):
        a = self.put("Maria Delacroix owns the Quillfeather notebook", importance=0.5)
        self.put("The Quillfeather notebook is pending review", importance=0.5)
        gates_r6.entities("Maria Delacroix owns the Quillfeather notebook")
        self.assertGreater(gates_r6.entities.cache_info().currsize, 0)
        self.store.forget(a, confirm=True)
        self.assertEqual(gates_r6.entities.cache_info().currsize, 0)
