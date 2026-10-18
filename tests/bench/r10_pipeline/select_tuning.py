"""Apply the declared round-10 tuning rule (TUNING_R10_GRID.md) to the runs in DIR (seeds 0-7)."""
import itertools
import json
import statistics
import sys
from pathlib import Path

D = Path(sys.argv[1])
SEEDS = range(8)


def load(bench, arm, tag=""):
    mid = "nocap" if bench == "stale" else "cap200"
    return [json.loads((D / f"{bench}_{mid}_{arm}_{s}{tag}.json").read_text()) for s in SEEDS]


def m(rows, k):
    return statistics.mean(r[k] for r in rows)


base = {b: load(b, "shipped") for b in ("real", "goal", "stale")}
r9 = {b: load(b, "r7x_nogoal_useful_ext") for b in ("real", "goal", "stale")}
out = []
for d, n, p in itertools.product((21, 30, 45), (7, 14), (0.05, 0.15, 0.30)):
    tag = f"_d{d}n{n}p{p}"
    r = {b: load(b, "r10_stale", tag) for b in ("real", "goal", "stale")}
    c = {
        "durable_0": all(x["durable_demoted_n"] == 0 for x in r["stale"]),
        "false_sup<=shipped": m(r["stale"], "false_supersession") <= m(base["stale"], "false_supersession") + 1e-12,
        "keep_both>=shipped": m(r["stale"], "keep_both_respected") >= m(base["stale"], "keep_both_respected") - 1e-12,
        "timestale_found>=0.95": m(r["stale"], "timestale_found_top3") >= 0.95,
        **{f"{k}>=shipped-1pp": m(r["real"], k) >= m(base["real"], k) - 0.01 for k in ("useful_facts_kept", "hit_preference", "hit_project")},
        "goal_serving>=r9-1pp": m(r["goal"], "goal_serving_survival") >= m(r9["goal"], "goal_serving_survival") - 0.01,
    }
    J = (m(r["real"], "latest_fact_correct") - m(r["real"], "stale_leak")
         + m(r["stale"], "status_latest_correct") - m(r["stale"], "status_stale_top1"))
    out.append({"STALE_DAYS": d, "STALE_NEWER_DAYS": n, "STALE_PENALTY": p, "J": J, "feasible": all(c.values()),
                "failed": [k for k, v in c.items() if not v],
                "real_stale_leak": m(r["real"], "stale_leak"), "real_latest": m(r["real"], "latest_fact_correct"),
                "status_latest": m(r["stale"], "status_latest_correct"), "unrelated_top1": m(r["stale"], "unrelated_top1"),
                "timestale_found": m(r["stale"], "timestale_found_top3"), "goal_serving": m(r["goal"], "goal_serving_survival")})
feas = [o for o in out if o["feasible"]]
default = next(o for o in out if (o["STALE_DAYS"], o["STALE_NEWER_DAYS"], o["STALE_PENALTY"]) == (30, 14, 0.15))
if feas:
    best = max(o["J"] for o in feas)
    near = [o for o in feas if best - o["J"] <= 0.005]
    pick = default if default in near else sorted(near, key=lambda o: (-o["STALE_DAYS"], o["STALE_PENALTY"], -o["STALE_NEWER_DAYS"]))[0]
else:
    pick = default
summary = {"baselines": {"shipped": {"real_stale_leak": m(base["real"], "stale_leak"), "real_latest": m(base["real"], "latest_fact_correct"),
                                     "status_latest": m(base["stale"], "status_latest_correct")},
                         "r9": {"real_stale_leak": m(r9["real"], "stale_leak"), "real_latest": m(r9["real"], "latest_fact_correct"),
                                "status_latest": m(r9["stale"], "status_latest_correct"), "goal_serving": m(r9["goal"], "goal_serving_survival")}},
           "grid": out, "n_feasible": len(feas), "picked": pick, "default_feasible": default["feasible"]}
print(json.dumps(summary, indent=1))
