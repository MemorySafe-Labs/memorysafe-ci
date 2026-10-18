"""CLI-like chain: K fresh processes in a row, each doing ONE write on the SAME store, so every
process starts from whatever the previous one left behind (including the sidecar file). DESIGN
EVIDENCE ONLY, temp stores. Reports, per arm and size, the first write (ms) and the exit-time
sidecar save (ms) of process 1 (nothing left behind) and the mean of processes 2..K.

  PYTHONPATH=src python tests/bench/gates_chain.py --sizes 200 1000 5000 10000 --template-cache /tmp/gates_cache [--chain 12]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from gates_profile import ARMS, build_template  # noqa: E402


def chain(n: int, arm: str, length: int, template: Path, temp: Path, trial: int) -> list[dict]:
    work = temp / f"chain_{n}_{arm}_{trial}"
    work.mkdir(parents=True)
    db = work / "m.sqlite3"
    src, dst = sqlite3.connect(template), sqlite3.connect(db)
    src.backup(dst)
    src.close()
    dst.close()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
    env.update({"MEMORYSAFE_DB_PATH": str(db), "MEMORYSAFE_STATE_DIR": str(work / "state"), "PYTHONPATH": str(ROOT / "src")})
    env.update({k: v.format(n=n) for k, v in ARMS[arm].items()})
    out = []
    for step in range(length):
        res = subprocess.run([sys.executable, str(HERE / "gates_profile.py"), "--worker", "--writes", "0", "--seed", str(1000 * trial + step)],
                             env=env, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout.strip().splitlines()[-1])
        out.append({"first_ms": data["first_ms"], "exit_ms": data["exit_ms"], "decision": data["first_decision"]})
    shutil.rmtree(work)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="*", default=[200, 1000, 5000])
    parser.add_argument("--arms", nargs="*", default=["shipped", "gate_cap"])
    parser.add_argument("--chain", type=int, default=12)
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--template-cache", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    report: dict = {}
    with tempfile.TemporaryDirectory() as temp:
        for n in args.sizes:
            template = (args.template_cache or Path(temp)) / f"template_{n}" / "m.sqlite3"
            if not template.exists():
                build_template(template, n)
            print(f"N = {n}: {args.trials} chains x {args.chain} one-write processes")
            print(f"  {'arm':10s} {'1st process: write':>20s} {'exit save':>10s}   {'processes 2..K: write':>22s} {'exit save':>10s}")
            for arm in args.arms:
                runs = [chain(n, arm, args.chain, template, Path(temp), t) for t in range(args.trials)]
                # a write the shipped near-duplicate check merges never reaches the capacity path: leave those out
                first = [r[0] for r in runs if r[0]["decision"] != "MERGE"] or [runs[0][0]]
                later = [x for r in runs for x in r[1:] if x["decision"] != "MERGE"]
                report.setdefault(str(n), {})[arm] = {
                    "first_process_write_ms": round(statistics.mean(x["first_ms"] for x in first), 2),
                    "first_process_exit_ms": round(statistics.mean(x["exit_ms"] for x in first), 2),
                    "later_write_ms": round(statistics.mean(x["first_ms"] for x in later), 2),
                    "later_exit_ms": round(statistics.mean(x["exit_ms"] for x in later), 2),
                    "later_samples": len(later),
                }
                s = report[str(n)][arm]
                print(f"  {arm:10s} {s['first_process_write_ms']:>20.1f} {s['first_process_exit_ms']:>10.1f}   "
                      f"{s['later_write_ms']:>22.1f} {s['later_exit_ms']:>10.1f}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
