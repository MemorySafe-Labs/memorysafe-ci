"""Round 14: reviewer B's cold first-write probe, as she ran it on the Intel Mac for round 13 -- shipped so the
external Mac endpoint of GATES_R14_PREREG.md is measured the same way every time.

METHOD (reviewer B, REVIEWER_B_GATES_R13_CHECK_2026-10-11.md): a store of N memories is filled once with every
flag OFF (pristine 0.4.19 code) and COPIED for every run; each run is a FRESH python process. Reported:
  open+first  store open + first remember(), timed in-process AFTER `memorysafe_chatgpt.storage` is
              imported (so it includes everything the gates load when a store is opened)
  proc        process start (parent's clock just before spawning) -> first write done
  next        open+first of the FOLLOWING fresh process on the same (now written) store
Median of --runs (default 11) per arm; arm order shuffled per run (fixed seed); one discarded warm-up run per
arm first (so every arm runs with its .pyc files compiled -- the pyc state is recorded either way).

ARMS (cap = MEMORYSAFE_GATE_MAX_ACTIVE for the gates, MEMORYSAFE_MAX_ACTIVE for 0.4.19):
  base        0.4.19 (BASE_SRC), no cap                 base_cap    0.4.19, MEMORYSAFE_MAX_ACTIVE=N
  r14_capN    this checkout, candidate flags, cap N     r14_cap2N   same, cap 2N (no eviction)
  r14_off_log this checkout, flags OFF, store with the gates' change-log triggers installed (their cost)
  r13_capN    (only with --r13-src) the round-13 code, cap N
ACCEPTANCE (prereg, external endpoint): median open+first of r14_capN <= 1.00 x base_cap, at N=1000 AND N=5000.

USAGE (macOS or Linux; nothing is installed, no network, temp directories only, never a live store):
  git worktree add /tmp/d9tree d9b7a76          # pristine 0.4.19, once
  python tests/bench/cold_probe_mac.py --base-src /tmp/d9tree/src --json cold_probe.json
Options: --sizes 1000 5000  --runs 11  --cpu 0 (Linux taskset)  --throttle 50 (Linux cpulimit -l)
         --cold-cache (Linux only: drop the page cache before every run; needs passwordless sudo)
         --cold-pyc (a fresh, empty PYTHONPYCACHEPREFIX per run: every module compiled from source)
"""
from __future__ import annotations

import time

_T0 = time.perf_counter()
_W0 = time.time()

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import platform  # noqa: E402
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
FLAGS = {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1",
         "MEMORYSAFE_GATE_STALE": "1", "MEMORYSAFE_GATE_FAST": "1", "MEMORYSAFE_GATE_R6_W_GOAL": "0",
         "MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED": "99"}
ARMS = {
    "base": ({}, "base", False),
    "base_cap": ({"MEMORYSAFE_MAX_ACTIVE": "{n}"}, "base", False),
    "r14_capN": ({**FLAGS, "MEMORYSAFE_GATE_MAX_ACTIVE": "{n}"}, "self", False),
    "r14_cap2N": ({**FLAGS, "MEMORYSAFE_GATE_MAX_ACTIVE": "{n2}"}, "self", False),
    "r14_off_log": ({}, "self", True),
    "r13_capN": ({**FLAGS, "MEMORYSAFE_GATE_MAX_ACTIVE": "{n}"}, "r13", False),
}
GATE_MODULES = ("gate_flags", "gates", "gates_r6", "gates_stale", "gates_goal", "gates_order")


def _note(rng: random.Random) -> tuple[str, str]:
    """A synthetic memory (same generator as gates_profile.synth when importable)."""
    try:
        sys.path.insert(0, str(HERE))
        import gates_profile  # type: ignore

        return gates_profile.synth(rng)
    except Exception:  # standalone fallback
        who = rng.choice(["Ana", "Ben", "Chen", "Dara", "Eli", "Femi", "Gus", "Hana"])
        what = rng.choice(["report", "invoice", "plan", "ticket", "draft", "list"])
        return (f"{who} sent the {what} {rng.randrange(10**6)} about {rng.choice(['budget', 'launch', 'trip'])}",
                rng.choice(["other", "task", "project", "preference"]))


def fill(path: Path, n: int, seed: int = 1) -> None:
    from memorysafe_chatgpt.storage import MemoryStore

    store = MemoryStore(path)
    rng = random.Random(seed)
    active = 0
    while active < n:
        text, category = _note(rng)
        if store.remember(text, category)["decision"] != "MERGE":
            active += 1
    store.checkpoint()


def install_log(path: Path) -> None:
    from memorysafe_chatgpt import gates

    with sqlite3.connect(path) as connection:
        gates.ensure_change_log(connection)


def _pyc_before() -> dict:
    """Was each gate module's .pyc already on disk BEFORE this process imported it (cold pyc = no)?"""
    import importlib.util

    out = {}
    for name in GATE_MODULES + ("gates_proto", "gates_r6_lazy", "storage"):
        spec = importlib.util.find_spec(f"memorysafe_chatgpt.{name}") if name != "storage" else None
        try:
            if spec is None:
                import memorysafe_chatgpt  # noqa: F401

                origin = str(Path(memorysafe_chatgpt.__file__).with_name(f"{name}.py"))
            else:
                origin = spec.origin
            if origin and os.path.exists(origin):
                out[name] = os.path.exists(importlib.util.cache_from_source(origin))
        except Exception:
            pass
    return out


def worker(seed: int) -> None:
    t_import = time.perf_counter()
    from memorysafe_chatgpt import storage
    from memorysafe_chatgpt.storage import MemoryStore
    t_imported = time.perf_counter()
    pyc_before = _pyc_before()
    timing: dict = {}
    gate_flags = sys.modules.get("memorysafe_chatgpt.gate_flags")   # absent in 0.4.19
    if gate_flags is not None and hasattr(gate_flags, "preload"):
        preload = gate_flags.preload

        def timed_preload() -> None:   # measurement only: how much of "open" is importing the gate modules
            start = time.perf_counter()
            preload()
            timing["preload_ms"] = 1000 * (time.perf_counter() - start)

        gate_flags.preload = timed_preload
    t_open = time.perf_counter()
    store = MemoryStore(Path(os.environ["MEMORYSAFE_DB_PATH"]))
    t1 = time.perf_counter()
    rng = random.Random(seed)
    store.remember(f"Probe note {rng.randrange(10**9)}: the {rng.choice(['amber', 'cedar', 'harbor'])} "
                   f"report for wing {rng.randrange(99)} is filed", "other")
    t2 = time.perf_counter()
    w2 = time.time()
    pyc = {}
    for name in GATE_MODULES:
        module = sys.modules.get(f"memorysafe_chatgpt.{name}")
        if module is not None:
            cached = getattr(module, "__cached__", None)
            pyc[name] = bool(cached and os.path.exists(cached))
    print(json.dumps({"import_ms": 1000 * (t_imported - t_import), "open_ms": 1000 * (t1 - t_open),
                      "first_ms": 1000 * (t2 - t1), "open_first_ms": 1000 * (t2 - t_open),
                      "end_wall": w2, "storage_pyc": bool(os.path.exists(getattr(storage, "__cached__", "") or "")),
                      "gate_modules_loaded": sorted(pyc), "gate_pyc_present": pyc, "pyc_before": pyc_before,
                      "preload_ms": timing.get("preload_ms", 0.0), "dont_write_bytecode": sys.dont_write_bytecode,
                      "pycache_prefix": getattr(sys, "pycache_prefix", None)}))


def _spawn(cmd: list[str], env: dict) -> tuple[dict, float]:
    start = time.time()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"worker failed: {p.stderr[-2000:]}")
    r = json.loads(p.stdout.strip().splitlines()[-1])
    return r, 1000 * (r["end_wall"] - start)


def _drop_caches() -> bool:
    if sys.platform != "linux":
        return False
    p = subprocess.run(["sudo", "-n", "sh", "-c", "sync; echo 1 > /proc/sys/vm/drop_caches"], capture_output=True)
    return p.returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--fill", type=Path)
    ap.add_argument("--install-log", type=Path)
    ap.add_argument("--n", type=int, default=0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000])
    ap.add_argument("--runs", type=int, default=11)
    ap.add_argument("--arms", nargs="+", default=None)
    ap.add_argument("--base-src", default=os.environ.get("BASE_SRC", "/tmp/d9tree/src"))
    ap.add_argument("--r13-src", default=os.environ.get("R13_SRC"))
    ap.add_argument("--src", default=str(ROOT / "src"))
    ap.add_argument("--templates", type=Path, default=Path(tempfile.gettempdir()) / "cold_probe_templates")
    ap.add_argument("--cpu", default=None, help="Linux: taskset core list, e.g. 0")
    ap.add_argument("--throttle", type=int, default=None, help="Linux: cpulimit -l percent, e.g. 50")
    ap.add_argument("--cold-cache", action="store_true")
    ap.add_argument("--cold-pyc", action="store_true")
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--order-seed", type=int, default=14)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    if a.worker:
        worker(a.seed)
        return
    if a.fill:
        fill(a.fill, a.n)
        return
    if a.install_log:
        install_log(a.install_log)
        return
    arms = a.arms or [x for x in ARMS if x != "r13_capN" or a.r13_src]
    srcs = {"base": a.base_src, "self": a.src, "r13": a.r13_src}
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("MEMORYSAFE_", "PYTHONPYCACHEPREFIX"))}
    clean.pop("PYTHONDONTWRITEBYTECODE", None)
    meta = {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(),
            "cpus": os.cpu_count(), "loadavg_start": os.getloadavg() if hasattr(os, "getloadavg") else None,
            "runs": a.runs, "cpu": a.cpu, "throttle": a.throttle, "cold_cache": a.cold_cache, "cold_pyc": a.cold_pyc,
            "srcs": srcs, "arms": arms, "started": time.strftime("%Y-%m-%d %H:%M:%S %Z")}
    out: dict = {"meta": meta, "sizes": {}}
    a.templates.mkdir(parents=True, exist_ok=True)
    for n in a.sizes:
        template = a.templates / f"base_{n}.sqlite3"
        logged = a.templates / f"base_{n}_log.sqlite3"
        if not template.exists():
            print(f"filling a {n}-memory store with every flag off (0.4.19 code) ...", flush=True)
            subprocess.run([sys.executable, __file__, "--fill", str(template), "--n", str(n)],
                           env={**clean, "PYTHONPATH": a.base_src, "MEMORYSAFE_STATE_DIR": str(a.templates / "state")},
                           check=True)
        if "r14_off_log" in arms and not logged.exists():
            shutil.copyfile(template, logged)
            subprocess.run([sys.executable, __file__, "--install-log", str(logged)],
                           env={**clean, "PYTHONPATH": a.src}, check=True)
        res: dict[str, list] = {arm: [] for arm in arms}
        warm = [] if a.no_warmup else [-1]
        for run in warm + list(range(a.runs)):
            order = list(arms)
            random.Random(a.order_seed * 1000 + n + run).shuffle(order)
            for arm in order:
                env_add, src, use_log = ARMS[arm]
                with tempfile.TemporaryDirectory() as tmp:
                    db = Path(tmp) / "m.sqlite3"
                    shutil.copyfile(logged if use_log else template, db)
                    env = dict(clean)
                    env.update({"MEMORYSAFE_DB_PATH": str(db), "MEMORYSAFE_STATE_DIR": str(Path(tmp) / "state"),
                                "PYTHONPATH": srcs[src]})
                    env.update({k: v.format(n=n, n2=2 * n) for k, v in env_add.items()})
                    if a.cold_pyc:
                        env["PYTHONPYCACHEPREFIX"] = str(Path(tmp) / "pyc")
                    cmd = [sys.executable, __file__, "--worker", "--seed", str(max(run, 0))]
                    if a.throttle:
                        cmd = ["cpulimit", "-q", "-l", str(a.throttle), "-f", "--"] + cmd
                    if a.cpu is not None:
                        cmd = ["taskset", "-c", a.cpu] + cmd
                    dropped = _drop_caches() if a.cold_cache else False
                    first, proc = _spawn(cmd, env)
                    if a.cold_pyc:
                        env["PYTHONPYCACHEPREFIX"] = str(Path(tmp) / "pyc2")
                    nxt, _p = _spawn(cmd[:-1] + [str(max(run, 0) + 1000)], env)
                    left = sorted(p.name for p in Path(tmp).iterdir()
                                  if p.name not in ("m.sqlite3", "m.sqlite3-wal", "m.sqlite3-shm", "state", "pyc", "pyc2"))
                    if run >= 0:
                        res[arm].append({**first, "proc_ms": proc, "next_ms": nxt["open_first_ms"], "cache_dropped": dropped,
                                         "files_left": left})
        summ = {}
        for arm, rs in res.items():
            med = lambda k: round(statistics.median(r[k] for r in rs), 2)  # noqa: E731
            summ[arm] = {"open_first_ms": med("open_first_ms"), "proc_ms": med("proc_ms"), "next_ms": med("next_ms"),
                         "open_ms": med("open_ms"), "first_ms": med("first_ms"), "import_ms": med("import_ms"),
                         "runs": len(rs), "open_first_runs_ms": [round(r["open_first_ms"], 2) for r in rs],
                         "gate_modules_loaded": sorted({m for r in rs for m in r["gate_modules_loaded"]}),
                         "preload_ms": med("preload_ms"),
                         "gate_pyc_missing_before_runs": sum(1 for r in rs if not all(
                             v for k, v in r["pyc_before"].items() if k in r["gate_modules_loaded"])),
                         "dont_write_bytecode_runs": sum(1 for r in rs if r["dont_write_bytecode"]),
                         "cache_dropped_runs": sum(1 for r in rs if r["cache_dropped"]),
                         "files_left": sorted({f for r in rs for f in r["files_left"]})}
        if "base_cap" in summ:
            for arm in summ:
                summ[arm]["x_base_cap"] = round(summ[arm]["open_first_ms"] / summ["base_cap"]["open_first_ms"], 3)
        if "base" in summ:
            for arm in summ:
                summ[arm]["x_base"] = round(summ[arm]["open_first_ms"] / summ["base"]["open_first_ms"], 3)
        out["sizes"][str(n)] = summ
        print(f"N={n}  (median of {a.runs}; ms)")
        print(f"  {'arm':<12} {'open+first':>10} {'proc':>8} {'next':>8} {'x base_cap':>10} {'gate import':>11} {'pyc missing':>11}")
        for arm, v in summ.items():
            print(f"  {arm:<12} {v['open_first_ms']:>10.1f} {v['proc_ms']:>8.1f} {v['next_ms']:>8.1f} "
                  f"{v.get('x_base_cap', 0):>10.2f} {v['preload_ms']:>11.1f} {v['gate_pyc_missing_before_runs']:>8}/{v['runs']}")
        if "r14_capN" in summ and "base_cap" in summ:
            ok = summ["r14_capN"]["x_base_cap"] <= 1.0
            print(f"  ACCEPTANCE r14_capN <= 1.00 x base_cap: {'PASS' if ok else 'FAIL'} ({summ['r14_capN']['x_base_cap']:.2f}x)")
    out["meta"]["loadavg_end"] = os.getloadavg() if hasattr(os, "getloadavg") else None
    if a.json:
        a.json.write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
