"""Round 6 tuning selection on seeds 0-7 (objective declared in GATES_R6_PREREG.md).

  python tests/bench/gates_r6_tune.py --dir DIR --arm gate_cap+useful --tags TAG1 TAG2 ...
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

PRIMARY = {"hit_critical": True, "stale_leak": False, "latest_fact_correct": True, "throwaways_retained": False}
GUARD = ["hit_preference", "hit_project", "useful_facts_kept"]
HARD = {"useful_recall_hit": True, "stale_active": False, "latest_fact_correct": True, "useless_retained": False}


def load(directory: Path, name: str) -> dict | None:
    path = directory / name
    return json.loads(path.read_text()) if path.exists() else None


def gains(arm_runs, base_runs, metrics):
    out = {}
    for m, up in metrics.items():
        d = statistics.mean(a[m] - b[m] for a, b in zip(arm_runs, base_runs))
        out[m] = d if up else -d
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--arm", required=True)
    p.add_argument("--tags", nargs="+", required=True)
    p.add_argument("--seeds", default="0-7")
    a = p.parse_args()
    lo, hi = map(int, a.seeds.split("-"))
    seeds = range(lo, hi + 1)
    base = [load(a.dir, f"real_cap200_shipped_{s}.json") for s in seeds]
    hbase = [load(a.dir, f"hard_notell_shipped_{s}.json") for s in seeds]
    rows = []
    for order, tag in enumerate(a.tags):
        runs = [load(a.dir, f"real_cap200_{a.arm}_{s}{tag}.json") for s in seeds]
        hruns = [load(a.dir, f"hard_notell_{a.arm}_{s}{tag}.json") for s in seeds]
        if None in runs or None in base or None in hruns or None in hbase:
            print(tag, "MISSING")
            continue
        g = gains(runs, base, PRIMARY)
        guard_ok = all(statistics.mean(r[m] for r in runs) >= statistics.mean(b[m] for b in base) - 0.01 for m in GUARD) \
            and sum(r["wrong_merges"] for r in runs) == 0
        score = sum(v >= 0.01 for v in g.values()) - sum(v <= -0.01 for v in g.values())
        hg = gains(hruns, hbase, HARD)
        key = (guard_ok, score, round(sum(g.values()), 6), round(sum(hg.values()), 6), -order)
        rows.append((key, tag, g, hg, guard_ok))
    rows.sort(reverse=True)
    for key, tag, g, hg, ok in rows:
        print(f"{tag:40s} guard={ok} score={key[1]} sum_pp={100 * key[2]:+.2f} hard_sum_pp={100 * key[3]:+.2f} | "
              + " ".join(f"{m}={100 * v:+.1f}" for m, v in g.items()) + " | hard " + " ".join(f"{m}={100 * v:+.1f}" for m, v in hg.items()))
    if rows:
        print("CHOSEN", rows[0][1])


if __name__ == "__main__":
    main()
