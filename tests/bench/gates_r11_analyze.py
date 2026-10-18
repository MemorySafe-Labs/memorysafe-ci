"""Round 11 analysis (pre-registered in GATES_R11_PREREG.md, an internal write-up).

Reuses the round-9 analysis (same primaries, tests, Holm, distractor non-inferiority, latency and suite
guards, decision rule) and the round-10 stale guards, with three arms: shipped, r10_stale (reference:
reported, cannot change the verdict) and r11 (the single primary arm). Adds the round-11 SPEED
ENDPOINTS (all vs shipped):
  S1 sequential realistic cap-200 write latency: mean paired diff (r11 - shipped) <= 0 AND one-sided
     Wilcoxon signed-rank p < 0.05 for "r11 faster" (exact, n=16);
  S2 profile mean write latency ratio r11/shipped <= 1.00 at N=1000 and at N=5000 (one interleaved
     pass, 12 runs per arm and size, 30 writes per run);
  S3 profile cold first write: median over runs, ratio r11/shipped <= 1.00 at N=1000 and at N=5000.
"Faster" = S1, S2 and S3 all met. "Faster AND smarter" = PASS (smarter, all guardrails) AND faster.
Also: the goal-bench tag split (descriptive) and r11 vs r10 on every realistic and goal metric.
"""
from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates_r9_analyze as A  # noqa: E402
import gates_r10_analyze as B  # noqa: E402

A.ARMS = ["shipped", "r10_stale", "r11"]
A.PRIMARY_ARMS = ["r11"]
A.REF = "r10_stale"
PRIMARY_ARM, REF_ARM = "r11", "r10_stale"
TAG_SECONDARY = [("goal_serving_survival_untagged", True), ("goal_serving_survival_tagged", True),
                 ("goal_distractor_retained_untagged", False), ("goal_memories_retained", True)]


def wilcoxon_one_sided_less(diffs: list[float]) -> float:
    """Exact one-sided p for H1: median diff < 0 (Wilcoxon signed-rank, average ranks for ties,
    zero differences dropped; exact enumeration over sign patterns for n <= 20)."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return 1.0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    if n <= 20:
        count = total = 0
        for signs in itertools.product((0, 1), repeat=n):
            s = sum(r for r, b in zip(ranks, signs) if b)
            total += 1
            count += s <= w_plus + 1e-9
        return count / total
    mu = n * (n + 1) / 4
    sd = (n * (n + 1) * (2 * n + 1) / 24) ** 0.5
    z = (w_plus - mu + 0.5) / sd
    return 0.5 * (1 + __import__("math").erf(z / 2 ** 0.5))


def speed(lat_dir: Path, seeds: list[int], profile: Path) -> dict:
    out: dict = {"S1": None, "S2": {}, "S3": {}}
    r = {a: A.load(lat_dir, "real_cap200", a, seeds) for a in ("shipped", REF_ARM, PRIMARY_ARM)}
    if all(x is not None for rs in r.values() for x in rs):
        diffs = [a["write_ms_mean"] - b["write_ms_mean"] for a, b in zip(r[PRIMARY_ARM], r["shipped"])]
        p1 = wilcoxon_one_sided_less(diffs)
        out["S1"] = {"mean_shipped_ms": statistics.mean(x["write_ms_mean"] for x in r["shipped"]),
                     "mean_r10_ms": statistics.mean(x["write_ms_mean"] for x in r[REF_ARM]),
                     "mean_r11_ms": statistics.mean(x["write_ms_mean"] for x in r[PRIMARY_ARM]),
                     "mean_diff_ms": statistics.mean(diffs), "one_sided_p": p1, "faster_seeds": sum(x < 0 for x in diffs),
                     "n": len(diffs), "met": statistics.mean(diffs) <= 0 and p1 < 0.05}
    data = json.loads(profile.read_text())
    for n in ("1000", "5000"):
        e = data["sizes"].get(n)
        if not e or PRIMARY_ARM not in e["arms"]:
            out["S2"][n] = out["S3"][n] = None
            continue
        b, x, ref = e["arms"]["shipped"], e["arms"][PRIMARY_ARM], e["arms"].get(REF_ARM)
        fb, fx = statistics.median(b["first_write_runs_ms"]), statistics.median(x["first_write_runs_ms"])
        out["S2"][n] = {"shipped_ms": b["mean_ms"], "r11_ms": x["mean_ms"], "ratio": x["mean_ms"] / b["mean_ms"],
                        "r10_ratio": (ref["mean_ms"] / b["mean_ms"]) if ref else None, "runs": len(x["run_means_ms"]),
                        "met": x["mean_ms"] <= b["mean_ms"] and len(x["run_means_ms"]) >= 12}
        out["S3"][n] = {"shipped_first_median_ms": fb, "r11_first_median_ms": fx, "ratio": fx / fb,
                        "r10_ratio": (statistics.median(ref["first_write_runs_ms"]) / fb) if ref else None,
                        "met": fx <= fb}
    out["faster"] = bool(out["S1"] and out["S1"]["met"] and all(v and v["met"] for v in out["S2"].values())
                         and all(v and v["met"] for v in out["S3"].values()))
    return out


def tag_split(directory: Path, gseeds: list[int]) -> dict:
    runs = {a: A.load(directory, "goal_cap200", a, gseeds) for a in A.ARMS}
    out = {}
    for k, up in TAG_SECONDARY:
        out[k] = {a: A.mean([r.get(k) for r in rs if r.get(k) is not None]) for a, rs in runs.items()}
        pairs = [(x.get(k), y.get(k)) for x, y in zip(runs[PRIMARY_ARM], runs["shipped"]) if x.get(k) is not None and y.get(k) is not None]
        out[k]["r11_vs_shipped"] = A.compare([p[0] for p in pairs], [p[1] for p in pairs], up)
        pairs = [(x.get(k), y.get(k)) for x, y in zip(runs[PRIMARY_ARM], runs[REF_ARM]) if x.get(k) is not None and y.get(k) is not None]
        out[k]["r11_vs_r10"] = A.compare([p[0] for p in pairs], [p[1] for p in pairs], up)
    out["caller_tags_mean"] = statistics.mean(r["caller_tags"] for r in runs[PRIMARY_ARM])
    return out


def vs_ref(directory: Path, seeds: list[int], goal_seeds: list[int]) -> dict:
    out = {}
    for b, m, up in A.PRIMARY + A.SECONDARY + [("goal", "goal_distractor_retained", False)]:
        ss = seeds if b == "real" else goal_seeds
        x = A.load(directory, f"{b}_cap200", PRIMARY_ARM, ss)
        r = A.load(directory, f"{b}_cap200", REF_ARM, ss)
        out[f"{b}:{m}"] = A.compare([z[m] for z in x], [z[m] for z in r], up)
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--seeds", default="1600-1615")
    p.add_argument("--goal-seeds", default="1700-1799")
    p.add_argument("--hard-seeds", default="1800-1815")
    p.add_argument("--stale-seeds", default="1900-1915")
    p.add_argument("--latency-dir", type=Path, required=True)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--suite-ok", choices=["yes", "no"], required=True)
    p.add_argument("--md", type=Path)
    p.add_argument("--json", type=Path)
    a = p.parse_args()
    seeds, gseeds = A.seeds_of(a.seeds), A.seeds_of(a.goal_seeds)
    res = A.analyse(a.dir, seeds, gseeds)
    if "table" not in res:
        print(json.dumps(res))
        sys.exit(1)
    hard = A.analyse_hard(a.dir, A.seeds_of(a.hard_seeds))
    lat_real = A.latency_real(a.latency_dir, seeds)
    lat_prof = A.latency_profile(a.profile)
    suite = a.suite_ok == "yes"
    # the r10 stale-bench analysis, with r11 in the r10 slot and r10 in the r9 slot
    B.A = A
    stale = _stale(a.dir, A.seeds_of(a.stale_seeds))
    res["vs_r6"] = {}
    decision = A.decide(res, hard, lat_real, lat_prof, suite)
    sp = speed(a.latency_dir, seeds, a.profile)
    for arm in A.ARMS[1:]:
        g = decision[arm]["guards"]
        if "guards" in stale:
            g.update(stale["guards"][arm])
        else:
            g["stale_bench_complete"] = False
        smarter = decision[arm]["smarter"]
        ok = smarter and all(g.values())
        if arm == PRIMARY_ARM:
            v = decision[arm]["verdict"]
            caveat = v[v.index("(hard"):] if "(hard" in v else ""
            decision[arm]["passes"] = ok
            decision[arm]["verdict"] = ("PASS" + (" with caveat " + caveat if caveat else "")) if ok else "no"
            decision[arm]["faster"] = sp["faster"]
            decision[arm]["faster_and_smarter"] = "YES" if (ok and sp["faster"]) else "NO (" + (
                "smarter PASS only" if ok else "faster only" if sp["faster"] else "neither") + ")"
        else:
            decision[arm]["passes"] = False
            decision[arm]["verdict"] = "reference only (would " + ("" if ok else "not ") + "pass the rule)"
            decision[arm]["faster_and_smarter"] = "n/a (reference)"
    ref = vs_ref(a.dir, seeds, gseeds)
    tags = tag_split(a.dir, gseeds)
    text = A.markdown(res, hard, lat_real, lat_prof, decision)
    L = []
    if "table" in stale:
        L.append("\n### Stale-status bench, seeds %d-%d (n=%d)\n" % (stale["seeds"][0], stale["seeds"][-1], len(stale["seeds"])))
        L.append("| metric | " + " | ".join(A.ARMS) + " |")
        L.append("|---|" + "---|" * len(A.ARMS))
        for k in stale["table"]["shipped"]:
            f = (lambda v: f"{v:.2f}") if k in ("durable_demoted_n", "durable_flagged_n", "stale_flags_total", "write_ms") else A.pct
            L.append(f"| {k} | " + " | ".join(f(stale["table"][arm][k]) for arm in A.ARMS) + " |")
        L.append("\nStale guards: " + "; ".join(f"{arm}: " + ", ".join(f"{g}={ok}" for g, ok in stale["guards"][arm].items())
                                               for arm in A.ARMS[1:]))
    else:
        L.append("\n### Stale-status bench: MISSING RUNS\n")
    L.append("\n### Speed endpoints (r11 vs shipped)\n")
    s1 = sp["S1"]
    if s1:
        L.append(f"- S1 sequential cap 200 (n={s1['n']}): shipped {s1['mean_shipped_ms']:.2f} ms, r10 {s1['mean_r10_ms']:.2f} ms, "
                 f"r11 {s1['mean_r11_ms']:.2f} ms; paired diff {s1['mean_diff_ms']:+.2f} ms, one-sided p {s1['one_sided_p']:.2g}, "
                 f"r11 faster on {s1['faster_seeds']}/{s1['n']} seeds -> {'MET' if s1['met'] else 'NOT met'}")
    else:
        L.append("- S1: MISSING")
    for n in ("1000", "5000"):
        x, y = sp["S2"].get(n), sp["S3"].get(n)
        if x:
            L.append(f"- S2 N={n} mean: shipped {x['shipped_ms']:.2f} ms, r11 {x['r11_ms']:.2f} ms, ratio {x['ratio']:.2f}x "
                     f"(r10 {x['r10_ratio']:.2f}x), {x['runs']} runs -> {'MET' if x['met'] else 'NOT met'}")
            L.append(f"- S3 N={n} first write (median): shipped {y['shipped_first_median_ms']:.1f} ms, r11 {y['r11_first_median_ms']:.1f} ms, "
                     f"ratio {y['ratio']:.2f}x (r10 {y['r10_ratio']:.2f}x) -> {'MET' if y['met'] else 'NOT met'}")
        else:
            L.append(f"- S2/S3 N={n}: MISSING")
    L.append(f"- **Faster (S1 and S2 and S3): {'YES' if sp['faster'] else 'NO'}**")
    L.append("\n### Goal bench split by the caller-tag model (descriptive, uncorrected)\n")
    L.append("| metric | shipped | r10 | r11 | r11 vs shipped (pp, p) | r11 vs r10 (pp, p) |")
    L.append("|---|---|---|---|---|---|")
    for k, _up in TAG_SECONDARY:
        t = tags[k]
        L.append(f"| {k} | {A.pct(t['shipped'])} | {A.pct(t['r10_stale'])} | {A.pct(t['r11'])} | "
                 f"{100 * t['r11_vs_shipped']['mean_diff']:+.1f}, {t['r11_vs_shipped']['wilcoxon_p']:.3g} | "
                 f"{100 * t['r11_vs_r10']['mean_diff']:+.1f}, {t['r11_vs_r10']['wilcoxon_p']:.3g} |")
    L.append(f"\nMean caller tags per goal run: {tags['caller_tags_mean']:.2f}")
    L.append("\n### r11 vs r10 on the realistic and goal benches (descriptive, uncorrected)\n")
    L.append("| metric | diff pp | W p | wins/losses | status |")
    L.append("|---|---|---|---|---|")
    for k, x in ref.items():
        L.append(f"| {k} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['wins']}/{x['losses']} | {x['status']} |")
    L.append("\n### Decision (round-11 rule)\n")
    L.append("| arm | better primaries (Holm) | worse primaries | guardrails failed | verdict | faster | faster AND smarter |")
    L.append("|---|---|---|---|---|---|---|")
    for arm, d in decision.items():
        failed = [g for g, ok in d["guards"].items() if not ok]
        L.append(f"| {arm} | {', '.join(d['better']) or '-'} | {', '.join(d['worse']) or '-'} | {', '.join(failed) or 'none'} | "
                 f"{d['verdict']} | {d.get('faster') if arm == PRIMARY_ARM else '-'} | {d['faster_and_smarter']} |")
    text += "\n".join(L) + "\n"
    print(text)
    if a.md:
        a.md.write_text(text)
    if a.json:
        a.json.write_text(json.dumps({"result": res, "hard": hard, "latency_real": lat_real, "latency_profile": lat_prof,
                                      "stale": stale, "speed": sp, "tag_split": tags, "r11_vs_r10": ref,
                                      "decision": decision}, indent=1, default=str))


def _stale(directory: Path, seeds: list[int]) -> dict:
    runs = {arm: A.load(directory, "stale_nocap", arm, seeds) for arm in A.ARMS}
    if any(r is None for rs in runs.values() for r in rs):
        return {"missing": {a: [s for s, r in zip(seeds, rs) if r is None] for a, rs in runs.items()}}
    keys = [k for k, _ in B.STALE_SECONDARY] + ["durable_demoted_n", "durable_flagged_n", "stale_flags_total", "write_ms"]
    table = {arm: {k: A.mean([r[k] for r in rs]) for k in keys} for arm, rs in runs.items()}
    guards = {arm: {
        "false_supersession_<=_shipped": table[arm]["false_supersession"] <= table["shipped"]["false_supersession"] + 1e-12,
        "durable_never_demoted": all(r["durable_demoted_n"] == 0 for r in runs[arm]),
        "keep_both_respected_>=_shipped": table[arm]["keep_both_respected"] >= table["shipped"]["keep_both_respected"] - 1e-12,
    } for arm in A.ARMS[1:]}
    return {"table": table, "guards": guards, "seeds": seeds}


if __name__ == "__main__":
    main()
