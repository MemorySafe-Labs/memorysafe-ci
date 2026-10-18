"""Round 8 CONFIRMATORY analysis (pre-registered in GATES_R8_PREREG.md, an internal write-up).

A copy of gates_r7_analyze.py (same tests, guardrails and decision rule) with the round-8 arms:
the single primary arm r7x_nogoal_useful_ext (the candidate) and the reference arm r7_useful_ext,
which is reported but cannot change the verdict. Holm runs across the five primaries of the
candidate, and jointly across all primary-arm x primary tests if PRIMARY_ARMS ever has more
than one arm. The candidate is also compared with the reference (secondary, descriptive).

  python tests/bench/gates_r8_analyze.py --dir DIR [--seeds 300-315] [--goal-seeds 400-415]
         [--hard-seeds 500-515] [--latency-dir DIR] [--profile JSON] [--suite-ok yes|no]
         [--tag TAG] [--md OUT.md] [--json OUT.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
from pathlib import Path

ARMS = ["shipped", "r7x_nogoal_useful_ext", "r7_useful_ext"]
PRIMARY_ARMS = ["r7x_nogoal_useful_ext"]
REF = "r7_useful_ext"   # (internal keys still named vs_r6 / mean_vs_r6 / r6: they mean "vs REF")
# (bench, metric, higher is better)
PRIMARY = [("real", "stale_leak", False), ("real", "latest_fact_correct", True), ("real", "hit_critical", True),
           ("goal", "goal_serving_survival", True), ("real", "throwaways_retained", False)]
REAL_GUARD_PP = ["hit_preference", "hit_project", "useful_facts_kept"]
SECONDARY = [("goal", "hit_goal_serving", True), ("goal", "goal_memories_retained", True),
             ("real", "critical_survival", True), ("real", "hit_evolving", True), ("real", "hit_repeated", True),
             ("goal", "stale_leak", False), ("goal", "latest_fact_correct", True), ("goal", "hit_critical", True)]
HARD_PRIMARY = {"useful_recall_hit": True, "stale_active": False, "latest_fact_correct": True, "useless_retained": False}
SETTINGS = ["tell", "notell", "notell_late"]
MIN_EFFECT = 0.01   # 1 percentage point
ALPHA = 0.05
FIRST_WRITE_MAX = 1.20


def seeds_of(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def wilcoxon_exact(diffs: list[float]) -> float:
    """Two-sided exact Wilcoxon signed-rank p (zeros dropped, average ranks for ties)."""
    d = [x for x in diffs if abs(x) > 1e-12]
    n = len(d)
    if n == 0:
        return 1.0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(abs(d[order[j + 1]]) - abs(d[order[i]])) < 1e-12:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    total = sum(ranks)
    observed = sum(r for r, x in zip(ranks, d) if x > 0)
    centre = total / 2
    extreme = abs(observed - centre) - 1e-9
    count = 0
    for signs in itertools.product((0, 1), repeat=n):
        w = sum(r for r, s in zip(ranks, signs) if s)
        if abs(w - centre) >= extreme:
            count += 1
    return count / 2 ** n


def sign_test(diffs: list[float]) -> float:
    pos = sum(1 for x in diffs if x > 1e-12)
    neg = sum(1 for x in diffs if x < -1e-12)
    n = pos + neg
    if n == 0:
        return 1.0
    k = min(pos, neg)
    p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def compare(arm_vals: list[float], base_vals: list[float], higher: bool) -> dict:
    diffs = [a - b for a, b in zip(arm_vals, base_vals) if a is not None and b is not None]
    mean = statistics.mean(diffs) if diffs else 0.0
    gain = mean if higher else -mean
    p_w = wilcoxon_exact(diffs)
    p_s = sign_test(diffs)
    status = "better" if (gain >= MIN_EFFECT and p_w < ALPHA) else "worse" if (gain <= -MIN_EFFECT and p_w < ALPHA) else "no clear difference"
    # (for the primaries this status is replaced by the Holm-adjusted one in holm())
    return {"mean_diff": mean, "gain": gain, "wilcoxon_p": p_w, "sign_p": p_s, "n": len(diffs),
            "wins": sum(1 for x in diffs if (x > 1e-12) == higher and abs(x) > 1e-12),
            "losses": sum(1 for x in diffs if (x > 1e-12) != higher and abs(x) > 1e-12), "status": status}


def load(directory: Path, prefix: str, arm: str, seeds: list[int], tag: str = "") -> list[dict]:
    out = []
    for seed in seeds:
        path = directory / f"{prefix}_{arm}_{seed}{tag}.json"
        out.append(json.loads(path.read_text()) if path.exists() else None)
    return out


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def holm(tests: dict[str, dict]) -> None:
    """Holm step-down across the given tests (in place): adds holm_p and sets status from it."""

    names = sorted(tests, key=lambda m: tests[m]["wilcoxon_p"])
    m = len(names)
    running = 0.0
    for i, name in enumerate(names):
        running = max(running, min(1.0, (m - i) * tests[name]["wilcoxon_p"]))
        t = tests[name]
        t["holm_p"] = running
        t["status"] = ("better" if (t["gain"] >= MIN_EFFECT and running < ALPHA)
                       else "worse" if (t["gain"] <= -MIN_EFFECT and running < ALPHA) else "no clear difference")


def runs_for(directory: Path, bench: str, seeds: list[int], tag: str = "") -> dict | None:
    runs = {arm: load(directory, f"{bench}_cap200", arm, seeds, tag) for arm in ARMS}
    missing = {arm: [s for s, r in zip(seeds, rs) if r is None] for arm, rs in runs.items()}
    return runs if not any(missing.values()) else {"missing": missing}


def analyse(directory: Path, seeds: list[int], goal_seeds: list[int], tag: str = "") -> dict:
    data = {"real": runs_for(directory, "real", seeds, tag), "goal": runs_for(directory, "goal", goal_seeds, tag)}
    for bench, runs in data.items():
        if "missing" in runs:
            return {"missing": {bench: runs["missing"]}}
    metrics = {"real": [m for b, m, _ in PRIMARY + SECONDARY if b == "real"] + REAL_GUARD_PP + ["active_at_end", "write_ms_mean"],
               "goal": [m for b, m, _ in PRIMARY + SECONDARY if b == "goal"] + ["goal_distractor_retained"] + REAL_GUARD_PP}
    table = {bench: {arm: {m: mean([r[m] for r in rs]) for m in metrics[bench]} for arm, rs in runs.items()}
             for bench, runs in data.items()}
    for bench, runs in data.items():
        for arm in ARMS:
            table[bench][arm]["wrong_merges_total"] = sum(r["wrong_merges"] for r in runs[arm])

    def tests_vs(base_arm: str, arms: list[str]) -> dict:
        out = {}
        for arm in arms:
            prim = {m: compare([r[m] for r in data[b][arm]], [r[m] for r in data[b][base_arm]], up) for b, m, up in PRIMARY}
            holm(prim)
            sec = {f"{b}:{m}": compare([r[m] for r in data[b][arm]], [r[m] for r in data[b][base_arm]], up)
                   for b, m, up in SECONDARY}
            dis = compare([r["goal_distractor_retained"] for r in data["goal"][arm]],
                          [r["goal_distractor_retained"] for r in data["goal"][base_arm]], False)
            out[arm] = {"primary": prim, "secondary": sec, "goal_distractor_retained": dis}
        return out

    tests = tests_vs("shipped", ARMS[1:])
    if len(PRIMARY_ARMS) > 1:   # joint Holm across arms x primaries
        joint = {f"{a}|{m}": tests[a]["primary"][m] for a in PRIMARY_ARMS for m in tests[a]["primary"]}
        holm(joint)
    return {"table": table, "tests": tests,
            "vs_r6": tests_vs(REF, PRIMARY_ARMS),
            "seeds": seeds, "goal_seeds": goal_seeds}


def analyse_hard(directory: Path, seeds: list[int], tag: str = "") -> dict:
    out = {}
    for setting in SETTINGS:
        runs = {arm: load(directory, f"hard_{setting}", arm, seeds, tag) for arm in ARMS}
        if any(r is None for rs in runs.values() for r in rs):
            out[setting] = {"missing": True}
            continue
        table = {arm: {m: mean([r[m] for r in rs]) for m in list(HARD_PRIMARY) + ["useful_retained", "recall_hit"]}
                 for arm, rs in runs.items()}
        for arm in ARMS:
            table[arm]["wrong_merges_total"] = sum(r["wrong_merges"] for r in runs[arm])
        tests = {arm: {m: compare([r[m] for r in runs[arm]], [r[m] for r in runs["shipped"]], up)
                       for m, up in HARD_PRIMARY.items()} for arm in ARMS[1:]}
        out[setting] = {"table": table, "tests": tests}
    return out


def latency_real(directory: Path, seeds: list[int]) -> dict:
    runs = {arm: load(directory, "real_cap200", arm, seeds) for arm in ARMS}
    if any(r is None for rs in runs.values() for r in rs):
        return {"missing": True}
    out = {}
    for arm in ARMS:
        vals = [r["write_ms_mean"] for r in runs[arm]]
        base = [r["write_ms_mean"] for r in runs["shipped"]]
        r6 = [r["write_ms_mean"] for r in runs[REF]]
        diffs = [a - b for a, b in zip(vals, base)]
        d6 = [a - b for a, b in zip(vals, r6)]
        out[arm] = {"mean_ms": statistics.mean(vals), "paired_diff_ms": statistics.mean(diffs),
                    "wilcoxon_p": wilcoxon_exact(diffs),
                    "faster": statistics.mean(diffs) < 0 and wilcoxon_exact(diffs) < ALPHA,
                    "guard_ok": statistics.mean(diffs) <= 0.5,
                    "vs_r6_diff_ms": statistics.mean(d6), "vs_r6_p": wilcoxon_exact(d6)}
    return out


def latency_profile(path: Path) -> dict:
    data = json.loads(path.read_text())
    out = {}
    for n, entry in data["sizes"].items():
        base = entry["arms"]["shipped"]
        base_first = statistics.median(base["first_write_runs_ms"])
        r6 = entry["arms"].get(REF)
        out[n] = {}
        for arm, v in entry["arms"].items():
            first = statistics.median(v["first_write_runs_ms"])
            out[n][arm] = {"mean_ms": v["mean_ms"], "ratio": v["mean_ms"] / base["mean_ms"],
                           "first_write_median_ms": first, "first_write_mean_ms": v["first_write_mean_ms"],
                           "first_ratio": first / base_first,
                           "guard_mean_ok": v["mean_ms"] <= 1.05 * base["mean_ms"],
                           "guard_first_ok": first <= FIRST_WRITE_MAX * base_first,
                           "mean_vs_r6": (v["mean_ms"] / r6["mean_ms"]) if r6 else None}
    return out


def decide(res: dict, hard: dict | None, lat_real: dict | None, lat_prof: dict | None, suite_ok: bool | None) -> dict:
    out = {}
    t = res["table"]
    for arm in ARMS[1:]:
        prim = res["tests"][arm]["primary"]
        better = [m for m in prim if prim[m]["status"] == "better"]
        worse = [m for m in prim if prim[m]["status"] == "worse"]
        guards = {
            "wrong_merges_0": t["real"][arm]["wrong_merges_total"] == 0 and t["goal"][arm]["wrong_merges_total"] == 0
            and (not hard or all(b["table"][arm]["wrong_merges_total"] == 0 for b in hard.values() if "table" in b)),
            **{f"{m}_within_1pp": t["real"][arm][m] >= t["real"]["shipped"][m] - 0.01 for m in REAL_GUARD_PP},
            "goal_distractor_retained_<=+1pp": t["goal"][arm]["goal_distractor_retained"]
            <= t["goal"]["shipped"]["goal_distractor_retained"] + 0.01,
        }
        if lat_real is not None and not lat_real.get("missing"):
            guards["latency_cap200_<=+0.5ms"] = lat_real[arm]["guard_ok"]
        if lat_prof is not None:
            for n, arms in lat_prof.items():
                if int(n) >= 1000 and arm in arms:
                    guards[f"latency_N{n}_mean_<=+5%"] = arms[arm]["guard_mean_ok"]
                    guards[f"cold_first_write_N{n}_<=1.2x"] = arms[arm]["guard_first_ok"]
        if suite_ok is not None:
            guards["full_test_suite"] = suite_ok
        # round 8: every pre-registered measurement must be present for a verdict
        guards["measurements_complete"] = bool(
            suite_ok is not None and lat_real is not None and not lat_real.get("missing") and lat_prof is not None
            and all(str(n) in lat_prof and arm in lat_prof[str(n)] for n in (1000, 5000))
            and hard is not None and all("tests" in b for b in hard.values()))
        smarter = len(better) >= 2 and not worse
        passes = smarter and all(guards.values())
        caveat = []
        if hard:
            for setting, block in hard.items():
                if "tests" in block:
                    n_worse = sum(1 for x in block["tests"][arm].values() if x["status"] == "worse")
                    if n_worse >= 2:
                        caveat.append(setting)
        faster = bool(lat_real and not lat_real.get("missing") and lat_real[arm]["faster"])
        if arm not in PRIMARY_ARMS:
            passes = False
        verdict = ("PASS with caveat (hard: " + ", ".join(caveat) + ")") if (passes and caveat) else "PASS" if passes else "no"
        if arm not in PRIMARY_ARMS:
            verdict = "reference only (would " + ("" if (smarter and all(guards.values())) else "not ") + "pass the rule)"
        out[arm] = {"better": better, "worse": worse, "guards": guards, "passes": passes, "verdict": verdict,
                    "faster": faster, "smarter": smarter,
                    "faster_and_smarter": "YES" if (faster and smarter) else "NO (" + (
                        "smarter only" if smarter else "faster only" if faster else "neither") + ")"}
    return out


def pct(v):
    return "-" if v is None else f"{100 * v:5.1f}%"


def markdown(res, hard, lat_real, lat_prof, decision) -> str:
    L = []
    if res and "table" in res:
        for bench, seeds in (("real", res["seeds"]), ("goal", res["goal_seeds"])):
            name = "Realistic bench" if bench == "real" else "Goal variant of the realistic bench"
            L.append(f"### {name} (16 weeks, cap 200), seeds {seeds[0]}-{seeds[-1]} (n={len(seeds)})\n")
            L.append("| metric | " + " | ".join(ARMS) + " |")
            L.append("|---|" + "---|" * len(ARMS))
            for m in res["table"][bench]["shipped"]:
                if m in ("wrong_merges_total", "active_at_end", "write_ms_mean"):
                    continue
                L.append(f"| {m} | " + " | ".join(pct(res["table"][bench][a][m]) for a in ARMS) + " |")
            L.append("| wrong merges (total) | " + " | ".join(str(res["table"][bench][a]["wrong_merges_total"]) for a in ARMS) + " |")
            L.append("")
        for title, block in (("vs shipped", res["tests"]), ("candidate vs r7_useful_ext reference (secondary)", res["vs_r6"])):
            L.append(f"\nPrimaries, paired by seed, {title} (diff in pp; W = exact Wilcoxon two-sided p; Holm = Holm-adjusted "
                     "across the 5 primaries of the arm; wins/losses by seed):\n")
            L.append("| arm | metric | diff pp | W p | Holm p | S p | wins/losses | status |")
            L.append("|---|---|---|---|---|---|---|---|")
            for arm, tests in block.items():
                for m, x in tests["primary"].items():
                    L.append(f"| {arm} | {m} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['holm_p']:.4f} | "
                             f"{x['sign_p']:.4f} | {x['wins']}/{x['losses']} | {x['status']} |")
                d = tests["goal_distractor_retained"]
                L.append(f"| {arm} | (guard) goal_distractor_retained | {100 * d['mean_diff']:+.2f} | {d['wilcoxon_p']:.4f} | - | "
                         f"{d['sign_p']:.4f} | {d['wins']}/{d['losses']} | - |")
            L.append("\nSecondary (uncorrected):\n")
            L.append("| arm | metric | diff pp | W p | wins/losses |")
            L.append("|---|---|---|---|---|")
            for arm, tests in block.items():
                for m, x in tests["secondary"].items():
                    L.append(f"| {arm} | {m} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['wins']}/{x['losses']} |")
    if hard:
        for setting, block in hard.items():
            if "table" not in block:
                continue
            L.append(f"\n### Hard toy bench, setting {setting}\n")
            L.append("| metric | " + " | ".join(ARMS) + " |")
            L.append("|---|" + "---|" * len(ARMS))
            for m in list(HARD_PRIMARY) + ["useful_retained", "recall_hit"]:
                L.append(f"| {m} | " + " | ".join(pct(block["table"][a][m]) for a in ARMS) + " |")
            L.append("| wrong merges (total) | " + " | ".join(str(block["table"][a]["wrong_merges_total"]) for a in ARMS) + " |")
            L.append("\n| arm | metric | diff pp | W p | wins/losses | status |")
            L.append("|---|---|---|---|---|---|")
            for arm, tests in block["tests"].items():
                for m, x in tests.items():
                    L.append(f"| {arm} | {m} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['wins']}/{x['losses']} | {x['status']} |")
    if lat_real and not lat_real.get("missing"):
        L.append("\n### Write latency, realistic bench cap 200 (sequential pass)\n")
        L.append("| arm | mean ms | diff vs shipped ms | W p | faster than shipped | guard (<= +0.5 ms) | diff vs r7 ref ms | W p vs r7 ref |")
        L.append("|---|---|---|---|---|---|---|---|")
        for arm, v in lat_real.items():
            L.append(f"| {arm} | {v['mean_ms']:.3f} | {v['paired_diff_ms']:+.3f} | {v['wilcoxon_p']:.4f} | {v['faster']} | "
                     f"{v['guard_ok']} | {v['vs_r6_diff_ms']:+.3f} | {v['vs_r6_p']:.4f} |")
    if lat_prof:
        L.append("\n### Write latency, profile (fresh processes; no sidecar file; first write = median over runs)\n")
        L.append("| N | arm | mean ms | mean / shipped | mean / r7 ref | first write ms (median) | first / shipped | first (mean) | "
                 "guard mean <= +5% | guard first <= 1.2x |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for n, arms in lat_prof.items():
            for arm, v in arms.items():
                r6 = "-" if v["mean_vs_r6"] is None else f"{v['mean_vs_r6']:.3f}"
                L.append(f"| {n} | {arm} | {v['mean_ms']:.2f} | {v['ratio']:.3f} | {r6} | {v['first_write_median_ms']:.1f} | "
                         f"{v['first_ratio']:.2f} | {v['first_write_mean_ms']:.1f} | {v['guard_mean_ok']} | {v['guard_first_ok']} |")
    if decision:
        L.append("\n### Decision (pre-registered rule)\n")
        L.append("| arm | better primaries (Holm) | worse primaries | guardrails failed | verdict | faster AND smarter? |")
        L.append("|---|---|---|---|---|---|")
        for arm, d in decision.items():
            failed = [g for g, ok in d["guards"].items() if not ok]
            L.append(f"| {arm} | {', '.join(d['better']) or '-'} | {', '.join(d['worse']) or '-'} | "
                     f"{', '.join(failed) or 'none'} | {d['verdict']} | {d['faster_and_smarter']} |")
    return "\n".join(L) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--seeds", default="300-315")
    parser.add_argument("--goal-seeds", default="400-415")
    parser.add_argument("--hard-seeds", default="")
    parser.add_argument("--latency-dir", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--suite-ok", choices=["yes", "no"])
    parser.add_argument("--tag", default="")
    parser.add_argument("--md", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    res = analyse(args.dir, seeds_of(args.seeds), seeds_of(args.goal_seeds), args.tag)
    hard = analyse_hard(args.dir, seeds_of(args.hard_seeds), args.tag) if args.hard_seeds else None
    lat_real = latency_real(args.latency_dir, seeds_of(args.seeds)) if args.latency_dir else None
    lat_prof = latency_profile(args.profile) if args.profile else None
    suite = None if args.suite_ok is None else args.suite_ok == "yes"
    decision = decide(res, hard, lat_real, lat_prof, suite) if "table" in res else None
    text = markdown(res, hard, lat_real, lat_prof, decision) if "table" in res else json.dumps(res)
    print(text)
    if args.md:
        args.md.write_text(text)
    if args.json:
        args.json.write_text(json.dumps({"result": res, "hard": hard, "latency_real": lat_real, "latency_profile": lat_prof,
                                         "decision": decision}, indent=1, default=str))


if __name__ == "__main__":
    main()
