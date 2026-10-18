"""Round 11: shadow check that MEMORYSAFE_GATE_FAST changes no lifecycle verdict.

Every consider_incoming call made while a benchmark runs is answered twice, once with the fast
path pinned off and once pinned on, and the two verdicts (action, reason, evidence, confidence,
old memory id) must be identical. Prints counts only (no memory text)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1] / "src"), str(HERE)]

from memorysafe_chatgpt import lifecycle, storage  # noqa: E402

STATS = {"calls": 0, "pairs": 0, "mismatch": 0, "nonnull": 0}


def _key(v):
    if v is None:
        return None
    return (v["action"], v["reason"], v["evidence"], v["confidence"], str(v["old"]["id"]))


def shadow(new_content, existing):
    rows = list(existing)
    storage._FAST_PIN.append(False)
    try:
        slow = lifecycle.consider_incoming(new_content, rows)
    finally:
        storage._FAST_PIN.pop()
    storage._FAST_PIN.append(True)
    try:
        fast = lifecycle.consider_incoming(new_content, rows)
    finally:
        storage._FAST_PIN.pop()
    STATS["calls"] += 1
    STATS["pairs"] += len(rows)
    STATS["nonnull"] += slow is not None
    if _key(slow) != _key(fast):
        STATS["mismatch"] += 1
    return slow


def main() -> None:
    import gates_r7_run as R

    storage.consider_incoming = shadow
    bench, arm, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
    if bench == "real":
        R.run_real(arm, seed)
    elif bench == "goal":
        R.run_real(arm, seed, goals=True)
    elif bench == "stale":
        R.run_stale(arm, seed)
    else:
        R.run_hard(arm, seed, "notell")
    print(json.dumps({"bench": bench, "arm": arm, "seed": seed, **STATS}))


if __name__ == "__main__":
    main()
