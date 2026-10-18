"""Tuning seeds only: why are rare-critical queries missed?"""
import sys, os, tempfile, collections
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
sys.path.insert(0, "/workspace/gates_proto/repo/tests/bench"); sys.path.insert(0, "/workspace/gates_proto/repo/src")
import gates_realistic_benchmark as B
from memorysafe_chatgpt import gates, storage
from memorysafe_chatgpt.storage import MemoryStore
ARMENV = {"shipped": {"MEMORYSAFE_MAX_ACTIVE": "200"},
          "useful+ext": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "200", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1"},
          "gate_cap": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "200"}}
def diag(seed, arm):
    work = B.build_workload(seed); facts, events = work["facts"], work["events"]
    clock = B.Clock(); out = collections.Counter(); ex = []
    with tempfile.TemporaryDirectory() as t:
        base = {"MEMORYSAFE_DB_PATH": f"{t}/d/m.sqlite3", "MEMORYSAFE_STATE_DIR": f"{t}/s"}
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("MEMORYSAFE_")}
        gates._INDEXES.clear()
        with patch.dict(os.environ, {**base, **ARMENV[arm]}), patch.object(storage, "_now", clock):
            store = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"]))
            mem = {}; cat = {}
            for day, evs in enumerate(events):
                clock.now = B.START + timedelta(days=day)
                for e in evs:
                    clock.now += timedelta(minutes=7, seconds=3)
                    if e[0] == "write":
                        r = store.remember(e[1], e[2])
                        if e[3] == "critical": mem[e[4]] = r["memory_id"]; cat[e[4]] = (e[2], r.get("protected"), e[1])
                    else:
                        _, q, fid = e
                        res = store.find(q, 3)
                        if facts[fid]["kind"] != "critical": continue
                        mk = facts[fid]["versions"][0]
                        hit = any(any(m in r["content"].lower() for m in mk) for r in res)
                        with store._session() as c:
                            st = c.execute("select state, protected, importance, recall_count from memories where id=?", (mem[fid],)).fetchone()
                            src = c.execute("select source from decision_events where memory_id=? and decision in ('EVICT','MOVE_OUT','EVICTED') order by rowid desc limit 1", (mem[fid],)).fetchone()
                            if src is None:
                                src = c.execute("select decision||':'||coalesce(source,'') from decision_events where memory_id=? order by rowid desc limit 1", (mem[fid],)).fetchone()
                        k = "HIT" if hit else ("MISS active(not top3)" if st[0] == "active" else f"MISS state={st[0]}")
                        lab = f"{k} | cat={cat[fid][0]} prot={st[1]}"
                        out[k] += 1; out["  " + lab] += 1
                        if not hit and len(ex) < 4: ex.append((cat[fid][0], st[0], st[2], src[0] if src else None, cat[fid][2][:70], q))
        os.environ.update(saved)
    return out, ex
if __name__ == "__main__":
    for arm in sys.argv[1:]:
        T = collections.Counter(); E = []
        for s in range(8):
            o, ex = diag(s, arm); T.update(o); E += [(s,) + x for x in ex]
        print("==", arm); [print(v, k) for k, v in sorted(T.items())]
        for x in E[:12]: print("  EX", x)
