"""Lazy rank_useful == full reference at every over-capacity write (tuning seeds)."""
import sys, os, tempfile
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
sys.path.insert(0, "/workspace/gates_proto/repo/tests/bench"); sys.path.insert(0, "/workspace/gates_proto/repo/src")
import gates_realistic_benchmark as B
from memorysafe_chatgpt import gates, gates_r6, storage
from memorysafe_chatgpt.storage import MemoryStore
orig = gates_r6.rank_useful
stats = {"checks": 0, "diff": 0}
def checked(store, connection, count, cap, keep_id=None, index_rows=None):
    k = max(count, 6)
    lazy = [i["id"] for i in orig(store, connection, k, cap, keep_id)]
    full = gates_r6.rank_useful_full(store, connection, k, cap, keep_id)
    stats["checks"] += 1
    if lazy != full:
        stats["diff"] += 1
        if stats["diff"] < 4: print("DIFF", lazy, full)
    return orig(store, connection, count, cap, keep_id, index_rows)
gates_r6.rank_useful = checked
for seed, goals, ext in [(0, False, "1"), (1, True, "1"), (2, True, "0")]:
    work = B.build_workload(seed, goals=goals); clock = B.Clock()
    with tempfile.TemporaryDirectory() as t:
        env = {"MEMORYSAFE_DB_PATH": f"{t}/d/m.sqlite3", "MEMORYSAFE_STATE_DIR": f"{t}/s", "MEMORYSAFE_GATE_CAPACITY": "1",
               "MEMORYSAFE_GATE_MAX_ACTIVE": "120", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": ext}
        gates._INDEXES.clear(); gates_r6._CACHES.clear()
        with patch.dict(os.environ, env), patch.object(storage, "_now", clock):
            st = MemoryStore(Path(env["MEMORYSAFE_DB_PATH"]))
            for day, evs in enumerate(work["events"]):
                clock.now = B.START + timedelta(days=day)
                for e in evs:
                    clock.now += timedelta(minutes=7, seconds=3)
                    if e[0] == "write": st.remember(e[1], e[2])
                    else: st.find(e[1], 3)
    print(seed, goals, stats)
