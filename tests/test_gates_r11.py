"""Round 11 (prototype, default off):
- MEMORYSAFE_GATE_FAST: exact-equivalent speed-ups (same verdicts, nothing written to disk);
- MEMORYSAFE_GATE_GOAL2: explicit goal / "that mattered" signals (gates_goal.py)."""

import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates, gates_goal, gates_r6, lifecycle, storage
from memorysafe_chatgpt.storage import MemoryStore

HERE = Path(__file__).resolve().parent


def _corpus() -> list[str]:
    """Every sentence-like string literal in the lifecycle and gate tests (synthetic texts)."""
    out = []
    for name in ("test_lifecycle.py", "test_gates_r10.py", "test_gates_r7.py", "test_storage.py"):
        path = HERE / name
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if 12 <= len(text) <= 300 and " " in text and "\n" not in text and not text.startswith(("MEMORYSAFE", "SELECT", "UPDATE")):
                    out.append(text)
    return sorted(set(out))


def _key(v):
    return None if v is None else (v["action"], v["reason"], v["evidence"], v["confidence"], v["old"]["id"])


class FastEquivalenceTests(unittest.TestCase):
    def test_off_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(storage._fast_on())

    def test_consider_incoming_same_verdicts_on_the_test_corpus(self):
        texts = _corpus()
        self.assertGreater(len(texts), 100)
        rows = [{"id": f"m{i}", "content": t} for i, t in enumerate(texts)]
        differ = 0
        for i, text in enumerate(texts):
            others = rows[:i] + rows[i + 1:]
            storage._FAST_PIN.append(False)
            try:
                slow = lifecycle.consider_incoming(text, others)
            finally:
                storage._FAST_PIN.pop()
            storage._FAST_PIN.append(True)
            try:
                fast = lifecycle.consider_incoming(text, others)
            finally:
                storage._FAST_PIN.pop()
            differ += _key(slow) != _key(fast)
        self.assertEqual(differ, 0)

    def test_bounded_negation_check_is_exact(self):
        pairs = [("The office is not open on Sundays", "The office is open on Sundays"),
                 ("We never ship on Fridays", "We ship on Fridays after the review"),
                 ("Dana does not eat shellfish", "Dana eats shellfish and fish")]
        for new, old in pairs:
            storage._FAST_PIN.append(False)
            slow = lifecycle._negation_change(new, old)
            storage._FAST_PIN.pop()
            storage._FAST_PIN.append(True)
            fast = lifecycle._negation_change(new, old)
            storage._FAST_PIN.pop()
            self.assertEqual(slow, fast)


class StoreCase(unittest.TestCase):
    env: dict = {}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        base = {"MEMORYSAFE_DB_PATH": str(self.dir / "m.sqlite3"), "MEMORYSAFE_STATE_DIR": str(self.dir / "s")}
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
        self.p_env = patch.dict(os.environ, {**clean, **base, **self.env}, clear=True)
        self.p_env.start()
        gates._INDEXES.clear()
        gates_r6._CACHES.clear()
        self.store = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"]))

    def tearDown(self):
        self.p_env.stop()
        self.temp.cleanup()

    def events(self, decision):
        with self.store._session() as c:
            return [tuple(r) for r in c.execute(
                "SELECT memory_id, reason FROM decision_events WHERE decision = ?", (decision,))]


R11 = {"MEMORYSAFE_GATE_FAST": "1", "MEMORYSAFE_GATE_GOAL2": "1", "MEMORYSAFE_GATE_CAPACITY": "1",
       "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1", "MEMORYSAFE_GATE_STALE": "1",
       "MEMORYSAFE_GATE_R6_W_GOAL": "0", "MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED": "99"}


class FastWritesNoFilesTests(StoreCase):
    env = R11

    def test_no_new_files(self):
        for i in range(12):
            self.store.remember(f"Note {i}: the Harlow office printer needs toner on floor {i}", "other")
        self.store.find("Harlow office printer", 3)
        names = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual([n for n in names if not n.startswith("m.sqlite3")], [])
        self.assertEqual([n for n in names if n.startswith("m.sqlite3") and n not in ("m.sqlite3", "m.sqlite3-wal", "m.sqlite3-shm")], [])


class Goal2OffTests(StoreCase):
    def test_tag_ignored_when_off(self):
        a = self.store.remember("Goal: run the Lisbon marathon in October", "project")
        b = self.store.remember("Race number pickup for the Lisbon marathon is at the Belem expo hall", "task",
                                tag="goal:" + a["memory_id"])
        self.assertEqual(b["decision"], "STORE")
        self.assertEqual(self.events("GOAL_TAG"), [])

    def test_off_ranking_unchanged_by_goal2_code(self):
        # off: _goal_signal is the round-7 rule and goal evidence always counts
        f = gates_r6.features("Goal: run the Lisbon marathon")
        self.assertEqual(gates_r6._goal_signal(None, None, {}, "x", f, [], gates_r6.params()), (True, False, True))


class Goal2Tests(StoreCase):
    env = {**R11, "MEMORYSAFE_GATE_R6_GOAL_ENTITY": "1"}   # the entity rule is off in the frozen r11; tested on

    def test_tag_is_an_ids_only_event_and_bad_tags_write_nothing(self):
        goal = self.store.remember("Goal: run the Lisbon marathon in October", "project")["memory_id"]
        fact = self.store.remember("Race number pickup for the Lisbon marathon is at the Belem expo hall", "task",
                                   tag=f"goal:{goal}")["memory_id"]
        self.assertEqual(self.events("GOAL_TAG"), [(fact, f"caller tag: goal {goal}")])
        with self.assertRaises(ValueError):
            self.store.remember("Another note about the expo hall parking", "other", tag="goal: Lisbon marathon!")
        with self.store._session() as c:
            n = c.execute("SELECT COUNT(*) FROM memories WHERE content LIKE '%parking%'").fetchone()[0]
        self.assertEqual(n, 0)
        r = gates_goal.mark_mattered(self.store, fact)
        self.assertTrue(r["tagged"])
        self.assertIn((fact, "caller tag: mattered"), self.events("GOAL_TAG"))

    def _classify(self, goal_text, text):
        from memorysafe_chatgpt.gates_r6 import entities, initials, features, _GOAL_FRAME_TEXT, _INCIDENTAL
        frame_words = frozenset(_GOAL_FRAME_TEXT.split())
        names = frozenset(n for n in entities(goal_text) | initials(goal_text) if n not in frame_words and n != "maya")
        prof = gates_goal.profiles({"g": goal_text}, {}, 5.0, gates.terms_of(_GOAL_FRAME_TEXT),
                                   {"g": features(goal_text)[3]})
        prof = [(g, names & n, k) for g, n, k in prof]
        return gates_goal.classify("x", text, features(text), prof, frozenset({"maya"}), gates_goal._Signals(),
                                   gates_r6.params(), _INCIDENTAL)[0]

    def test_entity_rule_needs_the_goal_name_and_no_one_else(self):
        g = "Goal: Maya wants to run the Lisbon marathon in October"
        self.assertTrue(self._classify(g, "The Lisbon marathon training plan needs four long runs of at least 30 km"))
        self.assertFalse(self._classify(g, "Maya's cousin Teo ran the Lisbon marathon in 2019"))         # someone else
        self.assertFalse(self._classify(g, "Maya read an article about how hot the Lisbon marathon was"))  # passing remark
        self.assertFalse(self._classify(g, "Maya found an old photo from a holiday in Lisbon"))          # name, no goal word
        self.assertFalse(self._classify(g, "The physio said the left knee needs a rest week"))           # no goal name

    def test_restatements_count_as_mattered(self):
        mid = self.store.remember("The Harlow office alarm code changes every quarter", "other")["memory_id"]
        for _ in range(2):
            self.store.remember("The Harlow office alarm code changes every quarter", "other")
        fc = gates_r6._feature_cache(self.store)
        sig = gates_goal._Signals()
        with self.store._session() as c:
            sig.load(c)
        self.assertGreaterEqual(sig.merges.get(mid, 0), 2)
        self.assertGreaterEqual(sig.merges.get(mid, 0), gates_r6.params()["RECONF_MIN"])
        del fc


class Goal2SignalInStoreTests(StoreCase):
    env = {**R11, "MEMORYSAFE_GATE_R6_GOAL_ENTITY": "1"}   # the entity rule is off in the frozen r11; tested on

    def test_tagged_fact_is_goal_related_untagged_distractor_is_not(self):
        goal = self.store.remember("Goal: Maya wants to run the Lisbon marathon in October", "project")["memory_id"]
        served = self.store.remember("Race number pickup for the Lisbon marathon is at the Belem expo hall", "other",
                                     tag=f"goal:{goal}")["memory_id"]
        member = self.store.remember("The Lisbon marathon training plan needs four long runs", "other")["memory_id"]
        other = self.store.remember("Maya's cousin Teo ran the Lisbon marathon last year", "other")["memory_id"]
        remark = self.store.remember("Maya saw a video about the Lisbon marathon route", "other")["memory_id"]
        for i in range(8):
            self.store.remember(f"Maya said lunch remark {i} about the soup at the food hall", "other")
        with self.store._session() as c:
            fc = gates_r6._feature_cache(self.store)
            rows = gates_r6._snapshot(c, fc, None).rows
            p = gates_r6.params()
            prof = gates_r6._goal_profiles(fc, c, rows, p)
            gates_r6._goal2_prepare(fc, c, rows, p)
            ids = [served, member, other, remark]
            gates_r6._feats(fc, c, ids, rows)
            got = {m: gates_r6._goal_signal(fc, c, rows, m, fc.by_id[m][1], prof, p)[0] for m in ids}
        self.assertEqual(got, {served: True, member: True, other: False, remark: False})


class Goal2FrozenDefaultTests(StoreCase):
    env = R11

    def test_frozen_r11_counts_tags_not_entity_membership(self):
        goal = self.store.remember("Goal: Maya wants to run the Lisbon marathon in October", "project")["memory_id"]
        served = self.store.remember("Race number pickup for the Lisbon marathon is at the Belem expo hall", "other",
                                     tag=f"goal:{goal}")["memory_id"]
        member = self.store.remember("The Lisbon marathon training plan needs four long runs", "other")["memory_id"]
        with self.store._session() as c:
            fc = gates_r6._feature_cache(self.store)
            rows = gates_r6._snapshot(c, fc, None).rows
            p = gates_r6.params()
            prof = gates_r6._goal_profiles(fc, c, rows, p)
            gates_r6._goal2_prepare(fc, c, rows, p)
            gates_r6._feats(fc, c, [served, member], rows)
            s = gates_r6._goal_signal(fc, c, rows, served, fc.by_id[served][1], prof, p)
            m = gates_r6._goal_signal(fc, c, rows, member, fc.by_id[member][1], prof, p)
        self.assertEqual(s, (True, False, False))   # goal-related by the tag; a tag alone is not a sign of use
        self.assertEqual(m[0], False)


if __name__ == "__main__":
    unittest.main()
