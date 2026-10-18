"""Round 14: the owner's 2 outdated notes kept as fixtures (GATES_R14_PREREG.md s7). The classifier was NOT fitted
to them: these tests assert no particular outcome, they only record (ids and kinds, no text) how the round-14
status classifier reads them, to /workspace/gates_proto/real/r14_her_notes_kind.json, and check the rule
"only 'present' notes may be age-demoted" on them. Without the private fixture (box only) they skip."""

import json
import os
import unittest
from pathlib import Path

from memorysafe_chatgpt import gates_r6

FIX = Path(os.environ.get("MEMORYSAFE_R10_FIXTURE", "/workspace/gates_proto/real/private/fixtures_r10.json"))
OUT = FIX.parent.parent / "r14_her_notes_kind.json"


@unittest.skipUnless(FIX.exists(), "private real-data fixture not present")
class HerNotesR14Tests(unittest.TestCase):
    def test_record_kinds_of_her_two_outdated_notes(self):
        f = json.loads(FIX.read_text())
        kinds = {}
        for mid in f["outdated"]:
            text = f["memories"][mid]["content"]
            kind = gates_r6.status_kind(text)
            self.assertIn(kind, (None, "present", "mixed", "uncertain", "historical"))
            kinds[mid] = {"kind": kind, "age_demotable": kind == "present", "clauses": len(gates_r6._clauses(text))}
        OUT.write_text(json.dumps(kinds, indent=1))
