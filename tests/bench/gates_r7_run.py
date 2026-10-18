"""Round 7 runner: ONE (bench, setting, arm, seed[, tuning combo]) per call, one JSON file per
result, so an interrupted batch resumes by skipping files that exist. DESIGN EVIDENCE ONLY.

  python tests/bench/gates_r7_run.py --bench real --arm r7_useful_ext --seed 300 --out DIR
  python tests/bench/gates_r7_run.py --bench goal --arm shipped --seed 400 --out DIR
  python tests/bench/gates_r7_run.py --bench hard --setting notell --arm r6_useful_ext --seed 500 --out DIR
  --set NAME=VALUE  sets MEMORYSAFE_GATE_R6_<NAME> (tuning only; held-out runs use the frozen defaults)

Arms:
  shipped            every gate flag off (default-off equivalence with the 0.4.19 source is a test)
  r6_useful_ext      round-6 code exactly (git worktree of f12f354, path R6_SRC), capacity+useful+extinction
  r7_useful_ext      this code, capacity+useful+extinction
  r7_sur_useful_ext  this code, capacity+surprise+useful+extinction
The r6 arm imports the package from R6_SRC before any benchmark module, so every module of the
package (storage, gates, gates_r6) is the round-6 version; the benchmark code is this round's.
Temp dirs only; never touches a live store or port 8765.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
R6_SRC = os.environ.get("R6_SRC", "/tmp/r6tree/src")
# round 13: the r12 arm runs the r12 code exactly (git worktree of 6c44303 at R12_SRC); r13 runs this code
R12_SRC = os.environ.get("R12_SRC", "/tmp/r12tree/src")

CAP = {"MEMORYSAFE_GATE_CAPACITY": "1"}
UE = {**CAP, "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1"}
R6_ARMS = {
    "shipped": {},
    "r6_useful_ext": dict(UE),
    "r7_useful_ext": dict(UE),
    "r7_sur_useful_ext": {**UE, "MEMORYSAFE_GATE_SURPRISE": "1"},
    # EXPLORATORY, NOT PRE-REGISTERED (added after tuning, before any held-out run): r7 with the
    # goal-relevance signal off (goal memories themselves still count as goal-related). Motivated by
    # tuning seeds 0-7, where every grid point failed the goal-distractor guardrail. Reported
    # separately; it cannot PASS under GATES_R7_PREREG.md.
    "r7x_nogoal_useful_ext": {**UE, "MEMORYSAFE_GATE_R6_W_GOAL": "0", "MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED": "99"},
}
# Round 10 candidate (GATES_R10_PREREG.md): the r9 candidate plus the stale/narrowed-supersession flag.
R6_ARMS["r10_stale"] = {**R6_ARMS["r7x_nogoal_useful_ext"], "MEMORYSAFE_GATE_STALE": "1"}
# round 11 candidate: r10 + exact-equivalent speed-ups + explicit goal / "mattered" signals
R6_ARMS["r11"] = {**R6_ARMS["r10_stale"], "MEMORYSAFE_GATE_FAST": "1", "MEMORYSAFE_GATE_GOAL2": "1"}
# round 12 confirmatory candidate (GATES_R12_PREREG.md): r10 decision logic + r11 exact speed-ups, GOAL2 off
R6_ARMS["r12"] = {**R6_ARMS["r10_stale"], "MEMORYSAFE_GATE_FAST": "1"}
# round 13 candidate (GATES_R13_PREREG.md): the r12 flag set on the round-13 code (review fixes)
R6_ARMS["r13"] = dict(R6_ARMS["r12"])
# round 14 (GATES_R14_PREREG.md): r13 = the round-13 code exactly (git worktree of a5f820e at R13_SRC);
# r14 = the same flag set on this code (review fixes, streamed ranking, lazily loaded prototype code)
R6_ARMS["r14"] = dict(R6_ARMS["r12"])
R13_SRC = os.environ.get("R13_SRC", "/tmp/r13tree/src")


def _import_code(arm: str) -> str:
    src = (R6_SRC if arm == "r6_useful_ext" else R12_SRC if arm == "r12" else R13_SRC if arm == "r13"
           else str(HERE.parents[1] / "src"))
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, src)
    import memorysafe_chatgpt  # noqa: F401  (pins the package before the benchmark modules import it)
    from memorysafe_chatgpt import gates, gates_r6, storage  # noqa: F401

    got = str(Path(memorysafe_chatgpt.__file__).resolve().parent.parent)
    assert got == str(Path(src).resolve()), (got, src)
    return got


TUNE: dict[str, str] = {}


def run_real(arm: str, seed: int, capacity: int = 200, goals: bool = False) -> dict:
    import gates_realistic_benchmark as R

    env = {"MEMORYSAFE_MAX_ACTIVE": "{cap}"} if arm == "shipped" else {**R6_ARMS[arm], "MEMORYSAFE_GATE_MAX_ACTIVE": "{cap}"}
    env.update(TUNE)
    R.ARMS[arm] = env
    return R.run_one(arm, seed, 16, capacity, "realistic", goals)


def run_hard(arm: str, seed: int, setting: str) -> dict:
    import gates_hard_benchmark as H

    H.ARMS[arm] = {**R6_ARMS[arm], **TUNE}
    slots = H.build_events(seed, setting)
    with tempfile.TemporaryDirectory() as temp:
        store_arm = H.StoreArm(H.ARMS[arm], temp)
        try:
            recalls = hits = 0
            useful_recalls = useful_hits = 0
            kinds: dict[str, str] = {}
            for slot in slots:
                for event in slot:
                    if event[0] == "write":
                        _, text, category, tag, kind = event
                        store_arm.write(text, category, tag, kind)
                        if tag:
                            kinds.setdefault(tag, kind)
                    else:
                        recalls += 1
                        hit = bool(store_arm.recall(event[1], event[2]))
                        hits += hit
                        if kinds.get(event[2]) == "useful":
                            useful_recalls += 1
                            useful_hits += hit
            useful = [t for t, k in kinds.items() if k == "useful"]
            useless = [t for t, k in kinds.items() if k == "useless"]
            queries = {e[2]: e[1] for slot in slots for e in slot if e[0] == "recall"}
            facts = [(f"F{i}", v) for i, (_, v, _) in enumerate(H.FACTS)]
            live = {t: [store_arm.store.explain(i)["content"] for i in store_arm._live(t)] for t, _v in facts}
            dups_total = sum(1 for slot in slots for e in slot if e[0] == "write" and e[4] == "dup")
            return {
                "useful_retained": sum(bool(store_arm.alive(t)) for t in useful) / len(useful),
                "useful_answerable": sum(store_arm.answerable(queries[t], t) for t in useful) / len(useful),
                "recall_hit": hits / recalls,
                "useful_recall_hit": useful_hits / max(1, useful_recalls),
                "useless_dropped": sum(not store_arm.alive(t) for t in useless) / len(useless),
                "useless_retained": sum(bool(store_arm.alive(t)) for t in useless) / len(useless),
                "latest_fact_correct": sum(store_arm.latest_correct(t, v) for t, v in facts) / len(facts),
                "stale_active": sum(any(old in live[t] for old in v[:-1]) for t, v in facts) / len(facts),
                "dups_folded": store_arm.merged / dups_total,
                "wrong_merges": store_arm.wrong_merges,
                "write_ms": (sum(store_arm.ms) / len(store_arm.ms)) if store_arm.ms else None,
                "active": store_arm.active(),
            }
        finally:
            store_arm.close()


def run_stale(arm: str, seed: int) -> dict:
    import gates_stale_benchmark as SB

    env = {"MEMORYSAFE_MAX_ACTIVE": "1000"} if arm == "shipped" else {**R6_ARMS[arm], "MEMORYSAFE_GATE_MAX_ACTIVE": "1000"}
    env.update(TUNE)
    return SB.run_one(env, seed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", choices=["real", "goal", "hard", "stale"], required=True)
    parser.add_argument("--setting", default="notell")
    parser.add_argument("--arm", choices=list(R6_ARMS), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--set", action="append", default=[])
    args = parser.parse_args()
    for item in args.set:
        name, value = item.split("=", 1)
        TUNE[f"MEMORYSAFE_GATE_R6_{name}"] = value
    mid = args.setting if args.bench == "hard" else ("nocap" if args.bench == "stale" else "cap200")
    name = f"{args.bench}_{mid}_{args.arm}_{args.seed}{args.tag}.json"
    code = _import_code(args.arm)
    target = args.out / name
    if target.exists():
        print("skip", target)
        return
    args.out.mkdir(parents=True, exist_ok=True)
    start = time.time()
    if args.bench == "hard":
        result = run_hard(args.arm, args.seed, args.setting)
    elif args.bench == "stale":
        result = run_stale(args.arm, args.seed)
    else:
        result = run_real(args.arm, args.seed, goals=args.bench == "goal")
    result.update({"bench": args.bench, "setting": args.setting, "arm": args.arm, "seed": args.seed, "code": code,
                   "set": args.set, "wall_s": round(time.time() - start, 2)})
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1, sort_keys=True, default=str))
    tmp.replace(target)
    print("done", target)


if __name__ == "__main__":
    main()
