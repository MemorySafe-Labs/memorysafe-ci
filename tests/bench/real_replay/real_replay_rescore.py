"""Round 10: score real-replay runs twice, (1) as originally pre-registered and (2) with the owner's labels
(all three flagged pairs are outdated; pairs 1-2 by time, not by the paired memory). Exploratory.

Reads the per-run JSONs (ids and numbers only), the read-only store copy (to rebuild sets A and B by the
pre-registered rules) and the PRIVATE fixture for the labels. Writes aggregates (numbers only).

  python real_replay_rescore.py --runs DIR [--runs DIR2 ...] --out FILE.json
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import sqlite3
from pathlib import Path

SRC = Path("/workspace/gates_proto/real/source/memorysafe_copy.sqlite3")
FIX = Path("/workspace/gates_proto/real/private/fixtures_r10.json")
HAZ = __import__("re").compile(r"\b(allerg\w*|danger\w*|safety|unsafe|emergency|never|must not|do not|password|secret|medical|medication)\b", 2)
SUPERSEDING = {"WEAKEN", "PROPOSE_WEAKEN", "EXTINGUISH"}
LIVE_STATUS = {"weakened", "proposed", "extinguished", "applied"}


def sets() -> dict:
    c = sqlite3.connect(f"file:{SRC}?mode=ro&immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    mems = [dict(r) for r in c.execute("SELECT * FROM memories")]
    forgotten = {r[0] for r in c.execute("SELECT memory_id FROM decision_events WHERE decision='FORGET' AND memory_id IS NOT NULL")}
    c.close()
    live = [m for m in mems if m["id"] not in forgotten]
    A = [m["id"] for m in live if m["protected"] or m["importance"] >= 0.8 or m["category"] == "decision" or HAZ.search(m["content"])]
    B = [m["id"] for m in live if (m["recall_count"] or 0) > 0]
    return {"A": A, "B": B}


def score(run: dict, S: dict, F: dict) -> dict:
    L = set(F["outdated"]) | set(F["outdated_implied"])
    alive = set(run["final_active_orig_ids"])
    o = {}
    # (1) original scoring, as pre-registered in REAL_REPLAY_PREREG.md
    o["orig_A"] = run["A_survived"] / run["A_n"]
    o["orig_B_surv"] = run["B_survived"] / run["B_n"]
    o["orig_B_hit3"] = run["B_hit3"] / run["B_n"]
    o["orig_C_leak"] = run["C_stale_leak"]
    o["orig_C_latest"] = run["C_latest_correct"]
    o["orig_K_found"] = run["K_both_found"]
    o["orig_E2_retained"] = run["E2_retained"] / max(1, run["E2_n"])
    # (2) label scoring
    assert len(S["A"]) == run["A_n"] and len(S["B"]) == run["B_n"], "set rebuild mismatch"
    A2 = [x for x in S["A"] if x not in L]
    B2 = [x for x in S["B"] if x not in L]
    miss = set(run["B_miss_ids"])
    o["lab_A"] = sum(x in alive for x in A2) / len(A2)
    o["lab_B_surv"] = sum(x in alive for x in B2) / len(B2)
    o["lab_B_hit3"] = sum(x not in miss for x in B2) / len(B2)
    keepC = [p for p in run["C_pairs"] if not (p["old"] in L and p["new"] in L)]
    dropC = [p for p in run["C_pairs"] if p["old"] in L and p["new"] in L]
    o["lab_C_n"] = len(keepC)
    o["lab_C_latest"] = sum(p["top1_new"] for p in keepC)
    o["lab_C_leak"] = sum(p["top1_old"] for p in keepC) + sum(p["top1_old"] or p["top1_new"] for p in dropC)
    keepK = [p for p in run["K_pairs"] if p["old"] not in L and p["new"] not in L]
    o["lab_K_n"] = len(keepK)
    o["lab_K_found"] = sum(p["old_found"] and p["new_found"] for p in keepK)
    acts = run.get("gate_actions")
    stand = set(run.get("stale_standing_orig_ids") or [])
    if acts is not None:
        sup = [(a, b) for a, b, d, st in acts if d in SUPERSEDING and st in LIVE_STATUS]
        ever_sup = [(a, b) for a, b, d, st in acts if d in SUPERSEDING]
        flags_ever = {a for a, _b, d, _s in acts if d == "STALE_FLAG"}
        real_sup = {tuple(x) for x in F["supersession_pairs"]} | {(p["old"], p["via"]) for p in F["pairs"] if p["via_replaces"]}
        o["ext_actions_ever"] = len(ever_sup)
        o["ext_actions_standing"] = len(sup)
        o["ext_outcome_correct"] = sum(a in L for a, _b in ever_sup)
        o["ext_reasoning_correct"] = sum((a, b) in real_sup for a, b in ever_sup)
        o["stale_flags_ever"] = len(flags_ever)
        o["stale_standing"] = len(stand)
        o["stale_standing_on_L"] = len(stand & L)
        o["stale_standing_on_A"] = len(stand & set(A2))
        for p in F["pairs"]:
            k = f"pair{p['pair']}"
            o[f"{k}_superseded_by_other_ever"] = (p["old"], p["via"]) in set(ever_sup)
            o[f"{k}_superseded_by_other_standing"] = (p["old"], p["via"]) in set(sup)
            o[f"{k}_flagged_ever"] = p["old"] in flags_ever
            o[f"{k}_flagged_standing"] = p["old"] in stand
    o["L_marked"] = sum((x not in alive) or (x in stand) or (acts is not None and any(a == x and d in SUPERSEDING and st in LIVE_STATUS for a, _b, d, st in acts)) for x in L)
    o["L_n"] = len(L)
    return o


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", action="append", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    F = json.loads(FIX.read_text())
    S = sets()
    groups: dict[str, list] = collections.defaultdict(list)
    digests: dict[str, set] = collections.defaultdict(set)
    for d in a.runs:
        for f in sorted(glob.glob(f"{d}/*.json")):
            run = json.loads(Path(f).read_text())
            key = f"{run['mode']}|cap{run['cap']}|{run['arm']}|{'ur' if run.get('user_resolutions') else 'plain'}|{Path(d).name}"
            groups[key].append(score(run, S, F))
            digests[key].add(run["outcome_digest"])
    out = {}
    for k, rows in sorted(groups.items()):
        agg = {}
        for m in rows[0]:
            vals = [r[m] for r in rows if m in r]
            agg[m] = vals[0] if all(v == vals[0] for v in vals) else {"mean": sum(vals) / len(vals), "values": vals}
        agg["reps"] = len(rows)
        agg["digests"] = sorted(digests[k])
        out[k] = agg
    Path(a.out).write_text(json.dumps(out, indent=1, default=str))
    print("wrote", a.out, len(out), "groups")


if __name__ == "__main__":
    main()
