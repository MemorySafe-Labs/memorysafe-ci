"""Exploratory n=1 replay of a real MemorySafe store (pre-registered in REAL_REPLAY_PREREG.md).

Reads a COPY of a store (opened read-only, immutable), replays its writes in created_at order
(plus the user's recorded FORGET actions at their times) into a fresh temporary store under one
arm, measures ground-truth metrics derived from the store's own records, deletes the temporary
store, and writes ONE JSON file per run that contains ids, counts and numbers only (no memory text).

  python real_replay.py --source COPY.sqlite3 --arm shipped|candidate|r10|r10fast|r11|r12 --mode lived|noprotect
                        --cap N|0 --rep K --out DIR [--work DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime as _DT, timezone
from pathlib import Path

HERE = Path(__file__).resolve()
# round 13: REPLAY_SRC selects the code (the r12 arm runs the r12 worktree); default this checkout
sys.path.insert(0, os.environ.get("REPLAY_SRC") or str(HERE.parents[3] / "src"))

CANDIDATE = {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_USEFUL": "1", "MEMORYSAFE_GATE_EXTINCTION": "1",
             "MEMORYSAFE_GATE_R6_W_GOAL": "0", "MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED": "99"}
R10 = {**CANDIDATE, "MEMORYSAFE_GATE_STALE": "1"}   # round 10 candidate (GATES_R10_PREREG.md)
R10FAST = {**R10, "MEMORYSAFE_GATE_FAST": "1"}       # round 11 equivalence check: must equal r10 exactly
R11 = {**R10FAST, "MEMORYSAFE_GATE_GOAL2": "1"}      # round 11 candidate (GATES_R11_PREREG.md)
R12 = dict(R10FAST)                                  # round 12 candidate (GATES_R12_PREREG.md): same flags as r10fast
R13 = dict(R12)                                      # round 13 candidate (GATES_R13_PREREG.md): r12 flags, r13 code
R14 = dict(R12)                                      # round 14 candidate (GATES_R14_PREREG.md): r12 flags, r14 code
ARM_ENV = {"candidate": CANDIDATE, "r10": R10, "r10fast": R10FAST, "r11": R11, "r12": R12, "r13": R13, "r14": R14}
NEG = re.compile(r"\b(not|no|never|none|nobody|nothing|without|cannot|can't|don't|doesn't|didn't|won't|isn't|aren't|wasn't|n't)\b", re.I)
NUM = re.compile(r"\d+(?:[.,:]\d+)*")
HAZ = re.compile(r"\b(allerg\w*|danger\w*|safety|unsafe|emergency|never|must not|do not|password|secret|medical|medication)\b", re.I)


class Clock:
    now = _DT(2026, 1, 1, tzinfo=timezone.utc)


class FakeDatetime(_DT):
    @classmethod
    def now(cls, tz=None):
        t = Clock.now
        return t if tz else t.replace(tzinfo=None)


def parse(ts: str) -> _DT:
    return _DT.fromisoformat(ts.replace("Z", "+00:00"))


def load_source(path: Path) -> dict:
    c = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    mems = [dict(r) for r in c.execute("SELECT rowid AS rid, * FROM memories")]
    events = [dict(r) for r in c.execute("SELECT * FROM decision_events")]
    rels = [dict(r) for r in c.execute("SELECT * FROM memory_relations")]
    c.close()
    return {"mems": mems, "events": events, "rels": rels}


def timeline(src: dict, user_resolutions: bool = False) -> list[tuple]:
    out = [(parse(m["created_at"]), 0, m["rid"], "write", m["id"]) for m in src["mems"]]
    for e in src["events"]:
        if e["decision"] == "FORGET" and e["memory_id"]:
            out.append((parse(e["created_at"]), 1, e["id"], "forget", e["memory_id"]))
    if user_resolutions:   # round 10 option: her KEEP_BOTH decisions, replayed at the time she made them
        for r in src["rels"]:
            if r["decision"] == "KEEP_BOTH" and r["resolved_at"]:
                out.append((parse(r["resolved_at"]), 2, r["id"], "keep_both", (r["old_memory_id"], r["new_memory_id"])))
    return sorted(out, key=lambda x: x[:3])


def run(args) -> dict:
    os.environ.update({"HOME": os.environ.get("HOME", "/tmp")})
    for k in list(os.environ):
        if k.startswith("MEMORYSAFE_"):
            del os.environ[k]
    work = Path(tempfile.mkdtemp(prefix="rr_", dir=args.work))
    os.environ["MEMORYSAFE_DB_PATH"] = str(work / "m.sqlite3")
    os.environ["MEMORYSAFE_STATE_DIR"] = str(work / "state")
    os.environ["MEMORYSAFE_GATE_PERSIST"] = "0"   # no sidecar file with text (cache only; decisions unchanged)
    if args.arm in ARM_ENV:
        os.environ.update(ARM_ENV[args.arm])
        if args.cap:
            os.environ["MEMORYSAFE_GATE_MAX_ACTIVE"] = str(args.cap)
    elif args.cap:
        os.environ["MEMORYSAFE_MAX_ACTIVE"] = str(args.cap)

    # deterministic ids and a replay clock that follows the original timestamps
    rng = random.Random(12345)
    uuid.uuid4 = lambda: uuid.UUID(int=rng.getrandbits(128), version=4)
    from memorysafe_chatgpt import storage, gates, gates_r6  # noqa: F401
    for mod in (storage, gates):
        mod.datetime = FakeDatetime
    if args.mode == "noprotect":   # counterfactual stress mode: the automatic protection rule never fires
        storage.MemoryStore._protection_rule = staticmethod(lambda *a, **k: False)
    store = storage.MemoryStore(work / "m.sqlite3")

    src = load_source(Path(args.source))
    by_id = {m["id"]: m for m in src["mems"]}
    idmap: dict[str, str] = {}
    merges, decisions, lat = [], {}, []
    for t, _k, _r, kind, oid in timeline(src, args.user_resolutions):
        Clock.now = t
        if kind == "write":
            m = by_id[oid]
            t0 = time.perf_counter()
            try:
                res = store.remember(m["content"], m["category"], importance=m["importance"],
                                     confidence=m["confidence"], source=m["source"])
            except ValueError:
                res = store.remember(m["content"], "other", importance=m["importance"],
                                     confidence=m["confidence"], source=m["source"])
                decisions["category_fallback"] = decisions.get("category_fallback", 0) + 1
            lat.append(1000 * (time.perf_counter() - t0))
            decisions[res["decision"]] = decisions.get(res["decision"], 0) + 1
            idmap[oid] = res["memory_id"]
            if res["decision"] == "MERGE":
                into = next((o for o, r in idmap.items() if r == res["memory_id"] and o != oid), None)
                merges.append({"orig": oid, "into_orig": into, "replay_id": res["memory_id"]})
        elif kind == "keep_both":
            a, b = idmap.get(oid[0]), idmap.get(oid[1])
            if not a or not b or a == b:
                decisions["keep_both_skipped"] = decisions.get("keep_both_skipped", 0) + 1
                continue
            with store._session() as cx:
                rel = cx.execute("SELECT id FROM memory_relations WHERE ((old_memory_id=? AND new_memory_id=?) OR "
                                 "(old_memory_id=? AND new_memory_id=?)) AND decision NOT IN ('STALE_FLAG') "
                                 "AND source NOT LIKE 'gate:%' ORDER BY id DESC LIMIT 1", (a, b, b, a)).fetchone()
            if rel is not None:
                store.resolve_conflict(int(rel[0]), "keep_both", confirm=True)
                decisions["keep_both_resolved"] = decisions.get("keep_both_resolved", 0) + 1
            else:   # no conflict in the replay store: record her decision the way resolve_conflict would
                from memorysafe_chatgpt.lifecycle import record_relation
                with store._session() as cx:
                    stamp = storage._now()
                    record_relation(cx, old_memory_id=a, new_memory_id=b, decision="KEEP_BOTH", status="resolved",
                                    reason="replayed user decision", evidence="replay", confidence=1.0,
                                    source="replay:user", created_at=stamp)
                    if gates_r6.extinction_enabled():
                        gates_r6.after_resolution(store, cx, "keep_both", a, b, stamp)
                decisions["keep_both_recorded"] = decisions.get("keep_both_recorded", 0) + 1
        else:
            rid = idmap.get(oid)
            owners = [o for o, r in idmap.items() if r == rid]
            if rid and owners == [oid]:
                store.forget(rid, confirm=True)
            else:
                decisions["forget_skipped_merged"] = decisions.get("forget_skipped_merged", 0) + 1

    Clock.now = max(parse(m["created_at"]) for m in src["mems"])
    conn = sqlite3.connect(work / "m.sqlite3")
    state = dict(conn.execute("SELECT id, state FROM memories"))
    event_counts = {f"{d}|{s}": n for d, s, n in conn.execute(
        "SELECT decision, source, COUNT(*) FROM decision_events GROUP BY 1, 2")}
    relation_counts = {f"{d}|{s}": n for d, s, n in conn.execute(
        "SELECT decision, status, COUNT(*) FROM memory_relations GROUP BY 1, 2")}
    # round 10 additions (ids only): every gate action and every stale flag, mapped to original ids
    back: dict[str, str] = {}
    for o, r in idmap.items():
        back.setdefault(r, o)
    standing = {r[0] for r in conn.execute(
        "SELECT r.old_memory_id FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id WHERE "
        "r.decision = 'STALE_FLAG' AND r.status = 'flagged' AND m.state = 'active' AND r.created_at >= m.updated_at")}
    gate_actions = [[back.get(o, o), back.get(n, n.replace(o, "self") if n.endswith("#age") else n), d, st]
                    for o, n, d, st in conn.execute(
                        "SELECT old_memory_id, new_memory_id, decision, status FROM memory_relations "
                        "WHERE source LIKE 'gate:%' ORDER BY id")]
    stale_standing = sorted(back.get(x, x) for x in standing)
    # round 14: standing review indications (mixed / uncertain notes get these instead of a demotion)
    review_standing = sorted(back.get(r[0], r[0]) for r in conn.execute(
        "SELECT r.old_memory_id FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id WHERE "
        "r.decision = 'STALE_REVIEW' AND r.status = 'review' AND m.state = 'active' AND r.created_at >= m.updated_at"))
    weakened = {r[0] for r in conn.execute("SELECT old_memory_id FROM memory_relations WHERE decision IN ('weakened','WEAKEN') OR status IN ('weakened','proposed')")} if True else set()
    conn.close()

    forgotten = {e["memory_id"] for e in src["events"] if e["decision"] == "FORGET"}
    live = [m for m in src["mems"] if m["id"] not in forgotten]
    stop = storage._STOPWORDS
    df: dict[str, int] = {}
    toks = {}
    for m in live:
        ws = [w for w in storage._normalize(m["content"]).split() if w not in stop and len(w) > 2]
        toks[m["id"]] = ws
        for w in set(ws):
            df[w] = df.get(w, 0) + 1

    def key_query(words: list[str], k: int = 4) -> str:
        seen, order = set(), []
        for i, w in enumerate(words):
            if w not in seen:
                seen.add(w)
                order.append((df.get(w, 0), i, w))
        return " ".join(w for _d, _i, w in sorted(order)[:k])

    def alive(oid):
        return state.get(idmap.get(oid)) == "active"

    def topk(q, k):
        return [r.get("memory_id") for r in store.find(q, k, record=False)] if q else []

    # (a) important: originally protected, importance >= 0.8, decision category, or hazard words
    A = [m["id"] for m in live if m["protected"] or m["importance"] >= 0.8 or m["category"] == "decision" or HAZ.search(m["content"])]
    # (b) useful: recorded recall_count > 0
    B = [m["id"] for m in live if (m["recall_count"] or 0) > 0]
    b_hit = []
    for oid in B:
        b_hit.append(oid if idmap.get(oid) in topk(key_query(toks[oid]), 3) else None)
    # (c) supersession pairs (SUPERSEDE relations; both sides not forgotten)
    C = []
    for r in src["rels"]:
        if r["decision"] == "SUPERSEDE" and r["old_memory_id"] not in forgotten and r["new_memory_id"] not in forgotten:
            o, n = r["old_memory_id"], r["new_memory_id"]
            shared = [w for w in toks[n] if w in set(toks[o])]
            q = key_query(shared) if len(set(shared)) >= 2 else key_query(toks[n])
            top = topk(q, 5)
            C.append({"old": o, "new": n, "top1_old": bool(top) and top[0] == idmap.get(o),
                      "top1_new": bool(top) and top[0] == idmap.get(n), "old_active": alive(o), "new_active": alive(n)})
    # keep-both pairs (user/system said both are valid): both should stay findable
    K = []
    for r in src["rels"]:
        if r["decision"] == "KEEP_BOTH" and r["old_memory_id"] not in forgotten and r["new_memory_id"] not in forgotten:
            o, n = r["old_memory_id"], r["new_memory_id"]
            K.append({"old": o, "new": n, "both_active": alive(o) and alive(n),
                      "old_found": idmap.get(o) in topk(key_query(toks[o]), 3),
                      "new_found": idmap.get(n) in topk(key_query(toks[n]), 3)})
    # (d) merges, rule-checked (texts compared in memory; only flags are written)
    for mg in merges:
        a = by_id[mg["orig"]]["content"]
        b = by_id[mg["into_orig"]]["content"] if mg["into_orig"] else ""
        wa = set(w for w in storage._normalize(a).split() if w not in stop)
        wb = set(w for w in storage._normalize(b).split() if w not in stop)
        jac = len(wa & wb) / max(1, len(wa | wb))
        mg.update({"negation_diff": bool(NEG.search(a)) != bool(NEG.search(b)),
                   "number_diff": set(NUM.findall(a)) != set(NUM.findall(b)),
                   "jaccard": round(jac, 3), "exact_normalized": storage._normalize(a) == storage._normalize(b)})
        mg["doubtful"] = (not mg["exact_normalized"]) and (mg["negation_diff"] or mg["number_diff"] or jac < 0.5)
    # (e) throwaway proxy (strict, as pre-registered) and loose variant
    E = [m["id"] for m in live if len(m["content"]) <= 80 and not m["recall_count"] and m["importance"] <= 0.7 and m["category"] == "other"]
    E2 = [m["id"] for m in live if not m["recall_count"] and m["category"] == "other"]

    final_active = sorted(o for o in idmap if alive(o))
    digest = hashlib.sha256(json.dumps([final_active, sorted(idmap.items())]).encode()).hexdigest()[:16]
    out = {
        "arm": args.arm, "user_resolutions": args.user_resolutions, "mode": args.mode, "cap": args.cap, "rep": args.rep,
        "n_written": len(src["mems"]), "n_live": len(live), "n_forgotten": len(forgotten),
        "active_at_end": sum(1 for s in state.values() if s == "active"),
        "states": {s: list(state.values()).count(s) for s in set(state.values())},
        "decisions": decisions, "event_counts": event_counts, "relation_counts": relation_counts,
        "A_n": len(A), "A_survived": sum(alive(o) for o in A), "A_lost_ids": [o for o in A if not alive(o)],
        "B_n": len(B), "B_survived": sum(alive(o) for o in B), "B_hit3": sum(1 for x in b_hit if x),
        "B_lost_ids": [o for o in B if not alive(o)], "B_miss_ids": [o for o, x in zip(B, b_hit) if not x],
        "C_n": len(C), "C_stale_leak": sum(p["top1_old"] for p in C), "C_latest_correct": sum(p["top1_new"] for p in C), "C_pairs": C,
        "K_n": len(K), "K_both_active": sum(p["both_active"] for p in K),
        "K_both_found": sum(p["old_found"] and p["new_found"] for p in K), "K_pairs": K,
        "merges": merges, "merges_doubtful": sum(m["doubtful"] for m in merges),
        "E_n": len(E), "E_retained": sum(alive(o) for o in E), "E2_n": len(E2), "E2_retained": sum(alive(o) for o in E2),
        "write_ms_mean": sum(lat) / len(lat), "write_ms_total": sum(lat),
        "final_active_orig_ids": final_active, "outcome_digest": digest,
        "gate_actions": gate_actions, "stale_standing_orig_ids": stale_standing,
        "review_standing_orig_ids": review_standing,
        "files_in_work": sorted(p.name for p in work.iterdir()),
    }
    shutil.rmtree(work)   # the temporary store holds her text; it is deleted after every run
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--arm", choices=["shipped", "candidate", "r10", "r10fast", "r11", "r12", "r13", "r14"], required=True)
    p.add_argument("--mode", choices=["lived", "noprotect"], required=True)
    p.add_argument("--cap", type=int, default=0)
    p.add_argument("--rep", type=int, default=0)
    p.add_argument("--out", required=True)
    p.add_argument("--work", default=None)
    p.add_argument("--user-resolutions", action="store_true",
                   help="also replay her KEEP_BOTH decisions at their resolved_at times (round 10)")
    args = p.parse_args()
    target = Path(args.out) / f"{args.mode}_cap{args.cap}_{args.arm}_{args.rep}{'_ur' if args.user_resolutions else ''}.json"
    if target.exists():
        print("exists", target)
        return
    res = run(args)
    target.write_text(json.dumps(res, indent=1))
    print("done", target)


if __name__ == "__main__":
    main()
