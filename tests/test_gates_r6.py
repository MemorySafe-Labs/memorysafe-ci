"""Round-6 gate tests: usefulness-weighted capacity gate (MEMORYSAFE_GATE_USEFUL) and
evidence-based active forgetting (MEMORYSAFE_GATE_EXTINCTION).

Covers: reversibility (restore undoes weakening and brings an extinguished memory back), no
action on age alone, protected memories untouched without confirm, open conflicts kept
available, default-off equivalence to the shipped 0.4.19 code (byte-level, when the
pristine commit is reachable with git), and the usefulness ordering itself.
Temp stores only; no live store, no port 8765.

    PYTHONPATH=src python -m pytest tests/test_gates_r6.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates, gates_r6, storage

from test_gates import GATE_FLAGS, GateCase, routine

R6_FLAGS = ("MEMORYSAFE_GATE_USEFUL", "MEMORYSAFE_GATE_EXTINCTION")
REPO = Path(__file__).resolve().parents[1]
PRISTINE = "d9b7a763cd1662227259ee7cfa9f70cd5ea9b2cb"  # MemorySafe-Labs/memorysafe main = v0.4.19


class R6Case(GateCase):
    def setUp(self) -> None:
        super().setUp()
        for flag in R6_FLAGS + tuple(k for k in os.environ if k.startswith("MEMORYSAFE_GATE_R6_")):
            os.environ.pop(flag, None)
        gates._INDEXES.clear()
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._clock = patch.object(storage, "_now", side_effect=self.tick)
        self._clock.start()

    def tearDown(self) -> None:
        self._clock.stop()
        super().tearDown()

    def tick(self) -> str:
        self.t += timedelta(minutes=1)
        return self.t.isoformat(timespec="seconds")

    def rel(self, memory_id: str, status: str = "weakened") -> int:
        with self.store._session() as c:
            return int(c.execute("SELECT COUNT(*) FROM memory_relations WHERE old_memory_id = ? AND status = ?",
                                 (memory_id, status)).fetchone()[0])

    def events(self, memory_id: str, decision: str) -> list[str]:
        with self.store._session() as c:
            return [r[0] for r in c.execute("SELECT reason FROM decision_events WHERE memory_id = ? AND decision = ?",
                                            (memory_id, decision))]

    def st(self, memory_id: str) -> str:
        return self.store.explain(memory_id)["state"]

    def rank(self, query: str, memory_id: str) -> int | None:
        ids = [r["memory_id"] for r in self.store.find(query, 10, record=False)]
        return ids.index(memory_id) if memory_id in ids else None


EXT = {"MEMORYSAFE_GATE_EXTINCTION": "1"}


class ReplacementEvidenceTests(unittest.TestCase):
    def test_positive_pairs(self) -> None:
        for new, old in [
            ("Omar has a new line manager, Chiara Haddad, since the reorg", "Omar's line manager is Noor Delgado"),
            ("The team moved off Asana, issues now live in YouTrack", "The team tracks issues in Asana"),
            ("We migrated the primary database from MongoDB to Postgres", "Our primary database is MongoDB"),
            ("The daily standup moved to 10:30", "The daily standup is at 9:15"),
        ]:
            self.assertTrue(gates_r6.replacement_evidence(new, old), (new, old))

    def test_frame_threshold(self) -> None:
        # 2 of 4 old words shared: evidence at FRAME_MIN 0.5, not at the tuned 0.6
        pair = ("Omar's new manager is Chiara Haddad after the reorg", "Omar's manager is Noor Delgado")
        self.assertTrue(gates_r6.replacement_evidence(*pair, frame_min=0.5))
        self.assertIsNone(gates_r6.replacement_evidence(*pair))

    def test_negative_pairs(self) -> None:
        for new, old in [
            # different slot: an amount does not replace a date
            ("The Orchid data warehouse budget was revised to $40,000", "The Orchid data warehouse deadline is March 17"),
            # no change language
            ("Omar said the tram was crowded this morning", "Omar said the bus was late this morning"),
            # same value restated
            ("The daily standup is now at 9:15", "The daily standup is at 9:15"),
            # too little shared subject
            ("Omar moved to Lisbon", "Omar lives in Porto"),
        ]:
            self.assertIsNone(gates_r6.replacement_evidence(new, old), (new, old))

    def test_age_is_not_an_input(self) -> None:
        import inspect
        source = inspect.getsource(gates_r6)
        for word in ("created_at <", "updated_at <", "julianday", "datetime(", "_days_since", "timedelta"):
            self.assertNotIn(word, source.split("# --- extinction")[1])


class OffByDefaultR6Tests(R6Case):
    def test_flags_off(self) -> None:
        self.assertFalse(gates_r6.useful_gate_enabled() or gates_r6.extinction_enabled())
        with self.flags(MEMORYSAFE_GATE_USEFUL="0", MEMORYSAFE_GATE_EXTINCTION="off"):
            self.assertFalse(gates_r6.useful_gate_enabled() or gates_r6.extinction_enabled())

    def test_no_r6_code_runs_and_no_trace_when_off(self) -> None:
        with patch.object(gates_r6, "after_write", side_effect=AssertionError("called")), \
                patch.object(gates_r6, "enforce_capacity_useful", side_effect=AssertionError("called")), \
                patch.object(gates_r6, "note_recall", side_effect=AssertionError("called")), \
                self.flags(MEMORYSAFE_MAX_ACTIVE="6"):
            first = self.store.remember("Our primary database is MongoDB", "project")
            for i in range(8):
                self.store.remember(routine(i), "task")
            result = self.store.remember("We migrated the primary database from MongoDB to Postgres", "project")
            self.store.find("primary database", 3)
        self.assertNotIn("extinguished_memory_ids", result)
        with self.store._session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decision_events WHERE source LIKE 'gate:%'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM memory_relations WHERE decision = 'WEAKEN'").fetchone()[0], 0)
        self.assertIn(self.st(first["memory_id"]), {"active", "superseded", "evicted"})

    def test_useful_flag_alone_does_nothing_without_the_capacity_gate(self) -> None:
        with self.flags(MEMORYSAFE_GATE_USEFUL="1", MEMORYSAFE_MAX_ACTIVE="5"), \
                patch.object(gates_r6, "enforce_capacity_useful", side_effect=AssertionError("called")):
            for i in range(9):
                self.store.remember(routine(i), "task")


SCRIPT = textwrap.dedent('''
    import json, os, sys, uuid, itertools
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    from unittest.mock import patch
    sys.path.insert(0, sys.argv[1])
    from memorysafe_chatgpt import storage
    from memorysafe_chatgpt.storage import MemoryStore
    t = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    def now():
        t[0] += timedelta(minutes=1)
        return t[0].isoformat(timespec="seconds")
    counter = itertools.count()
    class U:
        def __init__(self): self.hex = f"{next(counter):08x}" + "0" * 24
    db = Path(sys.argv[2])
    out = []
    with patch.object(storage, "_now", now), patch.object(uuid, "uuid4", U):
        s = MemoryStore(db)
        texts = ["Our primary database is MongoDB", "Omar's manager is Noor Delgado",
                 "The team tracks issues in Asana", "Omar is severely allergic to peanuts"]
        texts += [f"Routine round: Ana checked the cart in wing {i}" for i in range(14)]
        texts += ["We migrated the primary database from MongoDB to Postgres",
                  "Omar's new manager is Chiara Haddad after the reorg",
                  "The team moved off Asana, issues now live in YouTrack",
                  "Omar said the tram was crowded this morning"]
        for i, text in enumerate(texts):
            r = s.remember(text, ["project", "personal", "task", "other"][i % 4])
            out.append({k: v for k, v in r.items() if k != "memory_id"})
            if i % 5 == 4:
                out.append([x["content"] for x in s.find("primary database manager issues", 3)])
        ids = [m["memory_id"] for m in s.find("routine cart wing", 50, record=False)]
        out.append(s.restore(ids[0], confirm=True))
        conflicts = s.review_conflicts()
        out.append(conflicts)
    import sqlite3
    c = sqlite3.connect(db)
    for table in ("memories", "decision_events", "memory_relations"):
        out.append(c.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall())
    print(json.dumps(out, default=str, sort_keys=True))
''')


class DefaultOffEquivalenceTests(unittest.TestCase):
    """With every flag unset, the branch must produce byte-identical results and database rows
    to pristine 0.4.19 for the same scripted session (MAX_ACTIVE set, so eviction runs)."""

    def test_identical_to_pristine_0_4_19(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as temp:
            pristine = Path(temp) / "pristine"
            pristine.mkdir()
            archive = subprocess.run(["git", "-C", str(REPO), "archive", PRISTINE, "src"], capture_output=True)
            if archive.returncode != 0:
                self.skipTest("pristine 0.4.19 commit not in this clone")
            subprocess.run(["tar", "-x", "-C", str(pristine)], input=archive.stdout, check=True)
            script = Path(temp) / "s.py"
            script.write_text(SCRIPT)
            env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
            env.update({"MEMORYSAFE_MAX_ACTIVE": "12", "MEMORYSAFE_STATE_DIR": str(Path(temp) / "state")})
            outs = []
            for name, src in (("pristine", pristine / "src"), ("branch", REPO / "src")):
                db = Path(temp) / f"db_{name}" / "m.sqlite3"
                db.parent.mkdir()
                run = subprocess.run([sys.executable, str(script), str(src), str(db)], capture_output=True, text=True,
                                     env={**env, "MEMORYSAFE_DB_PATH": str(db)})
                self.assertEqual(run.returncode, 0, run.stderr[-2000:])
                outs.append(run.stdout)
            self.assertEqual(json.loads(outs[0]), json.loads(outs[1]))


class ExtinctionTests(R6Case):
    def seed(self) -> None:
        for i in range(12):
            self.store.remember(routine(i), "task")

    def test_weaken_lowers_rank_and_restore_undoes_it(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember("Our primary database is MongoDB", "task")["memory_id"]
            before = self.rank("which database is the primary one", old)
            new = self.store.remember("We migrated the primary database from MongoDB to Postgres", "task")["memory_id"]
            self.assertEqual(self.rel(old), 1)
            self.assertEqual(self.st(old), "active")
            self.assertTrue(self.events(old, "WEAKEN"))
            self.assertIn("Evidence from", self.events(old, "WEAKEN")[0])
            after = self.rank("which database is the primary one", old)
            self.assertIsNotNone(after)
            self.assertLess(self.rank("which database is the primary one", new), after)
            weak_score = next(r["match_score"] for r in self.store.find("which database is the primary one", 10, record=False) if r["memory_id"] == old)
            preview = self.store.restore(old)
            self.assertEqual(preview.get("would_undo_weakening"), 1)
            self.assertEqual(self.rel(old), 1)  # preview wrote nothing
            restored = self.store.restore(old, confirm=True)
            self.assertEqual(restored.get("weakening_undone"), 1)
            self.assertEqual(self.rel(old), 0)
            self.assertEqual(self.rel(old, "undone"), 1)
            self.assertTrue(self.events(old, "UNWEAKEN"))
            score = next(r["match_score"] for r in self.store.find("which database is the primary one", 10, record=False) if r["memory_id"] == old)
            self.assertAlmostEqual(score - weak_score, gates_r6.WEAK_PENALTY, places=2)

    def test_repeated_evidence_extinguishes_and_restore_brings_back(self) -> None:
        self.seed()
        rows_before = None
        with self.flags(**EXT):
            old = self.store.remember("Omar's line manager is Noor Delgado", "task")["memory_id"]
            rows_before = self.row_count()
            self.store.remember("Omar has a new line manager, Chiara Haddad, since the reorg", "task")
            self.assertEqual(self.st(old), "active")
            self.assertEqual(self.rel(old), 1)
            self.store.remember("We migrated the team: Omar's new line manager is Chiara Haddad", "task")
            self.assertEqual(self.st(old), gates_r6.EXTINGUISHED_STATE)
            self.assertTrue(self.events(old, "EXTINGUISH"))
            self.assertNotIn(old, [r["memory_id"] for r in self.store.find("who is Omar's manager", 10, record=False)])
            self.assertEqual(self.row_count(), rows_before + 2)  # never deleted
            back = self.store.restore(old, confirm=True)
            self.assertTrue(back["restored"])
            self.assertEqual(self.st(old), "active")
            self.assertEqual(self.rel(old), 0)
            self.assertIn(old, [r["memory_id"] for r in self.store.find("who is Omar's manager", 10, record=False)])

    def test_retrieved_then_corrected_counts_as_evidence(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember("Omar's line manager is Noor Delgado", "task")["memory_id"]
            self.store.find("who is Omar's manager", 3)
            self.store.remember("Omar has a new line manager, Chiara Haddad, since the reorg", "task")
        self.assertEqual(self.st(old), gates_r6.EXTINGUISHED_STATE)
        kinds = " ".join(self.events(old, "WEAKEN"))
        self.assertIn("retrieved then corrected", kinds)

    def test_correction_long_after_recall_is_not_c(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember("Omar's line manager is Noor Delgado", "task")["memory_id"]
            self.store.find("who is Omar's manager", 3)
            for i in range(12, 20):
                self.store.remember(routine(i), "task")
            self.store.remember("Omar has a new line manager, Chiara Haddad, since the reorg", "task")
        self.assertEqual(self.st(old), "active")
        self.assertEqual(self.rel(old), 1)

    def test_no_action_on_age_alone(self) -> None:
        with self.flags(**EXT, MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_USEFUL="1"):
            old = self.store.remember("Omar is based in Porto", "task")["memory_id"]
            self.t += timedelta(days=3650)  # ten years later
            for i in range(200):
                self.store.remember(routine(i) + f" round {i}", "task")
                self.t += timedelta(days=30)
            self.store.find("where is Omar based", 3)
        self.assertEqual(self.st(old), "active")
        self.assertEqual(self.rel(old), 0)
        with self.store._session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decision_events WHERE decision IN "
                                       "('WEAKEN','EXTINGUISH','PROPOSE_WEAKEN')").fetchone()[0], 0)

    def test_protected_memory_is_never_weakened_without_confirm(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember("Omar is based in Porto", "task")["memory_id"]
            self.store.protect(old, True, "user")
            base_rank = self.rank("where is Omar based", old)
            self.store.find("where is Omar based", 3)
            self.store.remember("Omar is now based in Lisbon after another move", "task")
            self.store.remember("Omar is now based in Lisbon after the move, new flat", "task")
            self.store.remember("Omar's manager is Noor Delgado", "task")
            self.assertEqual(self.st(old), "active")
            self.assertEqual(self.rel(old), 0)
            self.assertTrue(self.events(old, "PROPOSE_WEAKEN"))
            self.assertEqual(gates_r6.weaken(self.store, old)["needs_confirmation"], True)
            self.assertEqual(self.rel(old), 0)
            done = gates_r6.weaken(self.store, old, "user agreed", confirm=True)
            self.assertTrue(done["weakened"])
            self.assertEqual(self.rel(old), 1)
            self.assertEqual(self.st(old), "active")  # one piece of evidence: weakened, not moved
            self.store.restore(old, confirm=True)
            self.assertEqual(self.rel(old), 0)
            self.assertEqual(self.rank("where is Omar based", old) is not None, base_rank is not None)

    def test_open_conflict_side_is_weakened_but_never_moved_out(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember("Our primary database is MongoDB", "project")["memory_id"]
            with self.store._session() as c:
                from memorysafe_chatgpt.lifecycle import record_relation
                record_relation(c, old_memory_id=old, new_memory_id="X", decision="REVIEW", status="open",
                                reason="test", evidence="test", confidence=0.5, source="test", created_at="2026")
            self.store.find("primary database", 3)
            self.store.remember("We migrated the primary database from MongoDB to Postgres", "project")
            self.store.remember("We migrated the primary database from MongoDB to Postgres last week", "project")
        self.assertEqual(self.st(old), "active")
        self.assertGreaterEqual(self.rel(old), 2)

    def test_lower_confidence_write_gives_no_evidence(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember("Omar is based in Porto", "task", confidence=0.95)["memory_id"]
            self.store.remember("Omar is now based in Lisbon after another move", "task", confidence=0.6)
        self.assertEqual(self.rel(old), 0)

    def test_revert_withdraws_the_rejected_updates_evidence(self) -> None:
        self.seed()
        with self.flags(**EXT):
            old = self.store.remember("Our primary database is MongoDB", "task")["memory_id"]
            new = self.store.remember("We migrated the primary database from MongoDB to Postgres", "task")
            with self.store._session() as c:
                from memorysafe_chatgpt.lifecycle import record_relation
                cid = record_relation(c, old_memory_id=old, new_memory_id=new["memory_id"], decision="REVIEW",
                                      status="open", reason="t", evidence="t", confidence=0.5, source="t",
                                      created_at="2026")
            self.assertEqual(self.rel(old), 1)
            self.store.resolve_conflict(cid, "revert", confirm=True)
        self.assertEqual(self.rel(old), 0)
        self.assertEqual(self.st(old), "active")


class UsefulGateTests(R6Case):
    FLAGS = {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "14"}

    def test_rare_throwaway_goes_before_rare_useful(self) -> None:
        with self.flags(**self.FLAGS):
            throwaway = self.store.remember("Omar said the tram was crowded this morning", "other")["memory_id"]
            useful = self.store.remember("The spare server room key is under the reception desk", "other")["memory_id"]
            self.store.find("spare server room key", 3)  # used once: evidence of usefulness
            for i in range(14):
                self.store.remember(routine(i), "task")
        self.assertEqual(self.st(throwaway), "evicted")
        self.assertEqual(self.st(useful), "active")
        reason = " ".join(h["reason"] for h in self.store.explain(throwaway)["history"])
        self.assertIn("Usefulness gate", reason)
        self.assertIn("signs of use: none", reason)
        self.assertTrue(self.store.restore(throwaway, confirm=True)["restored"])

    def test_r5_gate_keeps_the_rare_throwaway_useful_gate_does_not(self) -> None:
        with self.flags(MEMORYSAFE_GATE_CAPACITY="1", MEMORYSAFE_GATE_MAX_ACTIVE="14"):
            throwaway = self.store.remember("Omar said the tram was crowded this morning", "other")["memory_id"]
            for i in range(14):
                self.store.remember(routine(i), "task")
        self.assertEqual(self.st(throwaway), "active")  # rarity alone protected it in r5

    def test_anchor_and_goal_are_signs_of_use(self) -> None:
        with self.flags(**self.FLAGS):
            self.store.remember("Goal: launch the Harbour clinic booking portal this spring", "other")
            goal_rel = self.store.remember("Harbour clinic booking portal needs a Spanish translation", "other")["memory_id"]
            anchored = self.store.remember("Never leave the cold room alarm silenced overnight", "other")["memory_id"]
            plain = self.store.remember("Omar said the neighbour's dog barked all night", "other")["memory_id"]
            for i in range(13):
                self.store.remember(routine(i), "task")
        self.assertEqual(self.st(goal_rel), "active")
        self.assertEqual(self.st(anchored), "active")
        self.assertEqual(self.st(plain), "evicted")

    def test_protected_and_just_written_are_never_moved(self) -> None:
        with self.flags(**self.FLAGS):
            kept = self.store.remember("Omar said the tram was crowded this morning", "other")["memory_id"]
            self.store.protect(kept, True, "user")
            for i in range(30):
                last = self.store.remember(routine(i), "task")["memory_id"]
                self.assertEqual(self.st(last), "active")
        self.assertEqual(self.st(kept), "active")

    def test_rare_quota_is_bounded(self) -> None:
        with self.flags(**self.FLAGS, MEMORYSAFE_GATE_R6_RARE_QUOTA="0.1"):
            for i in range(14):
                self.store.remember(routine(i), "task")
            with self.store._session() as c:
                chosen = gates_r6.rank_useful(self.store, c, 3, 14)
        self.assertEqual(len(chosen), 3)

    def test_weakened_memory_loses_keep_priority(self) -> None:
        with self.flags(**self.FLAGS, **EXT):
            old = self.store.remember("Omar's line manager is Noor Delgado", "other")["memory_id"]
            self.store.find("who is Omar's manager", 3)
            for i in range(6):
                self.store.remember(routine(i), "task")
            self.store.remember("Omar has a new line manager, Chiara Haddad, since the reorg", "other")
            self.assertEqual(self.rel(old), 1)
            for i in range(6, 20):
                self.store.remember(routine(i), "task")
        self.assertNotEqual(self.st(old), "active")


if __name__ == "__main__":
    unittest.main()
