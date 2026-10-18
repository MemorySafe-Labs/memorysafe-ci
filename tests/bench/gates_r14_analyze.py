"""Round 14 analysis (pre-registered in GATES_R14_PREREG.md, an internal write-up).

The round-13 analysis (same primaries, Holm, distractor non-inferiority, hard-bench caveat, latency, suite and
stale guards, speed endpoints S1-S3, Linux cold first write guard) with arms shipped, r13 (reference: the r13
code exactly, git worktree of a5f820e) and r14 (the single primary arm: the same flags on the round-14 code).

New guardrails (all must hold for PASS), parsed from the junit XML of tests/test_gates_r14.py and the logs of
reviewer A's two probe scripts (run unchanged):
  G-probes      edge_probes.py: CONTENT_ONLY_CACHE_STALE False, ROLLBACK_CACHE_STALE False, INNER_ENTITY_CACHE_
                RETAINS_FORGOTTEN_TEXT False, the three STATUS lines mixed / uncertain / historical (LOG_WRITES_AFTER_
                FLAGS_DISABLED True is the documented, kept behaviour); every original r10 probe False; the
                ReviewerAEdgeProbeTests pass.
  G-cache       no forgotten text in any cache layer: CacheLayerTests and ContentOnlyTests pass.
  G-rollback    RollbackTests pass (injected failures on the real remember and eviction paths vs a fresh-store oracle).
  G-schemas     McpSchemaTests pass (fields declared with flags on, absent with flags off).
  G-classifier  StatusClassifierTests pass (mixed, quoted and past notes never age-demoted).
  G-lifecycle   ChangeLogLifecycleTests, SidecarResidueTests and StreamedRankingTests pass; no failure anywhere in
                test_gates_r14.
  G-offlog      flags off WITH the change log installed (profile arm r14_off_log): steady-state mean write
                <= 1.05 x shipped at N=1000 and N=5000 (trigger overhead).
  G-sidecar     no file besides the database, -wal/-shm and the state dir in any cold-probe run of any arm.
  G-firstwrite  (as r13) Linux cold first write, r14_capN <= 1.00 x base_cap at N=1000 and N=5000, normal AND
                single core.
Reported, not guards: throttled (cpulimit 50%), cgroup CPU quota 50%, cold page cache, cold pyc, the import
breakdown. EXTERNAL (pending, never decided here): the Mac cold first write measured by reviewer B.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates_r9_analyze as A  # noqa: E402
import gates_r10_analyze as B  # noqa: E402
import gates_r12_analyze as R  # noqa: E402
import gates_r13_analyze as C  # noqa: E402

A.ARMS = ["shipped", "r13", "r14"]
A.PRIMARY_ARMS = ["r14"]
A.REF = "r13"
PRIMARY_ARM, REF_ARM = "r14", "r13"
R.PRIMARY_ARM, R.REF_ARM = PRIMARY_ARM, REF_ARM
C.PRIMARY_ARM, C.REF_ARM = PRIMARY_ARM, REF_ARM
GUARD_CLASSES = {"G-probes": ["ReviewerAEdgeProbeTests"], "G-cache": ["CacheLayerTests", "ContentOnlyTests"],
                 "G-rollback": ["RollbackTests"], "G-schemas": ["McpSchemaTests"],
                 "G-classifier": ["StatusClassifierTests"],
                 "G-lifecycle": ["ChangeLogLifecycleTests", "SidecarResidueTests", "StreamedRankingTests"]}
COLD_MODES = ("normal", "single_core", "throttled", "quota", "cold_cache", "cold_pyc")


def junit(path: Path) -> dict:
    out: dict = {"by_class": {}, "failed": [], "total": 0}
    if not path.exists():
        return out
    for tc in ET.parse(path).getroot().iter("testcase"):
        cls = tc.get("classname", "").rsplit(".", 1)[-1]
        bad = any(ch.tag in ("failure", "error") for ch in tc)
        skipped = any(ch.tag == "skipped" for ch in tc)
        d = out["by_class"].setdefault(cls, {"passed": 0, "failed": 0, "skipped": 0})
        d["failed" if bad else "skipped" if skipped else "passed"] += 1
        out["total"] += 1
        if bad:
            out["failed"].append(f"{cls}.{tc.get('name')}")
    return out


def probes(edge: Path, orig: Path) -> dict:
    e = edge.read_text().splitlines() if edge.exists() else []
    o = orig.read_text().splitlines() if orig.exists() else []
    kv = {ln.split(" ", 1)[0]: ln.split(" ", 1)[1] for ln in e if " " in ln and not ln.startswith("STATUS")}
    status = [ln.rsplit(" ", 1)[1] for ln in e if ln.startswith("STATUS")]
    orig_kv = {ln.split()[0]: ln.split()[-1] for ln in o if ln.strip()}
    ok = (kv.get("CONTENT_ONLY_CACHE_STALE") == "False" and kv.get("ROLLBACK_CACHE_STALE", "").startswith("False")
          and kv.get("INNER_ENTITY_CACHE_RETAINS_FORGOTTEN_TEXT") == "False" and status == ["mixed", "uncertain", "historical"]
          and len(orig_kv) >= 7 and all(v == "False" for v in orig_kv.values()))
    return {"edge": kv, "status": status, "original_r10": orig_kv, "holds": ok}


def cold(paths: dict) -> dict:
    out: dict = {"table": {}, "guard": {}, "files_left": {}, "meta": {}}
    for mode, path in paths.items():
        if not path or not path.exists():
            out["table"][mode] = None
            continue
        d = json.loads(path.read_text())
        out["meta"][mode] = {k: d["meta"].get(k) for k in ("python", "platform", "cpus", "runs", "cpu", "throttle",
                                                           "cold_cache", "cold_pyc", "loadavg_start", "loadavg_end", "started")}
        out["table"][mode] = d["sizes"]
        for n, arms in d["sizes"].items():
            for arm, v in arms.items():
                out["files_left"][f"{mode}:{n}:{arm}"] = v.get("files_left", [])
            if mode in ("normal", "single_core"):
                c, x = arms.get("base_cap"), arms.get("r14_capN")
                out["guard"][f"{mode}:N{n}"] = bool(c and x and x["open_first_ms"] <= c["open_first_ms"])
    out["no_sidecar"] = bool(out["files_left"]) and all(not v for v in out["files_left"].values())
    out["holds"] = all(out["guard"].get(f"{m}:N{n}") for m in ("normal", "single_core") for n in ("1000", "5000"))
    return out


def offlog(profile: Path) -> dict:
    d = json.loads(profile.read_text())
    out = {}
    for n in ("1000", "5000"):
        e = d["sizes"].get(n, {}).get("arms", {})
        b, x = e.get("shipped"), e.get("r14_off_log")
        out[n] = {"shipped_ms": b and b["mean_ms"], "off_log_ms": x and x["mean_ms"],
                  "ratio": (x["mean_ms"] / b["mean_ms"]) if b and x else None}
    out["holds"] = all(out[n]["ratio"] is not None and out[n]["ratio"] <= 1.05 for n in ("1000", "5000"))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--seeds", default="3000-3015")
    p.add_argument("--goal-seeds", default="3100-3199")
    p.add_argument("--hard-seeds", default="3200-3215")
    p.add_argument("--stale-seeds", default="3300-3315")
    p.add_argument("--latency-dir", type=Path, required=True)
    p.add_argument("--profile", type=Path, required=True)
    p.add_argument("--junit", type=Path, required=True)
    p.add_argument("--edge-probes", type=Path, required=True)
    p.add_argument("--orig-probes", type=Path, required=True)
    p.add_argument("--importtime", type=Path)
    for m in COLD_MODES:
        p.add_argument(f"--cold-{m.replace('_', '-')}", type=Path)
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
    ju = junit(a.junit)
    pr = probes(a.edge_probes, a.orig_probes)
    cw = cold({m: getattr(a, f"cold_{m}") for m in COLD_MODES})
    ol = offlog(a.profile)
    dd = C.decision_diffs(a.dir, None)
    imp = json.loads(a.importtime.read_text()) if a.importtime and a.importtime.exists() else None
    gclass = {}
    for g, classes in GUARD_CLASSES.items():
        gclass[g] = all(c in ju["by_class"] and ju["by_class"][c]["failed"] == 0 and ju["by_class"][c]["passed"] > 0
                        for c in classes)
    for arm in A.ARMS[1:]:
        g = decision[arm]["guards"]
        if "guards" in stale:
            g.update(stale["guards"][arm])
        else:
            g["stale_bench_complete"] = False
        smarter = decision[arm]["smarter"]
        if arm == PRIMARY_ARM:
            g["G-probes: reviewer A's probes pass unchanged"] = gclass["G-probes"] and pr["holds"]
            g["G-cache: no forgotten text in any cache layer"] = gclass["G-cache"]
            g["G-rollback: consistent with fresh-store oracle"] = gclass["G-rollback"]
            g["G-schemas: added fields declared"] = gclass["G-schemas"]
            g["G-classifier: mixed/quoted/past not age-demoted"] = gclass["G-classifier"]
            g["G-lifecycle: lifecycle tests, no failure in test_gates_r14"] = gclass["G-lifecycle"] and not ju["failed"] and ju["total"] > 0
            g["G-offlog: flags off + log <= 1.05x shipped"] = ol["holds"]
            g["G-sidecar: no extra file in any cold run"] = cw["no_sidecar"]
            for k, v in cw["guard"].items():
                g[f"G-firstwrite linux {k} <= 1.0x 0.4.19cap"] = bool(v)
            ok = smarter and all(g.values())
            v = decision[arm]["verdict"]
            caveat = v[v.index("(hard"):] if "(hard" in v else ""
            decision[arm]["passes"] = ok
            decision[arm]["verdict"] = ("PASS" + (" with caveat " + caveat if caveat else "")) if ok else "no"
            decision[arm]["faster"] = sp["faster"]
            decision[arm]["faster_and_smarter"] = ("PENDING (Mac cold first write, external)" if (ok and sp["faster"]) else
                                                   "NO (" + ("smarter PASS only" if ok else "faster only" if sp["faster"] else "neither") + ")")
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
    L.append("\n### Speed endpoints (r14 vs shipped; round-12 definitions)\n")
    s1 = sp["S1"]
    if s1:
        L.append(f"- S1 sequential cap 200 (n={s1['n']}): shipped {s1['mean_shipped_ms']:.2f} ms, r13 {s1['mean_r10_ms']:.2f} ms, "
                 f"r14 {s1['mean_cand_ms']:.2f} ms; paired diff {s1['mean_diff_ms']:+.2f} ms, one-sided p {s1['one_sided_p']:.2g}, "
                 f"r14 faster on {s1['faster_seeds']}/{s1['n']} seeds -> {'MET' if s1['met'] else 'NOT met'}")
    for n in ("1000", "5000"):
        x, y = sp["S2"].get(n), sp["S3"].get(n)
        if x:
            L.append(f"- S2 N={n} mean: shipped {x['shipped_ms']:.2f} ms, r14 {x['cand_ms']:.2f} ms, ratio {x['ratio']:.2f}x "
                     f"(r13 {x['r10_ratio']:.2f}x), {x['runs']} runs -> {'MET' if x['met'] else 'NOT met'}")
            L.append(f"- S3 N={n} first write (median, in-bench): shipped {y['shipped_first_median_ms']:.1f} ms, r14 "
                     f"{y['cand_first_median_ms']:.1f} ms, ratio {y['ratio']:.2f}x (r13 {y['r10_ratio']:.2f}x) -> {'MET' if y['met'] else 'NOT met'}")
    L.append(f"- **Faster (S1 and S2 and S3): {'YES' if sp['faster'] else 'NO'}**")
    L.append(f"- Flags off with the change log installed (trigger overhead, G-offlog): " + ", ".join(
        f"N={n} {ol[n]['off_log_ms']:.2f} ms vs shipped {ol[n]['shipped_ms']:.2f} ms = {ol[n]['ratio']:.3f}x" for n in ("1000", "5000")
        if ol[n]["ratio"] is not None) + f" -> {'HOLDS' if ol['holds'] else 'DOES NOT HOLD'}")
    L.append("\n### Cold first write in a fresh process, Linux box (medians; open+first; x = vs 0.4.19 at the same cap)\n")
    for mode in COLD_MODES:
        block = cw["table"].get(mode)
        if not block:
            L.append(f"\n{mode}: not run\n")
            continue
        m = cw["meta"][mode]
        L.append(f"\n{mode} (python {m['python']}, runs {m['runs']}, cpu {m['cpu']}, throttle {m['throttle']}, "
                 f"load {m['loadavg_start']} -> {m['loadavg_end']})\n")
        L.append("| N | " + " | ".join(f"{arm} ms (x)" for arm in next(iter(block.values()))) + " |")
        L.append("|---|" + "---|" * len(next(iter(block.values()))))
        for n, arms in block.items():
            L.append(f"| {n} | " + " | ".join(f"{v['open_first_ms']:.1f} ({v.get('x_base_cap', 0):.2f})" for v in arms.values()) + " |")
    L.append(f"\nLinux first-write guard: {cw['guard']} -> {'HOLDS' if cw['holds'] else 'DOES NOT HOLD'}; no sidecar: {cw['no_sidecar']}")
    L.append("\n**Mac cold first write (EXTERNAL endpoint): PENDING** -- reviewer B runs tests/bench/cold_probe_mac.py; acceptance "
             "r14_capN <= 1.00 x base_cap at N=1000 and N=5000 (median of 11). Not decided by this analysis.")
    if imp:
        L.append("\n### Import breakdown (-X importtime, cumulative us, median of 7)\n")
        mods = sorted({k for d in imp.values() for k in d if k.startswith("memorysafe")})
        L.append("| module | " + " | ".join(imp) + " |")
        L.append("|---|" + "---|" * len(imp))
        for k in mods:
            L.append(f"| {k} | " + " | ".join(str(int(imp[arm][k]["cum_us"])) if k in imp[arm] else "-" for arm in imp) + " |")
        L.append("| lazy prototype modules loaded | " + " | ".join(str(imp[arm].get("_lazy_loaded", "-")) for arm in imp) + " |")
    L.append(f"\n### New guardrails\n")
    L.append(f"- test_gates_r14 by class: {ju['by_class']}; failed: {ju['failed'] or 'none'}")
    L.append(f"- reviewer A edge probes: {pr['edge']}; STATUS {pr['status']}; original r10 probes {pr['original_r10']} -> "
             f"{'HOLD' if pr['holds'] else 'DO NOT HOLD'}")
    L.append("\n### r14 vs r13 on the realistic and goal benches (descriptive, uncorrected)\n")
    L.append("| metric | diff pp | W p | wins/losses | status |")
    L.append("|---|---|---|---|---|")
    for k, x in ref.items():
        L.append(f"| {k} | {100 * x['mean_diff']:+.2f} | {x['wilcoxon_p']:.4f} | {x['wins']}/{x['losses']} | {x['status']} |")
    L.append("\n### r14 vs r13 decision differences (held-out pairs)\n")
    L.append(f"- pairs {dd['pairs']}, differing in any non-timing field {dd['differ']}; by bench {dd['by_bench']}")
    L.append(f"- by category {dd['by_category']}")
    L.append("- fields: " + ", ".join(f"{k} ({v})" for k, v in dd["by_field"].items()))
    L.append("\n### Decision (round-14 rule)\n")
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
                                      "stale": stale, "speed": sp, "junit": ju, "probes": pr, "cold": cw, "offlog": ol,
                                      "importtime": imp, "decision_diffs": dd, "cand_vs_r13": ref, "decision": decision},
                                     indent=1, default=str))


if __name__ == "__main__":
    main()
