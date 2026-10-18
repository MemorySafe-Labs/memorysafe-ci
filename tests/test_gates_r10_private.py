"""Round 10 checks on the owner's real cases. The fixture holds private text and lives only on the
prototype box (/workspace/gates_proto/real/private/, built by make_private_fixtures.py); it is never
committed. Without it these tests skip. This file contains ids only."""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates_r6

FIX = Path(os.environ.get("MEMORYSAFE_R10_FIXTURE", "/workspace/gates_proto/real/private/fixtures_r10.json"))


@unittest.skipUnless(FIX.exists(), "private real-data fixture not present")
class RealCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = json.loads(FIX.read_text())
        cls.text = {k: v["content"] for k, v in cls.f["memories"].items()}
        cls.common = frozenset(cls.f["common_entities"])

    def setUp(self):
        p = patch.dict(os.environ, {"MEMORYSAFE_GATE_STALE": "1"})
        p.start()
        self.addCleanup(p.stop)

    def test_fixture_shape(self):
        self.assertEqual(len(self.f["pairs"]), 3)
        self.assertEqual(len(self.f["relations"]), 22)
        self.assertEqual(len(self.f["supersession_pairs"]), 4)

    def test_pairs_1_and_2_are_not_replacements(self):
        for p in self.f["pairs"]:
            if p["via_replaces"]:
                continue
            with self.subTest(pair=p["pair"]):
                self.assertIsNone(gates_r6.replacement_evidence(self.text[p["via"]], self.text[p["old"]], common=self.common))

    def test_outdated_memories_are_status_claims(self):
        for mid in self.f["outdated"]:
            self.assertTrue(gates_r6.is_status_claim(self.text[mid]), mid)

    def test_keep_both_relations_never_supersede(self):
        from memorysafe_chatgpt.storage import MemoryStore
        import tempfile

        kb = [r for r in self.f["relations"] if r["decision"] == "KEEP_BOTH"]
        self.assertEqual(len(kb), 11)
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"MEMORYSAFE_DB_PATH": f"{d}/m.sqlite3",
                                                                         "MEMORYSAFE_STATE_DIR": f"{d}/s"}):
            store = MemoryStore(Path(f"{d}/m.sqlite3"))
            from memorysafe_chatgpt.lifecycle import record_relation
            ids = {}
            for r in kb:
                for k in (r["old_memory_id"], r["new_memory_id"]):
                    if k not in ids:
                        ids[k] = store.remember(self.text[k], self.f["memories"][k]["category"])["memory_id"]
            pairs = [(ids[r["old_memory_id"]], ids[r["new_memory_id"]]) for r in kb]
            pairs = [(a, b) for a, b in pairs if a != b]
            self.assertGreaterEqual(len(pairs), 9)
            with store._session() as c:
                for a, b in pairs:
                    record_relation(c, old_memory_id=a, new_memory_id=b, decision="KEEP_BOTH", status="resolved",
                                    reason="fixture", evidence="fixture", confidence=1.0, source="manual",
                                    created_at="2026-10-01T00:00:00+00:00")
                for a, b in pairs:
                    for old, new in ((a, b), (b, a)):
                        out = gates_r6._apply(store, c, old, new, [("a2 replacement", "x")], "2026-10-02T00:00:00+00:00")
                        self.assertFalse(out["applied"])
                        if c.execute("SELECT state FROM memories WHERE id = ?", (old,)).fetchone()[0] == "active":
                            self.assertTrue(out.get("keep_both"))

if __name__ == "__main__":
    unittest.main()
