"""Round 10 analysis (pre-registered in GATES_R10_PREREG.md, an internal write-up).

Reuses the round-9 analysis unchanged (same primaries, tests, Holm, distractor non-inferiority,
latency and suite guards, decision rule) with three arms: shipped, the r9 candidate
(r7x_nogoal_useful_ext, reference: reported, cannot change the verdict) and the r10 candidate
(r10_stale, the single primary arm). Adds the round-10 guards from the stale-status bench:
  * false_supersession (unrelated date-sharing pairs): r10 mean <= shipped mean;
  * durable facts never demoted: durable_demoted_n == 0 on every seed;
  * keep-both respected: r10 mean keep_both_respected >= shipped mean;
and reports r10 vs r9 (descriptive) and the stale-bench secondaries.

  python tests/bench/gates_r10_analyze.py --dir DIR --seeds 1200-1215 --goal-seeds 1300-1399
     --hard-seeds 1400-1415 --stale-seeds 1500-1515 --latency-dir DIR --profile JSON --suite-ok yes|no
     --md OUT.md --json OUT.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates_r9_analyze as A  # noqa: E402

A.ARMS = ["shipped", "r7x_nogoal_useful_ext", "r10_stale"]
A.PRIMARY_ARMS = ["r10_stale"]
A.REF = "r7x_nogoal_useful_ext"
STALE_SECONDARY = [("status_latest_correct", True), ("status_stale_top1", False), ("unrelated_top1", True),
                   ("durable_top1", True), ("timestale_found_top3", True), ("reconfirmed_flagged", False),
                   ("tentative_supersession", False), ("chain_old_extinguished", False), ("false_supersession", False),
                   ("keep_both_respected", True)]


def analyse_stale(directory: Path, seeds: list[int]) -> dict:
    runs = {arm: A.load(directory, "stale_nocap", arm, seeds) for arm in A.ARMS}
    if any(r is None for rs in runs.values() for r in rs):
        return {"missing": {a: [s for s, r in zip(seeds, rs) if r is None] for a, rs in runs.items()}}
    keys = [k for k, _ in STALE_SECONDARY] + ["durable_demoted_n", "durable_flagged_n", "stale_flags_total", "write_ms"]
    table = {arm: {k: A.mean([r[k] for r in rs]) for k in keys} for arm, rs in runs.items()}
    tests = {arm: {k: A.compare([r[k] for r in runs[arm]], [r[k] for r in runs["shipped"]], up) for k, up in STALE_SECONDARY}
             for arm in A.ARMS[1:]}
    vs_r9 = {k: A.compare([r[k] for r in runs["r10_stale"]], [r[k] for r in runs["r7x_nogoal_useful_ext"]], up)
             for k, up in STALE_SECONDARY}
    guards = {arm: {
        "false_supersession_<=_shipped": table[arm]["false_supersession"] <= table["shipped"]["false_supersession"] + 1e-12,
        "durable_never_demoted": all(r["durable_demoted_n"] == 0 for r in runs[arm]),
        "keep_both_respected_>=_shipped": table[arm]["keep_both_respected"] >= table["shipped"]["keep_both_respected"] - 1e-12,
    } for arm in A.ARMS[1:]}
    return {"table": table, "tests": tests, "vs_r9": vs_r9, "guards": guards, "seeds": seeds}


def vs_r9(directory: Path, seeds: list[int], goal_seeds: list[int]) -> dict:
    out = {}
    for b, m, up in A.PRIMARY + A.SECONDARY:
        ss = seeds if b == "real" else goal_seeds
        a = A.load(directory, f"{b}_cap200", "r10_stale", ss)
        r = A.load(directory, f"{b}_cap200", "r7x_nogoal_useful_ext", ss)
        out[f"{b}:{m}"] = A.compare([x[m] for x in a], [x[m] for x in r], up)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--seeds", default="1200-1215")
    p.add_argument("--goal-seeds", default="1300-1399")
    p.add_argument("--hard-seeds", default="1400-1415")
    p.add_argument("--stale-seeds", default="1500-1515")
    p.add_argument("--latency-dir", type=Path)
    p.add_argument("--profile", type=Path)
    p.add_argument("--suite-ok", choices=["yes", "no"])
    p.add_argument("--md", type=Path)
    p.add_argument("--json", type=Path)
    a = p.parse_args()
    seeds, gseeds = A.seeds_of(a.seeds), A.seeds_of(a.goal_seeds)
    res = A.analyse(a.dir, seeds, gseeds)
    if "table" not in res:
        print(json.dumps(res))
        sys.exit(1)
    hard = A.analyse_hard(a.dir, A.seeds_of(a.hard_seeds))
    lat_real = A.latency_real(a.latency_dir, seeds) if a.latency_dir else None
    lat_prof = A.latency_profile(a.profile) if a.profile else None
    suite = None if a.suite_ok is None else a.suite_ok == "yes"
    stale = analyse_stale(a.dir, A.seeds_of(a.stale_seeds))
    res["vs_r6"] = {}
    decision = A.decide(res, hard, lat_real, lat_prof, suite)
    for arm in A.ARMS[1:]:
        g = decision[arm]["guards"]
        if "guards" in stale:
            g.update(stale["guards"][arm])
        else:
            g["stale_bench_complete"] = False
        smarter = decision[arm]["smarter"]
        passes = smarter and all(g.values()) and arm in A.PRIMARY_ARMS
        decision[arm]["passes"] = passes
        if arm in A.PRIMARY_ARMS:
            v = decision[arm]["verdict"]
            caveat = v[v.index("(hard"):] if "(hard" in v else ""
            decision[arm]["verdict"] = ("PASS" + (" with caveat " + caveat if caveat else "")) if passes else "no"
        else:
            decision[arm]["verdict"] = "reference only (would " + ("" if (smarter and all(g.values())) else "not ") + "pass the rule)"
    r9cmp = vs_r9(a.dir, seeds, gseeds)
    text = A.markdown(res, hard, lat_real, lat_prof, decision)
    L = ["\n### Stale-status bench (new in round 10), seeds %d-%d (n=%d)\n" % (stale["seeds"][0], stale["seeds"][-1], len(stale["seeds"]))
         if "table" in stale else "\n### Stale-status bench: MISSING RUNS\n"]
    if "table" in stale:
        L.append("| metric | " + " | ".join(A.ARMS) + " |")
        L.append("|---|" + "---|" * len(A.ARMS))
        for k in stale["table"]["shipped"]:
            f = (lambda v: f"{v:.2f}") if k in ("durable_demoted_n", "durable_flagged_n", "stale_flags_total", "write_ms") else A.pct
            L.append(f"| {k} | " + " | ".join(f(stale["table"][arm][k]) for arm in A.ARMS) + " |")
        L.append("\n| comparison | metric | diff pp | W p | wins/losses | status (uncorrected, descriptive) |")
        L.append("|---|---|---|---|---|---|")
        for arm, t in stale["tests"].items():
            for k, x in t.items():
                L.append(f"| {arm} vs shipped | {k} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['wins']}/{x['losses']} | {x['status']} |")
        for k, x in stale["vs_r9"].items():
            L.append(f"| r10 vs r9 | {k} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['wins']}/{x['losses']} | {x['status']} |")
        L.append("\nRound-10 guards: " + "; ".join(f"{arm}: " + ", ".join(f"{g}={ok}" for g, ok in stale["guards"][arm].items())
                                               for arm in A.ARMS[1:]))
    L.append("\n### r10 vs r9 candidate on the realistic and goal benches (descriptive, uncorrected)\n")
    L.append("| metric | diff pp | W p | wins/losses | status |")
    L.append("|---|---|---|---|---|")
    for k, x in r9cmp.items():
        L.append(f"| {k} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['wins']}/{x['losses']} | {x['status']} |")
    L.append("\n### Decision with the round-10 guards added\n")
    L.append("| arm | better primaries (Holm) | worse primaries | guardrails failed | verdict | faster AND smarter? |")
    L.append("|---|---|---|---|---|---|")
    for arm, d in decision.items():
        failed = [g for g, ok in d["guards"].items() if not ok]
        L.append(f"| {arm} | {', '.join(d['better']) or '-'} | {', '.join(d['worse']) or '-'} | {', '.join(failed) or 'none'} | "
                 f"{d['verdict']} | {d['faster_and_smarter']} |")
    text += "\n".join(L) + "\n"
    print(text)
    if a.md:
        a.md.write_text(text)
    if a.json:
        a.json.write_text(json.dumps({"result": res, "hard": hard, "latency_real": lat_real, "latency_profile": lat_prof,
                                      "stale": stale, "r10_vs_r9": r9cmp, "decision": decision}, indent=1, default=str))


if __name__ == "__main__":
    main()
