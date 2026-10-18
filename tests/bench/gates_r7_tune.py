"""Round 7 tuning objective (pre-registered grid and objective in GATES_R7_PREREG.md). Seeds 0-7 only.

  python tests/bench/gates_r7_tune.py --dir DIR --tags __a __b ... [--seeds 0-7]
Reads real_cap200_* and goal_cap200_* files of arm r7_useful_ext (tagged per combination) and the
untagged shipped files; ranks combinations lexicographically:
  1. guardrails (pref/project/useful facts within 1 pp of shipped; goal distractors <= shipped + 1 pp;
     no wrong merges)
  2. (# primaries improved by >= 1 pp) - (# worsened by >= 1 pp)
  3. sum of the primaries' gains in pp
  4. lower goal_distractor_retained, then the order the combinations are listed in (defaults first)
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gates_r7_analyze import PRIMARY, REAL_GUARD_PP, load, seeds_of  # noqa: E402


def score(directory: Path, tag: str, seeds: list[int], arm: str = "r7_useful_ext") -> dict:
    data = {b: (load(directory, f"{b}_cap200", arm, seeds, tag), load(directory, f"{b}_cap200", "shipped", seeds))
            for b in ("real", "goal")}
    if any(r is None for b in data.values() for side in b for r in side):
        return {"missing": True}
    m = lambda runs, k: statistics.mean(r[k] for r in runs)  # noqa: E731
    gains = {}
    for bench, metric, up in PRIMARY:
        a, s = data[bench]
        d = m(a, metric) - m(s, metric)
        gains[metric] = d if up else -d
    ra, rs = data["real"]
    ga, gs = data["goal"]
    guard = (all(m(ra, k) >= m(rs, k) - 0.01 for k in REAL_GUARD_PP)
             and m(ga, "goal_distractor_retained") <= m(gs, "goal_distractor_retained") + 0.01
             and sum(r["wrong_merges"] for r in ra + ga) == 0)
    net = sum(g >= 0.01 for g in gains.values()) - sum(g <= -0.01 for g in gains.values())
    return {"guard": guard, "net": net, "sum_pp": 100 * sum(gains.values()), "gains": gains,
            "distractors": m(ga, "goal_distractor_retained")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--tags", nargs="+", required=True)
    ap.add_argument("--seeds", default="0-7")
    args = ap.parse_args()
    seeds = seeds_of(args.seeds)
    rows = []
    for order, tag in enumerate(args.tags):
        s = score(args.dir, tag, seeds)
        if s.get("missing"):
            print(f"{tag:40s} missing")
            continue
        rows.append(((not s["guard"], -s["net"], -round(s["sum_pp"], 6), s["distractors"], order), tag, s))
    rows.sort()
    for _key, tag, s in rows:
        print(f"{tag:40s} guard={s['guard']} net={s['net']} sum_pp={s['sum_pp']:+.2f} distractors={100 * s['distractors']:.1f}% | "
              + " ".join(f"{k}={100 * v:+.1f}" for k, v in s["gains"].items()))
    if rows:
        print("CHOSEN", rows[0][1])


if __name__ == "__main__":
    main()
