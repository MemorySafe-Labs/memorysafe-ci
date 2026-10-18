"""Round 13 analysis (pre-registered in GATES_R13_PREREG.md, an internal write-up).

The round-12 analysis (same primaries, Holm, distractor non-inferiority, hard-bench caveat, latency,
suite and stale guards, speed endpoints S1-S3) with three arms: shipped, r12 (reference: reported,
cannot change the verdict; the r12 code exactly, git worktree of 6c44303) and r13 (the single primary
arm: r12 flags on the round-13 code). The round-12 decision-equivalence guard is replaced by:

New guardrails (all must hold for PASS):
  G-findings   every review finding has a passing regression test: tests/test_gates_r13.py all pass
               (pytest log) AND every reviewer A probe prints False (lumi_review_checks.py log);
  G-sidecar    no `<db>.gates-index*` file: no file other than the database, -wal/-shm and the state dir
               in any first-write run of any arm, AND the NoSidecar tests pass (part of G-findings);
  G-firstwrite cold first write = MemoryStore() + first remember() in a fresh process (open_ms + first_ms;
               the gate-module imports are charged to it), median over runs, r13 <= 1.00 x the comparator at
               N=1000 and N=5000, in the normal run AND the single-core (taskset -c 0) run. Comparator:
               pristine 0.4.19 (d9b7a76) under the same capacity (MEMORYSAFE_MAX_ACTIVE=N, base_cap).
Reported, not guards: the first remember() alone, process start -> first write done (total), shipped_cap,
pristine default (no cap), r12, the under-cap arms, the -X importtime breakdown.

Also: r13-vs-r12 decision differences on every held-out pair, categorised by bench and by which fields
differ, with an exploratory attribution to the open-conflict fix (L1) by an ablation arm (r13 with only
that fix reverted, runs in DIR_L1 if given).
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates_r9_analyze as A  # noqa: E402
import gates_r10_analyze as B  # noqa: E402
import gates_r12_analyze as R  # noqa: E402

A.ARMS = ["shipped", "r12", "r13"]
A.PRIMARY_ARMS = ["r13"]
A.REF = "r12"
PRIMARY_ARM, REF_ARM = "r13", "r12"
R.PRIMARY_ARM, R.REF_ARM = PRIMARY_ARM, REF_ARM
TIMING = ("ms", "wall")
WRITE_SIDE = ("merge", "retained", "survival", "kept", "share_of_store", "active_at_end", "extinguish", "demoted",
              "evict", "useful", "throwaway")


def findings(pytest_log: Path, probe_log: Path) -> dict:
    t = pytest_log.read_text() if pytest_log.exists() else ""
    m = re.search(r"(\d+) passed", t)
    failed = re.search(r"(\d+) (failed|error)", t)
    probes = [ln.split() for ln in (probe_log.read_text().splitlines() if probe_log.exists() else []) if ln.strip()]
    probes = [p for p in probes if len(p) == 2 and p[1] in ("True", "False")]
    return {"tests_passed": int(m.group(1)) if m else 0, "tests_failed": bool(failed),
            "probes": {p[0]: p[1] for p in probes},
            "holds": bool(m) and not failed and len(probes) >= 7 and all(p[1] == "False" for p in probes)}


def firstwrite(normal: Path, single: Path) -> dict:
    out = {"guard": {}, "table": {}, "files_left": {}}
    for mode, path in (("normal", normal), ("single_core", single)):
        if not path.exists():
            out["guard"][mode] = None
            continue
        data = json.loads(path.read_text())
        out["table"][mode] = {}
        for n, arms in data["sizes"].items():
            row = {}
            for arm, v in arms.items():
                row[arm] = {"open_first_ms": v["open_first_ms"], "first_ms": v["first_ms"], "total_ms": v["total_ms"],
                            "import_ms": v["import_ms"], "open_ms": v["open_ms"], "runs": v["runs"]}
                out["files_left"][f"{mode}:{n}:{arm}"] = v["files_left"]
            # open_first_ms = median over runs of (open + first remember) in the same process
            c, x = row.get("base_cap"), row.get(PRIMARY_ARM)
            for arm in row:
                if c:
                    row[arm]["ratio_vs_base_cap"] = row[arm]["open_first_ms"] / c["open_first_ms"]
                    row[arm]["first_ratio_vs_base_cap"] = row[arm]["first_ms"] / c["first_ms"]
                    row[arm]["total_ratio_vs_base_cap"] = row[arm]["total_ms"] / c["total_ms"]
            out["table"][mode][n] = row
            out["guard"][f"{mode}:N{n}"] = bool(c and x and x["open_first_ms"] <= c["open_first_ms"])
    out["no_sidecar"] = bool(out["files_left"]) and all(not v for v in out["files_left"].values())
    out["holds"] = (all(out["guard"].get(f"{m}:N{n}") for m in ("normal", "single_core") for n in ("1000", "5000")))
    return out


def decision_diffs(directory: Path, abl: Path | None) -> dict:
    cats = collections.Counter()
    fields = collections.Counter()
    benches = collections.Counter()
    l1 = collections.Counter()
    pairs = differ = 0
    for f in sorted(directory.glob(f"*_{PRIMARY_ARM}_*.json")):
        g = directory / f.name.replace(f"_{PRIMARY_ARM}_", f"_{REF_ARM}_")
        if not g.exists():
            continue
        a, b = json.loads(f.read_text()), json.loads(g.read_text())
        strip = lambda d: {k: v for k, v in d.items() if not any(t in k for t in TIMING) and k not in ("arm", "code")}
        sa, sb = strip(a), strip(b)
        pairs += 1
        if sa == sb:
            continue
        differ += 1
        bench = f.name.split(f"_{PRIMARY_ARM}_")[0]
        benches[bench] += 1
        keys = sorted(k for k in set(sa) | set(sb) if sa.get(k) != sb.get(k))
        for k in keys:
            fields[f"{bench}:{k}"] += 1
        write = any(any(w in k for w in WRITE_SIDE) for k in keys)
        cat = "write-side (what stays in recall)" if write else "find/order-side only (what is returned first)"
        cats[cat] += 1
        if abl is not None:
            h = abl / f.name.replace(f"_{PRIMARY_ARM}_", f"_{REF_ARM}_")
            if h.exists():
                sh = strip(json.loads(h.read_text()))
                if sh == sb:
                    l1["reverting L1 alone restores r12"] += 1
                elif sh == sa:
                    l1["unchanged by reverting L1 (other fixes)"] += 1
                else:
                    l1["partly L1 (reverting L1 changes it but not to r12)"] += 1
            else:
                l1["no ablation run"] += 1
    return {"pairs": pairs, "differ": differ, "by_bench": dict(benches), "by_category": dict(cats),
            "by_field": dict(fields.most_common()), "l1_attribution": dict(l1)}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--seeds", default="2500-2515")
    p.add_argument("--goal-seeds", default="2600-2699")
    p.add_argument("--hard-seeds", default="2700-2715")
    p.add_argument("--stale-seeds", default="2800-2815")
    p.add_argument("--latency-dir", type=Path, required=True)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--fw-normal", type=Path, required=True)
    p.add_argument("--fw-single", type=Path, required=True)
    p.add_argument("--findings-pytest", type=Path, required=True)
    p.add_argument("--findings-probes", type=Path, required=True)
    p.add_argument("--dir-l1", type=Path)
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
    B.A = A
    stale = R._stale(a.dir, A.seeds_of(a.stale_seeds))
    res["vs_r6"] = {}
    decision = A.decide(res, hard, lat_real, lat_prof, suite)
    sp = R.speed(a.latency_dir, seeds, a.profile)
    fd = findings(a.findings_pytest, a.findings_probes)
    fw = firstwrite(a.fw_normal, a.fw_single)
    dd = decision_diffs(a.dir, a.dir_l1)
    for arm in A.ARMS[1:]:
        g = decision[arm]["guards"]
        if "guards" in stale:
            g.update(stale["guards"][arm])
        else:
            g["stale_bench_complete"] = False
        smarter = decision[arm]["smarter"]
        if arm == PRIMARY_ARM:
            g["every_finding_has_passing_regression_test"] = fd["holds"]
            g["no_sidecar_file"] = fw["no_sidecar"]
            for k, v in fw["guard"].items():
                g[f"cold_first_write_{k}_<=1.0x_0.4.19cap"] = bool(v)
            ok = smarter and all(g.values())
            v = decision[arm]["verdict"]
            caveat = v[v.index("(hard"):] if "(hard" in v else ""
            decision[arm]["passes"] = ok
            decision[arm]["verdict"] = ("PASS" + (" with caveat " + caveat if caveat else "")) if ok else "no"
            decision[arm]["faster"] = sp["faster"]
            decision[arm]["faster_and_smarter"] = "YES" if (ok and sp["faster"]) else "NO (" + (
                "smarter PASS only" if ok else "faster only" if sp["faster"] else "neither") + ")"
        else:
            ok = smarter and all(g.values())
            decision[arm]["passes"] = False
            decision[arm]["verdict"] = "reference only (would " + ("" if ok else "not ") + "pass the round-12 rule)"
            decision[arm]["faster_and_smarter"] = "n/a (reference)"
    ref = R.vs_ref(a.dir, seeds, gseeds)
    text = A.markdown(res, hard, lat_real, lat_prof, decision)
    L = []
    if "table" in stale:
        L.append("\n### Stale-status bench, seeds %d-%d (n=%d)\n" % (stale["seeds"][0], stale["seeds"][-1], len(stale["seeds"])))
        L.append("| metric | " + " | ".join(A.ARMS) + " |")
        L.append("|---|" + "---|" * len(A.ARMS))
        for k in stale["table"]["shipped"]:
            f = (lambda v: f"{v:.2f}") if k in ("durable_demoted_n", "durable_flagged_n", "stale_flags_total", "write_ms") else A.pct
            L.append(f"| {k} | " + " | ".join(f(stale["table"][arm][k]) for arm in A.ARMS) + " |")
    else:
        L.append("\n### Stale-status bench: MISSING RUNS\n")
    L.append("\n### Speed endpoints (r13 vs shipped; round-12 definitions)\n")
    s1 = sp["S1"]
    if s1:
        L.append(f"- S1 sequential cap 200 (n={s1['n']}): shipped {s1['mean_shipped_ms']:.2f} ms, r12 {s1['mean_r10_ms']:.2f} ms, "
                 f"r13 {s1['mean_cand_ms']:.2f} ms; paired diff {s1['mean_diff_ms']:+.2f} ms, one-sided p {s1['one_sided_p']:.2g}, "
                 f"r13 faster on {s1['faster_seeds']}/{s1['n']} seeds -> {'MET' if s1['met'] else 'NOT met'}")
    for n in ("1000", "5000"):
        x, y = sp["S2"].get(n), sp["S3"].get(n)
        if x:
            L.append(f"- S2 N={n} mean: shipped {x['shipped_ms']:.2f} ms, r13 {x['cand_ms']:.2f} ms, ratio {x['ratio']:.2f}x "
                     f"(r12 {x['r10_ratio']:.2f}x), {x['runs']} runs -> {'MET' if x['met'] else 'NOT met'}")
            L.append(f"- S3 N={n} first write (median, in-bench): shipped {y['shipped_first_median_ms']:.1f} ms, r13 "
                     f"{y['cand_first_median_ms']:.1f} ms, ratio {y['ratio']:.2f}x (r12 {y['r10_ratio']:.2f}x) -> {'MET' if y['met'] else 'NOT met'}")
    L.append(f"- **Faster (S1 and S2 and S3): {'YES' if sp['faster'] else 'NO'}**")
    L.append("\n### Cold first write in a fresh process (new guardrail; medians)\n")
    for mode, block in fw["table"].items():
        for n, row in block.items():
            L.append(f"\n{mode}, N={n}\n")
            L.append("| arm | open+first ms | x 0.4.19cap | first ms | x | total ms | x | import ms | open ms | runs |")
            L.append("|---|---|---|---|---|---|---|---|---|---|")
            for arm, v in row.items():
                L.append(f"| {arm} | {v['open_first_ms']:.2f} | {v.get('ratio_vs_base_cap', 0):.2f} | {v['first_ms']:.2f} | "
                         f"{v.get('first_ratio_vs_base_cap', 0):.2f} | {v['total_ms']:.1f} | {v.get('total_ratio_vs_base_cap', 0):.2f} | "
                         f"{v['import_ms']:.2f} | {v['open_ms']:.2f} | {v['runs']} |")
    L.append(f"\nFirst-write guard: {fw['guard']} -> {'HOLDS' if fw['holds'] else 'DOES NOT HOLD'}; no sidecar in any run: {fw['no_sidecar']}")
    L.append(f"\nFindings guard: test_gates_r13 passed {fd['tests_passed']}, any failure {fd['tests_failed']}; probes {fd['probes']} -> "
             f"{'HOLDS' if fd['holds'] else 'DOES NOT HOLD'}")
    L.append("\n### r13 vs r12 on the realistic and goal benches (descriptive, uncorrected)\n")
    L.append("| metric | diff pp | W p | wins/losses | status |")
    L.append("|---|---|---|---|---|")
    for k, x in ref.items():
        L.append(f"| {k} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['wins']}/{x['losses']} | {x['status']} |")
    L.append("\n### r13 vs r12 decision differences (held-out pairs)\n")
    L.append(f"- pairs {dd['pairs']}, differing in any non-timing field {dd['differ']}; by bench {dd['by_bench']}")
    L.append(f"- by category {dd['by_category']}")
    L.append(f"- L1 (open-conflict) ablation attribution (exploratory): {dd['l1_attribution']}")
    L.append("- fields: " + ", ".join(f"{k} ({v})" for k, v in dd["by_field"].items()))
    L.append("\n### Decision (round-13 rule)\n")
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
                                      "stale": stale, "speed": sp, "findings": fd, "firstwrite": fw, "decision_diffs": dd,
                                      "cand_vs_r12": ref, "decision": decision}, indent=1, default=str))


if __name__ == "__main__":
    main()
