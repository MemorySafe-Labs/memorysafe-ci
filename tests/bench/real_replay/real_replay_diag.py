"""Post-hoc DIAGNOSIS for the real replay (not pre-registered): re-runs one configuration with the
same deterministic replay, keeps the temporary store until inspected, and reports which original
memories the extinction gate acted on and which memories differ between arms. Ids, flags and
numbers go to stdout/JSON; memory text is written only to a private file (--private) for choosing
anonymized examples, never to the report.
"""
import argparse, json, shutil, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import real_replay as RR

p = argparse.ArgumentParser()
p.add_argument("--source", required=True); p.add_argument("--arm", required=True); p.add_argument("--mode", required=True)
p.add_argument("--cap", type=int, default=0); p.add_argument("--work", required=True); p.add_argument("--out", required=True)
a = p.parse_args(); a.rep = 99
kept = {}
orig_rmtree = shutil.rmtree
def keep(path, *x, **k):
    kept["path"] = Path(path)
shutil.rmtree = keep
res = RR.run(a)
shutil.rmtree = orig_rmtree
work = kept["path"]
c = sqlite3.connect(work / "m.sqlite3")
src = RR.load_source(Path(a.source))
by_id = {m["id"]: m for m in src["mems"]}
# reconstruct the id map from the deterministic replay: same order -> same ids; read from result
rev = {}
conn_rows = dict(c.execute("SELECT id, content FROM memories"))
for oid, m in by_id.items():
    for rid, content in conn_rows.items():
        if content == " ".join(m["content"].split()):
            rev[rid] = oid
rels = [dict(zip(("old", "new", "decision", "status"), r)) for r in c.execute(
    "SELECT old_memory_id, new_memory_id, decision, status FROM memory_relations")]
gt = {(r["old_memory_id"], r["new_memory_id"]): r["decision"] for r in src["rels"]}
gt_any = {frozenset((r["old_memory_id"], r["new_memory_id"])): r["decision"] for r in src["rels"]}
acts = []
for r in rels:
    o, n = rev.get(r["old"]), rev.get(r["new"])
    acts.append({"decision": r["decision"], "status": r["status"], "old_orig": o, "new_orig": n,
                 "gt_relation": gt_any.get(frozenset((o, n))),
                 "old_cat": by_id.get(o, {}).get("category"), "new_cat": by_id.get(n, {}).get("category"),
                 "older_is_old": (by_id[o]["created_at"] <= by_id[n]["created_at"]) if o in by_id and n in by_id else None})
events = [dict(zip(("decision", "memory", "source"), r)) for r in c.execute(
    "SELECT decision, memory_id, source FROM decision_events WHERE source LIKE 'gate:%' AND decision != 'EVICT'")]
for e in events:
    e["orig"] = rev.get(e["memory"]); del e["memory"]
out = {"config": vars(a), "relations": acts, "gate_events": events, "final_active": res["final_active_orig_ids"]}
Path(a.out).write_text(json.dumps(out, indent=1))
priv = Path(a.out).with_suffix(".private.txt")
with open(priv, "w") as fh:
    for x in acts:
        for k in ("old_orig", "new_orig"):
            if x[k]:
                fh.write(f"{x['decision']} {k} {x[k]}: {by_id[x[k]]['content'][:160]}\n")
orig_rmtree(work)
print(json.dumps({k: v for k, v in out.items() if k != "final_active"}, indent=1))
