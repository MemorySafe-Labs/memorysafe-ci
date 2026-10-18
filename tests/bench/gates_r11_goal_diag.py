"""Round 11 diagnosis: why do arms with no goal signal keep ~61% of goal-serving facts?

For every goal-serving and goal-distractor write in the goal bench, record whether it is still
active at the end and which usefulness features its text has (anchored, hazard, slot, category
critical). Prints aggregates only (synthetic texts; no real memory text involved)."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1] / "src"), str(HERE)]

import gates_r7_run as R7  # noqa: E402
import gates_realistic_benchmark as RB  # noqa: E402
from memorysafe_chatgpt import gates, gates_r6, storage  # noqa: E402

OUT: list = []


def _wrap(orig):
    def play(store, arm, work, facts, events, clock, capacity, fifo):
        res = orig(store, arm, work, facts, events, clock, capacity, fifo)
        with store._session() as c:
            active = [str(r[0]).lower() for r in c.execute("SELECT content FROM memories WHERE state='active'")]
        for day in events:
            for e in day:
                if e[0] != "write" or e[3] not in ("goal_serving", "goal_distractor"):
                    continue
                text, cat, cls, fid = e[1], e[2], e[3], e[4]
                if cls == "goal_serving":
                    marker = facts[fid]["versions"][0][0]
                    alive = any(marker in a for a in active)
                else:
                    alive = " ".join(text.split()).lower() in active
                slots, anchored, _g, _t, hazard, _w = gates_r6.features(text)
                OUT.append({"cls": cls, "alive": alive, "anchored": anchored, "hazard": hazard,
                            "slot": bool(slots), "critical": cat in gates.CRITICAL_CATEGORIES,
                            "auto_protect_cat": cat in ("preference", "personal"), "cat": cat})
        return res
    return play


def main() -> None:
    arm = sys.argv[1]
    seeds = [int(s) for s in sys.argv[2].split(",")]
    RB.play = _wrap(RB.play)
    for s in seeds:
        R7.run_real(arm, s, goals=True)
    agg = {}
    for cls in ("goal_serving", "goal_distractor"):
        rows = [r for r in OUT if r["cls"] == cls]
        groups = Counter()
        alive = Counter()
        for r in rows:
            ev = "anchored" if r["anchored"] else ("hazard" if r["hazard"] else ("slot" if r["slot"] else "none"))
            key = f"{ev}|{'crit' if r['critical'] else ('prefpers' if r['auto_protect_cat'] else 'plain')}"
            groups[key] += 1
            alive[key] += r["alive"]
        agg[cls] = {"n": len(rows), "survival": round(sum(r["alive"] for r in rows) / max(1, len(rows)), 3),
                    "by_group": {k: [groups[k], round(alive[k] / groups[k], 3)] for k in sorted(groups)}}
    print(json.dumps({"arm": arm, "seeds": seeds, **agg}, indent=1))


if __name__ == "__main__":
    main()
