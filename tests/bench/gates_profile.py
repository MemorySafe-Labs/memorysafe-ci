"""Write-path timing for each gate at several store sizes. DESIGN EVIDENCE ONLY.

For each size N a template store with N active memories is built once (synthetic text,
flags off). Each (arm, run) then copies that store into a temp dir, opens it in a FRESH
python process (so caches start cold, as for a CLI call), and times WRITES further
`remember()` calls with the capacity set to N, so every accepted write overflows the cap
by one and the eviction/gate path runs on every write. Reported: wall-clock ms per write,
mean and p95 over all runs x writes (steady = after the first write of each process), and
the first write on its own (cold caches).

Arms
  nogate        no capacity, no gate: the cost of a write with nothing to evict
  shipped       MEMORYSAFE_MAX_ACTIVE=N, shipped eviction order
  gate_cap      MEMORYSAFE_GATE_CAPACITY (cap N)
  gate_sur      MEMORYSAFE_GATE_SURPRISE only (no cap)
  gate_cap+sur  both
  gate_cap@warm, gate_cap+sur@warm   the same, but the store starts WITH the sidecar file
                a previous gated CLI call left behind (round 4): the realistic second and later
                calls. The plain gate arms start with no sidecar file (first call ever, or the
                file was deleted). The warm store is primed by a few earlier gated writes in a copy
                of the template, so it holds a few more writes (and as many evictions) than the
                template the other arms copy.
Also timed once per size: `consolidate()` dry run on the full store (offline pass).
The worker also times what the process does at exit to save the sidecar file (`exit_ms`;
0 when the policy decides not to save). Wall-clock numbers; the box is shared, expect noise.

Never touches a live store or port 8765; everything lives in temp dirs.
Run:  PYTHONPATH=src python tests/bench/gates_profile.py --sizes 200 1000 5000 [--template-cache DIR] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

NAMES = ["Ana", "Bruno", "Chen", "Dalia", "Emre", "Farah", "Gil", "Hana", "Ivo", "Jun"]
THINGS = ["oxygen", "linen", "supply", "crash", "medication", "laundry"]
WINGS = ["east", "west", "north", "south", "annex", "lobby"]
SHIFTS = ["early", "late", "night", "weekend", "holiday", "split", "float", "relief"]
VERBS = ["checked", "restocked", "counted", "audited", "cleaned", "inspected", "sealed", "logged"]
WORDS = ("amber basil cedar dune ember fjord glade harbor iris juniper kettle lagoon meadow nectar orchard "
         "pebble quartz ridge saffron thicket umber valley willow yarrow zephyr anchor beacon canyon delta "
         "estuary forge garnet hollow inlet jetty knoll lantern marsh nimbus oasis prairie quarry reef summit "
         "tundra upland vista wharf xenon yonder zenith").split()

ARMS = {
    "nogate": {},
    "shipped": {"MEMORYSAFE_MAX_ACTIVE": "{n}"},
    "gate_cap": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "{n}"},
    "gate_sur": {"MEMORYSAFE_GATE_SURPRISE": "1"},
    "gate_cap+sur": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_SURPRISE": "1",
                     "MEMORYSAFE_GATE_MAX_ACTIVE": "{n}"},
}
# Round 6 arms (default-off additions on top of gate_cap)
ARMS["gate_cap+useful"] = {**ARMS["gate_cap"], "MEMORYSAFE_GATE_USEFUL": "1"}
ARMS["gate_cap+useful+ext"] = {**ARMS["gate_cap+useful"], "MEMORYSAFE_GATE_EXTINCTION": "1"}
ARMS["gate_cap+sur+useful+ext"] = {**ARMS["gate_cap+useful+ext"], "MEMORYSAFE_GATE_SURPRISE": "1"}
# round 7: the r6 arm runs the round-6 code (git worktree of f12f354 at R6_SRC); r7 arms run this code
ARMS["r6_useful_ext"] = ARMS["gate_cap+useful+ext"]
ARMS["r7_useful_ext"] = ARMS["gate_cap+useful+ext"]
ARMS["r7_sur_useful_ext"] = ARMS["gate_cap+sur+useful+ext"]
R6_SRC = os.environ.get("R6_SRC", "/tmp/r6tree/src")
# round 8: the confirmatory candidate, exactly the r7 exploratory arm (r7 code, word-overlap goal signal off)
ARMS["r7x_nogoal_useful_ext"] = {**ARMS["gate_cap+useful+ext"], "MEMORYSAFE_GATE_R6_W_GOAL": "0",
                                 "MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED": "99"}
# Round 10 candidate: the r9 candidate plus MEMORYSAFE_GATE_STALE (GATES_R10_PREREG.md).
ARMS["r10_stale"] = {**ARMS["r7x_nogoal_useful_ext"], "MEMORYSAFE_GATE_STALE": "1"}
# round 11 candidate: r10 + exact-equivalent speed-ups + explicit goal / "mattered" signals
ARMS["r11"] = {**ARMS["r10_stale"], "MEMORYSAFE_GATE_FAST": "1", "MEMORYSAFE_GATE_GOAL2": "1"}
# round 12 confirmatory candidate (GATES_R12_PREREG.md): r10 decision logic + r11 exact speed-ups, GOAL2 off
ARMS["r12"] = {**ARMS["r10_stale"], "MEMORYSAFE_GATE_FAST": "1"}
# round 13: r12 runs the r12 code (worktree of 6c44303 at R12_SRC); r13 = same flags on this code
ARMS["r13"] = dict(ARMS["r12"])
R12_SRC = os.environ.get("R12_SRC", "/tmp/r12tree/src")
# round 14: r13 runs the round-13 code (worktree of a5f820e at R13_SRC); r14 = same flags on this code;
# r14_off_log = every flag off on this code, store with the change-log triggers installed (their cost)
ARMS["r14"] = dict(ARMS["r12"])
ARMS["r14_off_log"] = {}
R13_SRC = os.environ.get("R13_SRC", "/tmp/r13tree/src")


def _src(arm: str) -> str:
    return (R6_SRC if arm.startswith("r6_") else R12_SRC if arm == "r12" else R13_SRC if arm == "r13"
            else str(ROOT / "src"))
ARMS["gate_cap@warm"] = ARMS["gate_cap"]
ARMS["gate_cap+sur@warm"] = ARMS["gate_cap+sur"]


def synth(rng: random.Random) -> tuple[str, str]:
    """A mixed synthetic memory: ~60% routine worded rounds (heavy word overlap, the
    quadratic worst case for rarity), ~15% numbered log lines, ~25% unique-ish notes."""
    roll = rng.random()
    if roll < 0.60:
        return (f"Routine round: {rng.choice(NAMES)} {rng.choice(VERBS)} the {rng.choice(THINGS)} cart in the "
                f"{rng.choice(WINGS)} wing on the {rng.choice(SHIFTS)} shift", "task")
    if roll < 0.75:
        return f"Routine item {rng.randrange(10**6)}: checked cart in room {rng.randrange(10**6)}", "task"
    words = rng.sample(WORDS, rng.randint(6, 9))
    return " ".join(words).capitalize(), rng.choice(["other", "preference", "fact"])


def build_template(path: Path, n: int, seed: int = 1) -> None:
    from memorysafe_chatgpt.storage import MemoryStore

    store = MemoryStore(path)
    rng = random.Random(seed)
    active = 0
    while active < n:
        text, category = synth(rng)
        if store.remember(text, category)["decision"] != "MERGE":
            active += 1
    store.checkpoint()


def worker(args: argparse.Namespace) -> None:
    from memorysafe_chatgpt.storage import MemoryStore

    store = MemoryStore(Path(os.environ["MEMORYSAFE_DB_PATH"]))
    rng = random.Random(10_000 + args.seed)
    samples, evictions, first_decision = [], 0, ""
    for _ in range(args.writes + 1):
        text, category = synth(rng)
        start = time.perf_counter()
        result = store.remember(text, category)
        samples.append(1000 * (time.perf_counter() - start))
        evictions += bool(result.get("evicted"))
        first_decision = first_decision or result["decision"]
    # What the process does at exit (gates.py registers the same call with atexit); timed here.
    from memorysafe_chatgpt import gates

    start = time.perf_counter()
    gates.save_indexes(force=args.prime)
    exit_ms = 1000 * (time.perf_counter() - start)
    print(json.dumps({"first_ms": samples[0], "steady_ms": samples[1:], "exit_ms": exit_ms, "first_decision": first_decision}))


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


def profile(sizes: list[int], runs: int, writes: int, arms: list[str], cache: Path | None = None) -> dict:
    report: dict = {"sizes": {}, "runs": runs, "writes_per_run": writes}
    with tempfile.TemporaryDirectory() as temp:
        for n in sizes:
            template = Path(temp) / f"template_{n}" / "m.sqlite3"
            if cache is not None:
                template = cache / f"template_{n}" / "m.sqlite3"
            t0 = time.perf_counter()
            if not template.exists():
                build_template(template, n)
            entry: dict = {"template_build_s": round(time.perf_counter() - t0, 1), "arms": {}}
            warm_dir = Path(temp) / f"warm_{n}"
            for arm in arms:
                steady, firsts, exits = [], [], []
                if arm.endswith("@warm") and not warm_dir.exists():
                    # a few earlier gated writes in a copy of the template leaves the sidecar file behind
                    warm_dir.mkdir(parents=True)
                    src = sqlite3.connect(template)
                    dst = sqlite3.connect(warm_dir / "m.sqlite3")
                    src.backup(dst)
                    src.close()
                    dst.close()
                    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
                    env.update({"MEMORYSAFE_DB_PATH": str(warm_dir / "m.sqlite3"),
                                "MEMORYSAFE_STATE_DIR": str(warm_dir / "state"), "PYTHONPATH": str(ROOT / "src")})
                    env.update({k: v.format(n=n) for k, v in ARMS["gate_cap"].items()})
                    subprocess.run([sys.executable, __file__, "--worker", "--prime", "--writes", "3", "--seed", "99"],
                                   env=env, capture_output=True, text=True, check=True)
                for run in range(runs):
                    work = Path(temp) / f"w_{n}_{arm}_{run}"
                    work.mkdir(parents=True)
                    db = work / "m.sqlite3"
                    if arm.endswith("@warm"):
                        shutil.copy(warm_dir / "m.sqlite3", db)
                        shutil.copy(warm_dir / "m.sqlite3.gates-index", work / "m.sqlite3.gates-index")
                    else:
                        src = sqlite3.connect(template)
                        dst = sqlite3.connect(db)
                        src.backup(dst)
                        src.close()
                        dst.close()
                    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
                    env.update({"MEMORYSAFE_DB_PATH": str(db), "MEMORYSAFE_STATE_DIR": str(work / "state"),
                                "PYTHONPATH": _src(arm)})
                    env.update({k: v.format(n=n) for k, v in ARMS[arm].items()})
                    out = subprocess.run([sys.executable, __file__, "--worker", "--writes", str(writes),
                                          "--seed", str(run)], env=env, capture_output=True, text=True, check=True)
                    data = json.loads(out.stdout.strip().splitlines()[-1])
                    steady += data["steady_ms"]
                    firsts.append(data["first_ms"])
                    exits.append(data["exit_ms"])
                    shutil.rmtree(work)
                entry["arms"][arm] = {
                    "mean_ms": round(statistics.mean(steady), 2), "p95_ms": round(p95(steady), 2),
                    "first_write_mean_ms": round(statistics.mean(firsts), 2), "samples": len(steady),
                    "first_write_runs_ms": [round(v, 1) for v in firsts], "exit_save_mean_ms": round(statistics.mean(exits), 2),
                }
            if "gate_cap" in arms or True:
                env = {**os.environ, "MEMORYSAFE_DB_PATH": str(Path(temp) / f"c_{n}" / "m.sqlite3"),
                       "MEMORYSAFE_STATE_DIR": str(Path(temp) / f"c_{n}" / "s"), "MEMORYSAFE_GATE_CONSOLIDATE": "1"}
                (Path(temp) / f"c_{n}").mkdir()
                shutil.copy(template, env["MEMORYSAFE_DB_PATH"])
                code = ("import time,json;from pathlib import Path;from memorysafe_chatgpt import gates;"
                        "from memorysafe_chatgpt.storage import MemoryStore;import os;"
                        "s=MemoryStore(Path(os.environ['MEMORYSAFE_DB_PATH']));t=time.perf_counter();"
                        f"r=gates.consolidate(s,apply=False,capacity={n // 2},propose_merges=True);"
                        "print(json.dumps({'ms':1000*(time.perf_counter()-t),'moves':len(r['move']),'proposals':len(r['merge_proposals'])}))")
                out = subprocess.run([sys.executable, "-c", code], env={**env, "PYTHONPATH": str(ROOT / "src")},
                                     capture_output=True, text=True, check=True)
                entry["consolidate_dry_run"] = json.loads(out.stdout.strip().splitlines()[-1])
            report["sizes"][str(n)] = entry
    return report


def _one_run(template: Path, work: Path, n: int, arm: str, run: int, writes: int) -> dict:
    work.mkdir(parents=True)
    db = work / "m.sqlite3"
    src = sqlite3.connect(template)
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    dst.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
    if arm == "r14_off_log":   # round 14: the flags-off cost of a database that carries the gates' triggers
        subprocess.run([sys.executable, "-c", "import sqlite3, sys; from memorysafe_chatgpt import gates; "
                        "c = sqlite3.connect(sys.argv[1]); gates.ensure_change_log(c); c.commit(); c.close()", str(db)],
                       env={**env, "PYTHONPATH": str(ROOT / "src")}, check=True)
    env.update({"MEMORYSAFE_DB_PATH": str(db), "MEMORYSAFE_STATE_DIR": str(work / "state"),
                "PYTHONPATH": _src(arm)})
    env.update({k: v.format(n=n) for k, v in ARMS[arm].items()})
    out = subprocess.run([sys.executable, __file__, "--worker", "--writes", str(writes), "--seed", str(run)],
                         env=env, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout.strip().splitlines()[-1])
    sidecars = sorted(p.name for p in work.iterdir() if p.name != "m.sqlite3" and not p.name.startswith("m.sqlite3-"))
    shutil.rmtree(work)
    return {**data, "files_left": sidecars}


def profile_interleaved(sizes: list[int], runs: int, writes: int, arms: list[str], cache: Path | None,
                        order_seed: int, run_dir: Path) -> dict:
    """Round 8: one pass, arm order randomised per round (random.Random(order_seed * 1000 + size + round)),
    the same worker seed for every arm within a round (paired), one result file per process run in
    run_dir (an interrupted pass resumes: existing files are kept, never re-measured)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"sizes": {}, "runs": runs, "writes_per_run": writes, "order_seed": order_seed, "interleaved": True}
    with tempfile.TemporaryDirectory() as temp:
        for n in sizes:
            template = (cache or Path(temp)) / f"template_{n}" / "m.sqlite3"
            if not template.exists():
                build_template(template, n)
            order_log = []
            for rnd in range(runs):
                order = list(arms)
                random.Random(order_seed * 1000 + n + rnd).shuffle(order)
                order_log.append(order)
                for arm in order:
                    path = run_dir / f"N{n}_{arm}_{rnd}.json"
                    if path.exists():
                        continue
                    data = _one_run(template, Path(temp) / f"w_{n}_{arm}_{rnd}", n, arm, rnd, writes)
                    path.write_text(json.dumps({"n": n, "arm": arm, "round": rnd, **data}))
            entry: dict = {"arms": {}, "order": order_log}
            for arm in arms:
                rs = [json.loads((run_dir / f"N{n}_{arm}_{r}.json").read_text()) for r in range(runs)]
                steady = [x for r in rs for x in r["steady_ms"]]
                firsts = [r["first_ms"] for r in rs]
                entry["arms"][arm] = {
                    "mean_ms": round(statistics.mean(steady), 2), "p95_ms": round(p95(steady), 2),
                    "first_write_mean_ms": round(statistics.mean(firsts), 2), "samples": len(steady),
                    "first_write_runs_ms": [round(v, 1) for v in firsts],
                    "run_means_ms": [round(statistics.mean(r["steady_ms"]), 3) for r in rs],
                    "exit_save_mean_ms": round(statistics.mean(r["exit_ms"] for r in rs), 2),
                    "files_left": sorted({f for r in rs for f in r["files_left"]}),
                }
            report["sizes"][str(n)] = entry
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--prime", action="store_true", help="worker: always write the sidecar file at the end")
    parser.add_argument("--writes", type=int, default=30)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--sizes", type=int, nargs="*", default=[200, 1000, 5000])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--arms", nargs="*", default=list(ARMS))
    parser.add_argument("--template-cache", type=Path,
                        help="directory to keep/reuse the synthetic template stores between runs (temp data only)")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--interleave-seed", type=int, help="round 8: interleaved randomised arm order, one pass")
    parser.add_argument("--run-dir", type=Path, help="round 8: one result file per process run (resumable)")
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    if args.interleave_seed is not None:
        report = profile_interleaved(args.sizes, args.runs, args.writes, args.arms, args.template_cache,
                                     args.interleave_seed, args.run_dir)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))
        for n, entry in report["sizes"].items():
            for arm, s in entry["arms"].items():
                print(n, arm, s["mean_ms"], statistics.median(s["first_write_runs_ms"]), s["files_left"])
        return
    report = profile(args.sizes, args.runs, args.writes, args.arms, args.template_cache)
    print(f"Write-path profile: {args.runs} fresh-process runs x {args.writes} writes per arm; wall-clock ms per write.\n")
    for n, entry in report["sizes"].items():
        print(f"N = {n} active memories (cap = N for capped arms; template built in {entry['template_build_s']}s)")
        print(f"  {'arm':18s} {'mean ms':>8s} {'p95 ms':>8s} {'1st write':>10s} {'exit save':>10s}")
        for arm, s in entry["arms"].items():
            print(f"  {arm:18s} {s['mean_ms']:>8.2f} {s['p95_ms']:>8.2f} {s['first_write_mean_ms']:>10.2f} {s['exit_save_mean_ms']:>10.2f}")
        c = entry["consolidate_dry_run"]
        print(f"  consolidate dry run (cap N/2, propose merges): {c['ms']:.0f} ms, {c['moves']} moves, {c['proposals']} proposals\n")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
