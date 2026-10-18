"""Round 11 checks on the owner's three labelled pairs and the other fixture texts. The fixture holds
private text and lives only on the prototype box (built by make_private_fixtures.py, never
committed). Without it these tests skip. This file contains ids only."""

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates_r6, lifecycle, storage

FIX = Path(os.environ.get("MEMORYSAFE_R10_FIXTURE", "/workspace/gates_proto/real/private/fixtures_r10.json"))
R11 = {"MEMORYSAFE_GATE_STALE": "1", "MEMORYSAFE_GATE_FAST": "1", "MEMORYSAFE_GATE_GOAL2": "1",
       "MEMORYSAFE_GATE_EXTINCTION": "1", "MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_USEFUL": "1"}


@unittest.skipUnless(FIX.exists(), "private real-data fixture not present")
class RealCaseR11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = json.loads(FIX.read_text())
        cls.text = {k: v["content"] for k, v in cls.f["memories"].items()}
        cls.common = frozenset(cls.f["common_entities"])

    def setUp(self):
        p = patch.dict(os.environ, R11)
        p.start()
        self.addCleanup(p.stop)

    def test_pairs_1_and_2_still_not_replacements_with_r11_flags(self):
        for p in self.f["pairs"]:
            if p["via_replaces"]:
                continue
            with self.subTest(pair=p["pair"]):
                self.assertIsNone(gates_r6.replacement_evidence(self.text[p["via"]], self.text[p["old"]], common=self.common))

    def test_all_three_outdated_sides_are_status_claims(self):
        for mid in self.f["outdated"]:
            self.assertTrue(gates_r6.is_status_claim(self.text[mid]), mid)

    def test_fast_path_same_lifecycle_verdicts_on_fixture_texts(self):
        ids = sorted(self.text)
        rows = [{"id": k, "content": self.text[k]} for k in ids]
        for k in ids:
            others = [r for r in rows if r["id"] != k]
            got = []
            for pin in (False, True):
                storage._FAST_PIN.append(pin)
                try:
                    v = lifecycle.consider_incoming(self.text[k], others)
                finally:
                    storage._FAST_PIN.pop()
                got.append(None if v is None else (v["action"], v["reason"], v["evidence"], v["confidence"], v["old"]["id"]))
            self.assertEqual(got[0], got[1], k)

    def test_pair_verdicts_unchanged_by_fast(self):
        for p in self.f["pairs"]:
            got = []
            for pin in (False, True):
                storage._FAST_PIN.append(pin)
                try:
                    got.append(lifecycle.classify_pair(self.text[p["via"]], self.text[p["old"]]))
                finally:
                    storage._FAST_PIN.pop()
            self.assertEqual(got[0]["action"], got[1]["action"], p["pair"])
            self.assertEqual(got[0]["reason"], got[1]["reason"], p["pair"])


if __name__ == "__main__":
    unittest.main()
