"""Tuning seeds 0-7 only: why does the surprise gate cancel extinction's stale-leak gain?"""
import sys, os, tempfile, collections, json
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
sys.path.insert(0, "/workspace/gates_proto/repo/tests/bench"); sys.path.insert(0, "/workspace/gates_proto/repo/src")
import gates_realistic_benchmark as B
from memorysafe_chatgpt import gates, storage
from memorysafe_chatgpt.storage import MemoryStore
CAP = {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "200", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1"}
ARMENV = {"ext": CAP, "sur+ext": {**CAP, "MEMORYSAFE_GATE_SURPRISE": "1"},
          "sur(noprot)+ext": {**CAP, "MEMORYSAFE_GATE_SURPRISE": "1", "MEMORYSAFE_GATE_SURPRISE_PROTECT": "0"}}
def info(c, mid):
    r = c.execute("select state, protected, importance, category, recall_count from memories where id=?", (mid,)).fetchone()
    w = c.execute("select count(*) from memory_relations where status='weakened' and old_memory_id=?", (mid,)).fetchone()[0]
    ev = [x[0] for x in c.execute("select decision from decision_events where memory_id=?", (mid,)).fetchall()]
    sb = [x[0][:60] for x in c.execute("select reason from decision_events where memory_id=? and source like '%surprise%'", (mid,)).fetchall()]
    return dict(state=r[0], prot=r[1], imp=round(r[2], 2), cat=r[3], weak=w, events=sorted(set(ev)), sur=sb[:2])
def run(seed, arm, extra=None):
    work = B.build_workload(seed); facts, events = work["facts"], work["events"]
    clock = B.Clock(); out = {}
    with tempfile.TemporaryDirectory() as t:
        base = {"MEMORYSAFE_DB_PATH": f"{t}/d/m.sqlite3", "MEMORYSAFE_STATE_DIR": f"{t}/s"}
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("MEMORYSAFE_")}
        gates._INDEXES.clear()
        try:
            with patch.dict(os.environ, {**base, **ARMENV[arm], **(extra or {})}), patch.object(storage, "_now", clock):
                store = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"]))
                vmem = collections.defaultdict(list); qn = 0
                for day, evs in enumerate(events):
                    clock.now = B.START + timedelta(days=day)
                    for e in evs:
                        clock.now += timedelta(minutes=7, seconds=3)
                        if e[0] == "write":
                            r = store.remember(e[1], e[2])
                            if e[3] == "evolving": vmem[(e[4], e[5])].append((r["memory_id"], r["decision"], r.get("lifecycle")))
                        else:
                            _, q, fid = e; fact = facts[fid]
                            res = store.find(q, 3)
                            if fact["kind"] != "evolving": continue
                            cur = max(k for k, s in enumerate(fact["starts"]) if s <= day)
                            if cur < 1: continue
                            qn += 1
                            top = res[0] if res else None
                            low = top["content"].lower() if top else ""
                            has = lambda ms: any(m in low for m in ms)
                            old = tuple(m for k, ms in enumerate(fact["versions"]) if k != cur for m in ms)
                            outcome = "correct" if has(fact["versions"][cur]) else ("leak" if has(old) else "other")
                            d = {"outcome": outcome, "fid": fid}
                            if outcome == "leak":
                                with store._session() as c:
                                    d["top"] = info(c, top.get("id") or top["memory_id"]); d["top_text"] = top["content"][:90]
                                    d["cur"] = [(x[1], x[2], info(c, x[0])) for x in vmem[(fid, cur)]]
                                    d["cur_in_top3"] = any(any(m in r["content"].lower() for m in fact["versions"][cur]) for r in res)
                            out[(fid, day, qn)] = d
        finally:
            os.environ.update(saved)
    return out
if __name__ == "__main__":
    tally = collections.Counter(); ex = []
    for s in range(8):
        a = run(s, "ext"); b = run(s, "sur+ext")
        for k in a:
            oa, ob = a[k]["outcome"], b.get(k, {}).get("outcome")
            tally[f"ext={oa} sur={ob}"] += 1
            if oa == "correct" and ob == "leak":
                d = b[k]
                key = (f"top:prot={d['top']['prot']} weak={d['top']['weak']} sur={'yes' if d['top']['sur'] else 'no'} "
                       f"| cur:{[(x[0], x[1], x[2]['state'], x[2]['prot'], x[2]['weak']) for x in d['cur']]} cur_in_top3={d['cur_in_top3']}")
                tally["  FLIP " + key] += 1
                if len(ex) < 10: ex.append((s, k[0], d))
    for k, v in sorted(tally.items()): print(v, k)
    for x in ex: print(json.dumps(x, default=str)[:900])
