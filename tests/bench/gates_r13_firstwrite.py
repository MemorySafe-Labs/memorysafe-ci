"""Round 13: cold first-write probe (reviewer-B-style). DESIGN/MEASUREMENT TOOL, synthetic temp stores only.

A store of N memories is built once with every flag off (gates_profile.build_template). Each run copies it,
then starts a FRESH python process that (worker mode) records time from process start, imports the package,
opens the store and makes ONE remember() call, then a second. Reported per run: import_ms (package import),
open_ms (MemoryStore()), first_ms (the first remember), second_ms, total_ms (process start -> first write
done, i.e. what a CLI user waits for minus interpreter start-up). Arms:
  base / base_cap   pristine 0.4.19 code (d9b7a76 worktree, BASE_SRC), default / MEMORYSAFE_MAX_ACTIVE=N
  shipped       this checkout, no MEMORYSAFE_* at all (0.4.19 default behaviour: no cap)
  shipped_cap   MEMORYSAFE_MAX_ACTIVE=N (shipped capacity, overflow by one)
  r12           the r12 flag set, cap N, code from R12_SRC (git worktree of 6c44303)
  r13           the r13 flag set, cap N, this checkout
  r12_under / r13_under   same flags but cap 2N (no eviction: the gates' fixed cost only)
Optional --cpu 0 runs every worker under `taskset -c 0` (one core, the slower-CPU simulation).
Never touches a live store or port 8765.
"""
from __future__ import annotations

import time

_T0 = time.perf_counter()

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
R12_SRC = os.environ.get("R12_SRC", "/tmp/r12tree/src")
BASE_SRC = os.environ.get("BASE_SRC", "/tmp/d9tree/src")   # pristine 0.4.19 (d9b7a76), reviewer B's baseline
FLAGS = {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1",
         "MEMORYSAFE_GATE_STALE": "1", "MEMORYSAFE_GATE_FAST": "1", "MEMORYSAFE_GATE_R6_W_GOAL": "0",
         "MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED": "99"}
ARMS = {
    "base": ({}, "base"),
    "base_cap": ({"MEMORYSAFE_MAX_ACTIVE": "{n}"}, "base"),
    "shipped": ({}, None),
    "shipped_cap": ({"MEMORYSAFE_MAX_ACTIVE": "{n}"}, None),
    "r12": ({**FLAGS, "MEMORYSAFE_GATE_MAX_ACTIVE": "{n}"}, "r12"),
    "r13": ({**FLAGS, "MEMORYSAFE_GATE_MAX_ACTIVE": "{n}"}, None),
    "r12_under": ({**FLAGS, "MEMORYSAFE_GATE_MAX_ACTIVE": "{n2}"}, "r12"),
    "r13_under": ({**FLAGS, "MEMORYSAFE_GATE_MAX_ACTIVE": "{n2}"}, None),
}


def worker(seed: int) -> None:
    t_import = time.perf_counter()
    from memorysafe_chatgpt.storage import MemoryStore
    t_open = time.perf_counter()
    store = MemoryStore(Path(os.environ["MEMORYSAFE_DB_PATH"]))
    t1 = time.perf_counter()
    rng = random.Random(seed)
    store.remember(f"Probe note {rng.randrange(10**9)}: the {rng.choice(['amber', 'cedar', 'harbor'])} "
                   f"report for wing {rng.randrange(99)} is filed", "other")
    t2 = time.perf_counter()
    store.remember(f"Probe note {rng.randrange(10**9)}: the {rng.choice(['quartz', 'reef', 'willow'])} "
                   f"list for room {rng.randrange(99)} is ready", "other")
    t3 = time.perf_counter()
    print(json.dumps({"pre_ms": 1000 * (t_import - _T0), "import_ms": 1000 * (t_open - t_import),
                      "open_ms": 1000 * (t1 - t_open), "first_ms": 1000 * (t2 - t1), "second_ms": 1000 * (t3 - t2),
                      "total_ms": 1000 * (t2 - _T0)}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000])
    ap.add_argument("--runs", type=int, default=12)
    ap.add_argument("--arms", nargs="+", default=list(ARMS))
    ap.add_argument("--cpu", default=None, help="taskset core list, e.g. 0")
    ap.add_argument("--template-cache", type=Path, default=Path("/tmp/r6cache"))
    ap.add_argument("--order-seed", type=int, default=13)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    if a.worker:
        worker(a.seed)
        return
    sys.path.insert(0, str(HERE))
    import gates_profile
    out: dict = {"cpu": a.cpu, "runs": a.runs, "sizes": {}}
    for n in a.sizes:
        template = a.template_cache / f"template_{n}" / "m.sqlite3"
        if not template.exists():
            gates_profile.build_template(template, n)
        res = {arm: [] for arm in a.arms}
        for run in range(a.runs):
            order = list(a.arms)
            random.Random(a.order_seed * 1000 + n + run).shuffle(order)
            for arm in order:
                env_add, src = ARMS[arm]
                with tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / "m.sqlite3"
                    s, d = sqlite3.connect(template), sqlite3.connect(db)
                    s.backup(d)
                    s.close()
                    d.close()
                    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
                    env.update({"MEMORYSAFE_DB_PATH": str(db), "MEMORYSAFE_STATE_DIR": str(Path(tmp) / "state"),
                                "PYTHONPATH": {"r12": R12_SRC, "base": BASE_SRC}.get(src, str(ROOT / "src"))})
                    env.update({k: v.format(n=n, n2=2 * n) for k, v in env_add.items()})
                    cmd = [sys.executable, __file__, "--worker", "--seed", str(run)]
                    if a.cpu is not None:
                        cmd = ["taskset", "-c", a.cpu] + cmd
                    p = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
                    r = json.loads(p.stdout.strip().splitlines()[-1])
                    r["files_left"] = sorted(x.name for x in Path(tmp).iterdir()
                                             if x.name != "m.sqlite3" and not x.name.startswith("m.sqlite3-") and x.name != "state")
                    res[arm].append(r)
        summ = {}
        for arm, rs in res.items():
            med = {k: statistics.median(r[k] for r in rs) for k in ("import_ms", "open_ms", "first_ms", "second_ms", "total_ms")}
            med["open_first_ms"] = statistics.median(r["open_ms"] + r["first_ms"] for r in rs)
            summ[arm] = {**{k: round(v, 2) for k, v in med.items()}, "runs": len(rs),
                         "open_first_runs_ms": [round(r["open_ms"] + r["first_ms"], 2) for r in rs],
                         "first_runs_ms": [round(r["first_ms"], 1) for r in rs],
                         "total_runs_ms": [round(r["total_ms"], 1) for r in rs],
                         "files_left": sorted({f for r in rs for f in r["files_left"]})}
        out["sizes"][str(n)] = summ
        for arm, v in summ.items():
            print(n, arm, "first", v["first_ms"], "total", v["total_ms"], "import", v["import_ms"], "open", v["open_ms"], v["files_left"])
    if a.json:
        a.json.write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
