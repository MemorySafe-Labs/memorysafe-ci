"""Build the PRIVATE round-10 fixtures from the read-only copy of the real store.

Writes /workspace/gates_proto/real/private/fixtures_r10.json (mode 0600). That file contains memory
text and is never committed; this script and the tests that read the fixture contain ids only.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "src"))

SRC = Path("/workspace/gates_proto/real/source/memorysafe_copy.sqlite3")
OUT = Path("/workspace/gates_proto/real/private/fixtures_r10.json")
# the owner's labels (round 10): all three flagged pairs are genuinely outdated; pairs 1 and 2 because time
# moved on, NOT because the paired memory replaced them; pair 3 is a real replacement.
PAIRS = [
    {"pair": 1, "old": "MS-20260902-3C36E9D3", "via": "MS-20260902-04F18F36", "old_outdated": True, "via_replaces": False},
    {"pair": 2, "old": "MS-20260826-BCCBAA90", "via": "MS-20260926-48BB2EDC", "old_outdated": True, "via_replaces": False},
    {"pair": 3, "old": "MS-20260826-BCCBAA90", "via": "MS-20261006-BF874B3B", "old_outdated": True, "via_replaces": True},
]
OUTDATED = ["MS-20260902-3C36E9D3", "MS-20260826-BCCBAA90"]
OUTDATED_IMPLIED = ["MS-20260822-0977CBD3"]   # superseded by BCCBAA90 in her own store, so older still


def main() -> None:
    from memorysafe_chatgpt import gates_r6

    c = sqlite3.connect(f"file:{SRC}?mode=ro&immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    mems = {r["id"]: dict(r) for r in c.execute("SELECT id, content, category, created_at, updated_at, state FROM memories")}
    forgotten = {r[0] for r in c.execute("SELECT memory_id FROM decision_events WHERE decision = 'FORGET' AND memory_id IS NOT NULL")}
    rels = [dict(r) for r in c.execute("SELECT old_memory_id, new_memory_id, decision, status FROM memory_relations ORDER BY id")]
    c.close()
    live = [m for k, m in mems.items() if k not in forgotten]
    sup = [r for r in rels if r["decision"] == "SUPERSEDE" and r["old_memory_id"] not in forgotten and r["new_memory_id"] not in forgotten]
    need = {p["old"] for p in PAIRS} | {p["via"] for p in PAIRS} | set(OUTDATED_IMPLIED)
    for r in rels:
        need |= {r["old_memory_id"], r["new_memory_id"]}
    fixture = {
        "pairs": PAIRS, "outdated": OUTDATED, "outdated_implied": OUTDATED_IMPLIED,
        "relations": rels, "supersession_pairs": [[r["old_memory_id"], r["new_memory_id"]] for r in sup],
        "forgotten": sorted(forgotten),
        "common_entities": sorted(gates_r6.common_entities(live)),
        "n_live": len(live),
        "memories": {k: mems[k] for k in sorted(need) if k in mems},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(fixture, f, indent=1)
    tmp.replace(OUT)
    print(f"wrote {OUT}: {len(rels)} relations, {len(sup)} supersession pairs, {len(fixture['memories'])} texts")


if __name__ == "__main__":
    main()
