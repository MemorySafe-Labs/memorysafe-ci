"""Tuning seeds 0-7, goal variant: which distractors / serving facts are kept, and why."""
import sys, os, tempfile, collections
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
sys.path.insert(0, "/workspace/gates_proto/repo/tests/bench"); sys.path.insert(0, "/workspace/gates_proto/repo/src")
import gates_realistic_benchmark as B
from memorysafe_chatgpt import gates, gates_r6, storage
from memorysafe_chatgpt.storage import MemoryStore
KINDS = ["share1", "remark", "other", "other2"]
def run(seed, arm, extra):
    work = B.build_workload(seed, goals=True); clock = B.Clock(); out = collections.Counter()
    with tempfile.TemporaryDirectory() as t:
        base = {"MEMORYSAFE_DB_PATH": f"{t}/d/m.sqlite3", "MEMORYSAFE_STATE_DIR": f"{t}/s"}
        env = {"MEMORYSAFE_MAX_ACTIVE": "200"} if arm == "shipped" else {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "200", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1", **extra}
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("MEMORYSAFE_")}
        gates._INDEXES.clear(); gates_r6._CACHES.clear()
        try:
            with patch.dict(os.environ, {**base, **env}), patch.object(storage, "_now", clock):
                st = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"])); ids = {}
                for day, evs in enumerate(work["events"]):
                    clock.now = B.START + timedelta(days=day)
                    for e in evs:
                        clock.now += timedelta(minutes=7, seconds=3)
                        if e[0] == "write":
                            r = st.remember(e[1], e[2])
                            if e[3] in ("goal_distractor", "goal_serving"): ids[e[4]] = (r["memory_id"], e[2], e[1])
                        else: st.find(e[1], 3)
                with st._session() as c:
                    for fid, (mid, cat, text) in ids.items():
                        r = c.execute("select state, protected, recall_count from memories where id=?", (mid,)).fetchone()
                        kind = KINDS[int(fid.split("-d")[1])] if "-d" in fid else "serving"
                        f = gates_r6.features(text, storage._normalize(text))
                        why = "prot" if r[1] else ("anch" if f[1] else "") + ("rec" if r[2] else "")
                        out[(kind, r[0])] += 1
                        if r[0] == "active" and kind != "serving": out[(kind, "active-why", why or "-")] += 1
        finally:
            os.environ.update(saved)
    return out
if __name__ == "__main__":
    for arm, extra in [("shipped", {}), ("r7", {"MEMORYSAFE_GATE_R6_GOAL_DF_MAX": "0.06"}), ("r7-nogoal", {"MEMORYSAFE_GATE_R6_GOAL_DF_MAX": "0.06", "MEMORYSAFE_GATE_R6_W_GOAL": "0", "MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED": "99"})]:
        T = collections.Counter()
        for s in range(8): T.update(run(s, arm, extra))
        print("==", arm); [print("  ", v, k) for k, v in sorted(T.items(), key=str)]
