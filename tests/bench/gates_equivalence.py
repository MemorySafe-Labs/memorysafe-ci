"""Do two source trees make the SAME gate decisions? DESIGN CHECK, temp stores only.

Runs a scripted sequence of one-operation processes (like a series of CLI calls) against a copy
of a synthetic template store (see gates_profile.py) with a given source tree on PYTHONPATH,
and prints one JSON trace: for every operation its decision, the memories the gate moved out
(by rowid, with the full reason text -- which contains the keep score), and at the end every
row's state. Run it once per tree and compare the two traces with `--compare`.

  python tests/bench/gates_equivalence.py --src /path/to/tree/src --template DB --out a.json
  python tests/bench/gates_equivalence.py --compare a.json b.json

The sequence mixes gated writes with a forget, a restore, recalls and, every few steps, a
write with the gates' flags off (so a cache left by an earlier process goes stale). Never
touches a live store or port 8765.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ID = re.compile(r"MS-\d{8}-[0-9A-F]{8}")
# Round 4: a surprise-gate merge also keeps the write as a hidden 'merge_copy' row (a MERGE_COPY event,
# and a sentence in the merge reason). Those rows are left out and the sentence is stripped, and rows
# are numbered by their rank among the remaining rows, so a round-3 tree and a later one compare equal
# exactly when they made the same decisions about the same memories.
COPY_SENTENCE = re.compile(r" The write is kept as hidden copy MS-[0-9A-F-]+; `restore MS-[0-9A-F-]+` undoes the merge\.")


def worker(step: int, db: str, mode: str, size: int) -> None:
    from gates_profile import synth
    from memorysafe_chatgpt.storage import MemoryStore

    store = MemoryStore(Path(db))
    rng = random.Random(7_000 + step)
    out: dict = {"step": step}
    with sqlite3.connect(db) as probe:
        copies = {m for (m,) in probe.execute("SELECT memory_id FROM decision_events WHERE decision = 'MERGE_COPY'")}
        rowid = {}
        for rank, (i, _r) in enumerate(r for r in probe.execute("SELECT id, rowid FROM memories ORDER BY rowid") if r[0] not in copies):
            rowid[i] = rank

        def mapped(text: str) -> str:
            return ID.sub(lambda m: f"#{rowid.get(m.group(0), '?')}", COPY_SENTENCE.sub("", text))

        kind = step % 8
        if kind == 5:  # forget the lowest-rowid active, unprotected memory
            row = probe.execute("SELECT id, rowid FROM memories WHERE state='active' AND protected=0 ORDER BY rowid LIMIT 1").fetchone()
            res = store.forget(row[0], confirm=True)
            out.update(op="forget", rowid=rowid.get(row[0]), forgotten=res.get("forgotten"))
        elif kind == 6:  # restore the newest evicted memory
            row = next((r for r in probe.execute("SELECT id, rowid FROM memories WHERE state='evicted' ORDER BY rowid DESC")
                        if r[0] not in copies), None)  # (a hidden merge copy is not an eviction)
            if row:
                res = store.restore(row[0], confirm=True)
                out.update(op="restore", rowid=rowid.get(row[0]), restored=res.get("restored"))
            else:
                out.update(op="restore", rowid=None)
        elif kind == 7:  # recalls
            hits = store.find(synth(rng)[0], 5)
            out.update(op="find", hits=[rowid.get(h.get("memory_id") or h.get("id"), "?") for h in hits])
        else:
            text, category = synth(rng)
            if kind == 4 and mode != "plain":  # a write with every gate flag off
                saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("MEMORYSAFE_GATE_")}
                res = store.remember(text, category)
                os.environ.update(saved)
                out["gates"] = "off"
            else:
                res = store.remember(text, category)
            out.update(op="write", text=text, decision=res["decision"], reason=mapped(res.get("reason", "")),
                       evicted=[rowid.get(i, "?") for i in (res.get("evicted") or [])]
                       if isinstance(res.get("evicted"), list) else res.get("evicted"))
    with sqlite3.connect(db) as probe:
        out["events"] = [
            [e[0], e[1], ID.sub(lambda m: "#", COPY_SENTENCE.sub("", e[2] or ""))]
            for e in probe.execute(
                "SELECT decision, source, reason FROM decision_events WHERE decision != 'MERGE_COPY' ORDER BY rowid DESC LIMIT 6")
        ]
        out["evicted_rowids"] = [rowid[i] for (i,) in probe.execute("SELECT id FROM memories WHERE state='evicted' ORDER BY rowid")
                                 if i in rowid]
    print(json.dumps(out, sort_keys=True))


def run(src: str, template: Path, steps: int, flags: dict[str, str], size: int, cache_off: bool) -> dict:
    trace = []
    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "m.sqlite3"
        s, d = sqlite3.connect(template), sqlite3.connect(db)
        s.backup(d)
        s.close()
        d.close()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
        env.update({"MEMORYSAFE_DB_PATH": str(db), "MEMORYSAFE_STATE_DIR": str(Path(temp) / "st"), "PYTHONPATH": src})
        env.update({k: v.format(n=size) for k, v in flags.items()})
        if cache_off:
            env["MEMORYSAFE_GATE_PERSIST"] = "0"
        for step in range(steps):
            out = subprocess.run([sys.executable, __file__, "--worker", "--step", str(step), "--db", str(db), "--size", str(size),
                                  "--mode", "mixed"], env=env, capture_output=True, text=True, check=True)
            trace.append(json.loads(out.stdout.strip().splitlines()[-1]))
        with sqlite3.connect(db) as probe:
            copies = {m for (m,) in probe.execute("SELECT memory_id FROM decision_events WHERE decision = 'MERGE_COPY'")}
            final = [[rank, *r[1:]] for rank, r in enumerate(
                r for r in probe.execute("SELECT id, state, importance, protected, recall_count FROM memories ORDER BY rowid")
                if r[0] not in copies)]
    return {"trace": trace, "final": final, "sidecar_ignored_note": "ids mapped to rowids"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--step", type=int)
    parser.add_argument("--db")
    parser.add_argument("--mode", default="mixed")
    parser.add_argument("--size", type=int, default=200)
    parser.add_argument("--src")
    parser.add_argument("--template", type=Path)
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--arm", default="gate_cap")
    parser.add_argument("--no-cache", action="store_true", help="set MEMORYSAFE_GATE_PERSIST=0 (no sidecar file)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.step, args.db, args.mode, args.size)
        return
    if args.compare:
        a, b = (json.loads(p.read_text()) for p in args.compare)
        same = a["trace"] == b["trace"] and a["final"] == b["final"]
        for x, y in zip(a["trace"], b["trace"]):
            if x != y:
                print("FIRST DIFFERENCE at step", x["step"], "\n ", x, "\n ", y)
                break
        print("IDENTICAL" if same else "DIFFERENT", f"({len(a['trace'])} operations, {len(a['final'])} rows)")
        sys.exit(0 if same else 1)
    from gates_profile import ARMS

    result = run(args.src, args.template, args.steps, ARMS[args.arm], args.size, args.no_cache)
    args.out.write_text(json.dumps(result, indent=1, sort_keys=True))
    print(f"wrote {args.out}: {len(result['trace'])} operations")


if __name__ == "__main__":
    main()
