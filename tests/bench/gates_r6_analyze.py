"""Round 6 analysis (pre-registered in GATES_R6_PREREG.md, an internal write-up). DESIGN EVIDENCE ONLY.

Reads the per-run JSON files written by gates_r6_run.py, pairs arms with `shipped` by seed and
applies the pre-registered tests and decision rule.

  python tests/bench/gates_r6_analyze.py --dir DIR --seeds 100-115 [--hard-seeds 200-215]
         [--latency-dir DIR] [--profile JSON] [--tag TAG] [--md OUT.md] [--json OUT.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
from pathlib import Path

ARMS = ["shipped", "gate_cap", "gate_cap+useful", "gate_cap+useful+ext", "gate_cap+sur+useful+ext"]
REAL_PRIMARY = {"hit_critical": True, "stale_leak": False, "latest_fact_correct": True, "throwaways_retained": False}
REAL_GUARD_PP = ["hit_preference", "hit_project", "useful_facts_kept"]
REAL_SECONDARY = ["critical_survival", "hit_evolving", "hit_repeated", "throwaway_share_of_store", "active_at_end"]
HARD_PRIMARY = {"useful_recall_hit": True, "stale_active": False, "latest_fact_correct": True, "useless_retained": False}
SETTINGS = ["tell", "notell", "notell_late"]
MIN_EFFECT = 0.01   # 1 percentage point
ALPHA = 0.05


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


def analyse_real(directory: Path, seeds: list[int], tag: str = "") -> dict:
    runs = {arm: load(directory, "real_cap200", arm, seeds, tag) for arm in ARMS}
    missing = {arm: [s for s, r in zip(seeds, rs) if r is None] for arm, rs in runs.items()}
    if any(missing.values()):
        return {"missing": missing}
    table = {arm: {m: mean([r[m] for r in rs]) for m in list(REAL_PRIMARY) + REAL_GUARD_PP + REAL_SECONDARY
                   + ["wrong_merges", "write_ms_mean", "change_queries"]} for arm, rs in runs.items()}
    for arm in ARMS:
        table[arm]["wrong_merges_total"] = sum(r["wrong_merges"] for r in runs[arm])
    tests = {}
    for arm in ARMS[1:]:
        tests[arm] = {m: compare([r[m] for r in runs[arm]], [r[m] for r in runs["shipped"]], up)
                      for m, up in REAL_PRIMARY.items()}
        for m in REAL_SECONDARY[:3]:
            tests[arm][m] = compare([r[m] for r in runs[arm]], [r[m] for r in runs["shipped"]], True)
    return {"table": table, "tests": tests, "seeds": seeds}


def analyse_hard(directory: Path, seeds: list[int], tag: str = "") -> dict:
    out = {}
    for setting in SETTINGS:
        runs = {arm: load(directory, f"hard_{setting}", arm, seeds, tag) for arm in ARMS}
        if any(r is None for rs in runs.values() for r in rs):
            out[setting] = {"missing": True}
            continue
        table = {arm: {m: mean([r[m] for r in rs]) for m in list(HARD_PRIMARY) + ["useful_retained", "recall_hit", "write_ms"]}
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
        diffs = [a - b for a, b in zip(vals, base)]
        out[arm] = {"mean_ms": statistics.mean(vals), "paired_diff_ms": statistics.mean(diffs),
                    "wilcoxon_p": wilcoxon_exact(diffs), "faster": statistics.mean(diffs) < 0 and wilcoxon_exact(diffs) < ALPHA,
                    "guard_ok": statistics.mean(diffs) <= 0.5}
    return out


def latency_profile(path: Path) -> dict:
    data = json.loads(path.read_text())
    out = {}
    for n, entry in data["sizes"].items():
        base = entry["arms"]["shipped"]["mean_ms"]
        out[n] = {arm: {"mean_ms": v["mean_ms"], "ratio": v["mean_ms"] / base, "first_write_ms": v["first_write_mean_ms"],
                        "guard_ok": (int(n) < 1000) or v["mean_ms"] <= 1.05 * base} for arm, v in entry["arms"].items()}
    return out


def decide(real: dict, lat_real: dict | None, lat_prof: dict | None, suite_ok: bool | None) -> dict:
    out = {}
    base = real["table"]["shipped"]
    for arm in ARMS[1:]:
        tests = real["tests"][arm]
        better = [m for m in REAL_PRIMARY if tests[m]["status"] == "better"]
        worse = [m for m in REAL_PRIMARY if tests[m]["status"] == "worse"]
        guards = {
            "wrong_merges_0": real["table"][arm]["wrong_merges_total"] == 0,
            **{f"{m}_within_1pp": real["table"][arm][m] >= base[m] - 0.01 for m in REAL_GUARD_PP},
        }
        if lat_real is not None and not lat_real.get("missing"):
            guards["latency_cap200_<=+0.5ms"] = lat_real[arm]["guard_ok"]
        if lat_prof is not None:
            for n, arms in lat_prof.items():
                if int(n) >= 1000 and arm in arms:
                    guards[f"latency_N{n}_<=+5%"] = arms[arm]["guard_ok"]
        if suite_ok is not None:
            guards["full_test_suite"] = suite_ok
        passes = len(better) >= 2 and not worse and all(guards.values())
        smarter = len(better) >= 2 and not worse
        faster = bool(lat_real and not lat_real.get("missing") and lat_real[arm]["faster"])
        out[arm] = {"better": better, "worse": worse, "guards": guards, "passes": passes,
                    "owner": "faster AND smarter" if (faster and smarter) else "smarter only" if smarter else
                             "faster only" if faster else "neither"}
    return out


def pct(v):
    return "-" if v is None else f"{100 * v:5.1f}%"


def markdown(real, hard, lat_real, lat_prof, decision) -> str:
    lines = []
    if real and "table" in real:
        lines.append(f"### Realistic bench (16 weeks, cap 200), seeds {real['seeds'][0]}-{real['seeds'][-1]} (n={len(real['seeds'])})\n")
        lines.append("| metric | " + " | ".join(ARMS) + " |")
        lines.append("|---|" + "---|" * len(ARMS))
        for m in list(REAL_PRIMARY) + REAL_GUARD_PP + REAL_SECONDARY[:3]:
            lines.append(f"| {m} | " + " | ".join(pct(real["table"][a][m]) for a in ARMS) + " |")
        lines.append("| wrong merges (total) | " + " | ".join(str(real["table"][a]["wrong_merges_total"]) for a in ARMS) + " |")
        lines.append("| active at end | " + " | ".join(f"{real['table'][a]['active_at_end']:.1f}" for a in ARMS) + " |")
        lines.append("\nPaired vs shipped (mean diff in pp; W = exact Wilcoxon two-sided p; S = sign test p; wins/losses by seed):\n")
        lines.append("| arm | metric | diff pp | W p | S p | wins/losses | status |")
        lines.append("|---|---|---|---|---|---|---|")
        for arm, tests in real["tests"].items():
            for m, t in tests.items():
                lines.append(f"| {arm} | {m} | {100 * t['mean_diff']:+.2f} | {t['wilcoxon_p']:.4f} | {t['sign_p']:.4f} | "
                             f"{t['wins']}/{t['losses']} | {t['status']} |")
    if hard:
        for setting, block in hard.items():
            if "table" not in block:
                continue
            lines.append(f"\n### Hard bench, setting {setting}\n")
            lines.append("| metric | " + " | ".join(ARMS) + " |")
            lines.append("|---|" + "---|" * len(ARMS))
            for m in list(HARD_PRIMARY) + ["useful_retained", "recall_hit"]:
                lines.append(f"| {m} | " + " | ".join(pct(block["table"][a][m]) for a in ARMS) + " |")
            lines.append("| wrong merges (total) | " + " | ".join(str(block["table"][a]["wrong_merges_total"]) for a in ARMS) + " |")
            lines.append("\n| arm | metric | diff pp | W p | wins/losses | status |")
            lines.append("|---|---|---|---|---|---|")
            for arm, tests in block["tests"].items():
                for m, t in tests.items():
                    lines.append(f"| {arm} | {m} | {100 * t['mean_diff']:+.2f} | {t['wilcoxon_p']:.4f} | {t['wins']}/{t['losses']} | {t['status']} |")
    if lat_real and not lat_real.get("missing"):
        lines.append("\n### Write latency, realistic bench cap 200 (sequential pass)\n")
        lines.append("| arm | mean ms | paired diff ms | W p | faster | guard (<= +0.5 ms) |")
        lines.append("|---|---|---|---|---|---|")
        for arm, v in lat_real.items():
            lines.append(f"| {arm} | {v['mean_ms']:.3f} | {v['paired_diff_ms']:+.3f} | {v['wilcoxon_p']:.4f} | {v['faster']} | {v['guard_ok']} |")
    if lat_prof:
        lines.append("\n### Write latency, profile (fresh processes, steady-state writes)\n")
        lines.append("| N | arm | mean ms | ratio to shipped | first (cold) write ms | guard (<= +5%) |")
        lines.append("|---|---|---|---|---|---|")
        for n, arms in lat_prof.items():
            for arm, v in arms.items():
                lines.append(f"| {n} | {arm} | {v['mean_ms']:.2f} | {v['ratio']:.3f} | {v['first_write_ms']:.1f} | {v['guard_ok']} |")
    if decision:
        lines.append("\n### Decision (pre-registered rule)\n")
        lines.append("| arm | better primaries | worse primaries | guardrails failed | passes | the owner's filter |")
        lines.append("|---|---|---|---|---|---|")
        for arm, d in decision.items():
            failed = [g for g, ok in d["guards"].items() if not ok]
            lines.append(f"| {arm} | {', '.join(d['better']) or '-'} | {', '.join(d['worse']) or '-'} | "
                         f"{', '.join(failed) or 'none'} | {'PASS' if d['passes'] else 'no'} | {d['owner']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--seeds", default="100-115")
    parser.add_argument("--hard-seeds", default="")
    parser.add_argument("--latency-dir", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--suite-ok", choices=["yes", "no"])
    parser.add_argument("--tag", default="")
    parser.add_argument("--md", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    seeds = seeds_of(args.seeds)
    real = analyse_real(args.dir, seeds, args.tag)
    hard = analyse_hard(args.dir, seeds_of(args.hard_seeds), args.tag) if args.hard_seeds else None
    lat_real = latency_real(args.latency_dir, seeds) if args.latency_dir else None
    lat_prof = latency_profile(args.profile) if args.profile else None
    suite = None if args.suite_ok is None else args.suite_ok == "yes"
    decision = decide(real, lat_real, lat_prof, suite) if "table" in real else None
    text = markdown(real, hard, lat_real, lat_prof, decision)
    print(text)
    if args.md:
        args.md.write_text(text)
    if args.json:
        args.json.write_text(json.dumps({"real": real, "hard": hard, "latency_real": lat_real, "latency_profile": lat_prof,
                                         "decision": decision}, indent=1, default=str))


if __name__ == "__main__":
    main()
