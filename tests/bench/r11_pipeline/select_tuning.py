"""Apply the declared round-11 tuning rule (TUNING_R11_GRID.md) to the runs in DIR (seeds 0-7)."""
import json
import statistics
import sys
from pathlib import Path

D = Path(sys.argv[1])
SEEDS = range(8)
PTS = [(0, 1, t, r) for t in (0, 1) for r in (2, 99)] + [(1, k, t, r) for k in (0, 1) for t in (0, 1) for r in (2, 99)]


def load(bench, arm, tag=""):
    return [json.loads((D / f"{bench}_cap200_{arm}_{s}{tag}.json").read_text()) for s in SEEDS]


def m(rows, k):
    return statistics.mean(r[k] for r in rows)


base = {b: load(b, "shipped") for b in ("real", "goal")}
r10 = {b: load(b, "r10_stale") for b in ("real", "goal")}
out = []
for e, k, t, r in PTS:
    tag = f"_e{e}k{k}t{t}r{r}"
    x = {b: load(b, "r11", tag) for b in ("real", "goal")}
    c1 = m(x["goal"], "goal_distractor_retained") <= m(base["goal"], "goal_distractor_retained") + 0.015 + 1e-12
    rest = {
        "serving>=r10": m(x["goal"], "goal_serving_survival") >= m(r10["goal"], "goal_serving_survival") - 1e-12,
        "stale_leak<=r10+1pp": m(x["real"], "stale_leak") <= m(r10["real"], "stale_leak") + 0.01,
        "latest>=r10-1pp": m(x["real"], "latest_fact_correct") >= m(r10["real"], "latest_fact_correct") - 0.01,
        "hit_critical>=r10-1pp": m(x["real"], "hit_critical") >= m(r10["real"], "hit_critical") - 0.01,
        "throwaways<=r10+1pp": m(x["real"], "throwaways_retained") <= m(r10["real"], "throwaways_retained") + 0.01,
        **{f"{q}>=shipped-1pp": m(x["real"], q) >= m(base["real"], q) - 0.01 for q in ("useful_facts_kept", "hit_preference", "hit_project")},
        "0_wrong_merges": all(z["wrong_merges"] == 0 for b in ("real", "goal") for z in x[b]),
    }
    out.append({"GOAL_ENTITY": e, "GOAL_KEYWORD": k, "TAG_EVIDENCE": t, "RECONF_MIN": r,
                "feasible": c1 and all(rest.values()), "rest_ok": all(rest.values()),
                "failed": ([] if c1 else ["distractor<=shipped+1.5pp"]) + [q for q, v in rest.items() if not v],
                "goal_serving": m(x["goal"], "goal_serving_survival"),
                "goal_serving_untagged": m(x["goal"], "goal_serving_survival_untagged"),
                "goal_distractor": m(x["goal"], "goal_distractor_retained"),
                "goal_distractor_untagged": m(x["goal"], "goal_distractor_retained_untagged"),
                "real_stale_leak": m(x["real"], "stale_leak"), "real_latest": m(x["real"], "latest_fact_correct"),
                "real_throwaways": m(x["real"], "throwaways_retained"), "real_hit_critical": m(x["real"], "hit_critical")})


def simple(o):
    return (o["GOAL_ENTITY"], o["TAG_EVIDENCE"], -o["RECONF_MIN"], -o["GOAL_KEYWORD"])


feas = [o for o in out if o["feasible"]]
if feas:
    best = max(o["goal_serving"] for o in feas)
    near = [o for o in feas if best - o["goal_serving"] <= 0.005]
    pick = sorted(near, key=lambda o: (o["goal_distractor"], simple(o)))[0]
    how = "best feasible point"
else:
    ok = [o for o in out if o["rest_ok"]]
    if ok:
        pick = sorted(ok, key=lambda o: (o["goal_distractor"], -o["goal_serving"], simple(o)))[0]
        how = "no feasible point: lowest distractor retention among points meeting constraints 2-6"
    else:
        pick = next(o for o in out if (o["GOAL_ENTITY"], o["GOAL_KEYWORD"], o["TAG_EVIDENCE"], o["RECONF_MIN"]) == (1, 1, 1, 2))
        how = "no point meets constraints 2-6: defaults frozen"
bl = {n: {"goal_serving": m(v["goal"], "goal_serving_survival"), "goal_distractor": m(v["goal"], "goal_distractor_retained"),
          "real_stale_leak": m(v["real"], "stale_leak"), "real_latest": m(v["real"], "latest_fact_correct"),
          "real_throwaways": m(v["real"], "throwaways_retained"), "real_hit_critical": m(v["real"], "hit_critical")}
      for n, v in (("shipped", base), ("r10_stale", r10))}
print(json.dumps({"baselines": bl, "grid": out, "n_feasible": len(feas), "picked": pick, "how": how}, indent=1))
