"""Round 6 runner: ONE (bench, setting, arm, seed[, tuning combo]) per call, one JSON file per
result, so an interrupted batch resumes by skipping files that exist. DESIGN EVIDENCE ONLY.

  python tests/bench/gates_r6_run.py --bench real --arm gate_cap+useful --seed 100 --out DIR
  python tests/bench/gates_r6_run.py --bench hard --setting notell --arm shipped --seed 200 --out DIR
  --set NAME=VALUE  sets MEMORYSAFE_GATE_R6_<NAME> (tuning only; held-out runs use the frozen defaults)
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
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

CAP = {"MEMORYSAFE_GATE_CAPACITY": "1"}
R6_ARMS = {
    "shipped": {},
    "gate_cap": dict(CAP),
    "gate_cap+useful": {**CAP, "MEMORYSAFE_GATE_USEFUL": "1"},
    "gate_cap+useful+ext": {**CAP, "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1"},
    "gate_cap+sur+useful+ext": {**CAP, "MEMORYSAFE_GATE_SURPRISE": "1", "MEMORYSAFE_GATE_USEFUL": "1",
                                "MEMORYSAFE_GATE_EXTINCTION": "1"},
}


TUNE: dict[str, str] = {}  # MEMORYSAFE_GATE_R6_* overrides from --set; the benches clear MEMORYSAFE_GATE_* first,
                           # so they must travel inside the arm's own environment


def run_real(arm: str, seed: int, capacity: int = 200) -> dict:
    import gates_realistic_benchmark as R

    env = {"MEMORYSAFE_MAX_ACTIVE": "{cap}"} if arm == "shipped" else {**R6_ARMS[arm], "MEMORYSAFE_GATE_MAX_ACTIVE": "{cap}"}
    env.update(TUNE)
    R.ARMS[arm] = env
    return R.run_one(arm, seed, 16, capacity, "realistic")


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench", choices=["real", "hard"], required=True)
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
    name = f"{args.bench}_{args.setting if args.bench == 'hard' else 'cap200'}_{args.arm}_{args.seed}{args.tag}.json"
    target = args.out / name
    if target.exists():
        print("skip", target)
        return
    args.out.mkdir(parents=True, exist_ok=True)
    start = time.time()
    result = run_real(args.arm, args.seed) if args.bench == "real" else run_hard(args.arm, args.seed, args.setting)
    result.update({"bench": args.bench, "setting": args.setting, "arm": args.arm, "seed": args.seed,
                   "set": args.set, "wall_s": round(time.time() - start, 2)})
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1, sort_keys=True, default=str))
    tmp.replace(target)
    print("done", target)


if __name__ == "__main__":
    main()
