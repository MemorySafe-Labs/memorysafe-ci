"""Round 10 stale-status bench (synthetic analogues of the real-replay cases; no real memory text).

Simulated clock (storage._now patched). Per seed, one store over ~120 days receives:
  chains     status note about entity E, then (21-50 days later) newer activity about E
  timestale  status note never updated (flag is allowed; it must still be findable)
  reconf     status note restated within the last 30 days (must not be flagged)
  durable    durable facts, many carrying dates or numbers (must never be flagged/demoted for age)
  unrelated  pairs of unrelated notes sharing a date and a change word (must not supersede)
  tentative  a state note, then a plan to change it (a plan is not a replacement)
  keepboth   pairs the user resolved as KEEP_BOTH (neither may be weakened/flagged via the other)
  filler     throwaway notes, some with the same dates
Queries run at the end. Metrics are ids-and-numbers only.
"""
from __future__ import annotations

import os
import random
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gates, storage
from memorysafe_chatgpt.storage import MemoryStore

START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
END_DAY = 120
USER = "Dana"
ENTITIES = ["Quillfeather", "Brightwater", "Calderon", "Ostrava", "Pemberly", "Vantablue", "Harrowgate", "Lindqvist",
            "Marrowfield", "Northcote", "Okafor", "Rosenthal", "Saltmarsh", "Tamberlane", "Umbridge", "Westerly",
            "Yarborough", "Zellweg", "Ashcombe", "Blackthorn", "Corriveau", "Dunmore", "Elsworth", "Fairbanks"]
PEOPLE = ["Priya", "Tomas", "Ines", "Kwame", "Lotte", "Mateo", "Noor", "Oskar", "Rhea", "Sven", "Yara", "Zeno",
          "Aiko", "Bram", "Chidi", "Delia", "Emeka", "Freya", "Gideon", "Hana"]
CHAINS = [
    ("The {E} contract is still unsigned as of {d1}", "{E} countersigned the contract on {d2}", "what is the status of the {E} contract"),
    ("The {E} hardware delivery is pending as of {d1}", "{E} delivered the hardware to the annex on {d2}", "where is the {E} hardware delivery"),
    ("{E} visa application is awaiting approval as of {d1}", "{E} got the visa approved on {d2}", "what is the status of the {E} visa application"),
    ("The {E} invoice is still outstanding as of {d1}", "{E} paid the invoice in full on {d2}", "has {E} paid the invoice"),
    ("The {E} server is currently running version 2.{a}", "{E} server was upgraded to version 3.{b} on {d2}", "which version is the {E} server on"),
    ("The {E} offer is unconfirmed as of {d1}", "{E} confirmed the offer in writing on {d2}", "is the {E} offer confirmed"),
]
TIMESTALE = [("The {E} permit renewal is pending as of {d1}", "is the {E} permit renewal done"),
             ("The {E} audit report is under review as of {d1}", "where is the {E} audit report")]
DURABLE = [("{P} is allergic to {x}", "is {P} allergic to anything", ["penicillin", "peanuts", "shellfish", "latex"]),
           ("{P} birthday is {x}", "when is {P} birthday", ["14 March", "2 September", "30 June", "7 November"]),
           ("We chose {x} for the {E} ledger on {d1} because it handles audits well", "why did we choose the database for the {E} ledger", ["Postgres", "SQLite", "DuckDB"]),
           ("{P} lives at {n} {x}", "where does {P} live", ["Larch Street", "Harbour Road", "Mill Lane"]),
           ("{P} takes {x} every morning", "what medication does {P} take", ["metformin", "levothyroxine", "lisinopril"])]
UNRELATED = ("{P} sent the {doc} to {U} on {d}, not signed yet", "{Q} rescheduled the {meet} from {d} to the following week",
             "did {P} send the {doc}")
DOCS = ["mutual NDA", "data processing addendum", "statement of work", "pilot agreement"]
MEETS = ["GTM call", "board prep", "design review", "pricing sync"]
TENTATIVE = ("The {E} cluster is currently running version 4.{a}", "Plan to evaluate upgrading the {E} cluster to version 5.{b} next quarter",
             "which version is the {E} cluster running")
KEEPBOTH = ("{P} prefers email for project updates", "{P} prefers phone calls for urgent issues",
            "how does {P} want project updates", "how should we reach {P} for urgent issues")
FILLER = ["Coffee with {P} on {d} was nice", "Lunch at the food hall on {d}, the soup was fine", "Parking at the annex was full on {d}",
          "Watched a documentary about lighthouses", "Bought printer paper and staples", "The office plant needs water",
          "{P} recommended a podcast about bridges", "Train was ten minutes late on {d}"]


def _date(day: int) -> str:
    d = START + timedelta(days=day)
    return f"{d.day} {d.strftime('%B')}"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> str:
        return self.now.isoformat(timespec="seconds")


def build(seed: int) -> dict:
    rng = random.Random(seed)
    ents = rng.sample(ENTITIES, len(ENTITIES))
    people = rng.sample(PEOPLE, len(PEOPLE))
    ev: list[tuple] = []   # (day, kind, key, text)
    items: dict[str, dict] = {}
    for i in range(8):
        t_old, t_new, q = CHAINS[i % len(CHAINS)]
        E = ents.pop()
        d1 = rng.randint(3, 40)
        d2 = d1 + rng.randint(21, 50)
        a, b = rng.randint(1, 9), rng.randint(0, 9)
        ev.append((d1, "chain_old", f"c{i}", t_old.format(E=E, d1=_date(d1), a=a)))
        ev.append((d2, "chain_new", f"c{i}", t_new.format(E=E, d2=_date(d2), b=b)))
        items[f"c{i}"] = {"q": q.format(E=E)}
    for i in range(6):
        t, q = TIMESTALE[i % 2]
        E = ents.pop()
        d1 = rng.randint(3, 50)
        ev.append((d1, "timestale", f"t{i}", t.format(E=E, d1=_date(d1))))
        items[f"t{i}"] = {"q": q.format(E=E)}
    for i in range(6):
        t, q = TIMESTALE[i % 2]
        E = ents.pop()
        text = t.format(E=E, d1="this week").replace("as of this week", "as of this week")
        for d in (rng.randint(3, 30), rng.randint(50, 80), rng.randint(END_DAY - 25, END_DAY - 2)):
            ev.append((d, "reconf", f"r{i}", text))
        items[f"r{i}"] = {"q": q.format(E=E)}
    for i in range(10):
        t, q, xs = DURABLE[i % len(DURABLE)]
        P, E = people[i], ents[i % len(ents)]
        d1 = rng.randint(0, 20)
        ev.append((d1, "durable", f"d{i}", t.format(P=P, E=E, x=rng.choice(xs), n=rng.randint(2, 99), d1=_date(d1))))
        items[f"d{i}"] = {"q": q.format(P=P, E=E)}
    for i in range(8):
        P, Q = people[10 + i % 10], people[(15 + i) % 20]
        d = rng.randint(5, 90)
        doc, meet = DOCS[i % 4], MEETS[(i + 1) % 4]
        ev.append((d, "unrel_a", f"u{i}", UNRELATED[0].format(P=P, doc=doc, U=USER, d=_date(d))))
        ev.append((d + 1, "unrel_b", f"u{i}", UNRELATED[1].format(Q=Q, meet=meet, d=_date(d))))
        items[f"u{i}"] = {"q": UNRELATED[2].format(P=P, doc=doc)}
    for i in range(4):
        E = ents.pop()
        d = rng.randint(5, 60)
        a, b = rng.randint(0, 9), rng.randint(0, 9)
        ev.append((d, "tent_a", f"p{i}", TENTATIVE[0].format(E=E, a=a)))
        ev.append((d + rng.randint(5, 20), "tent_b", f"p{i}", TENTATIVE[1].format(E=E, b=b)))
        items[f"p{i}"] = {"q": TENTATIVE[2].format(E=E)}
    for i in range(4):
        P = people[i + 12]
        d = rng.randint(10, 80)
        ev.append((d, "kb_a", f"k{i}", KEEPBOTH[0].format(P=P)))
        ev.append((d, "kb_b", f"k{i}", KEEPBOTH[1].format(P=P)))
        items[f"k{i}"] = {"qa": KEEPBOTH[2].format(P=P), "qb": KEEPBOTH[3].format(P=P)}
    for i in range(60):
        d = rng.randint(0, END_DAY - 1)
        ev.append((d, "filler", f"f{i}", rng.choice(FILLER).format(P=rng.choice(people), d=_date(rng.randint(0, END_DAY)))))
    ev.sort(key=lambda e: e[0])   # stable: within a day, generation order
    return {"events": ev, "items": items}


def _rel(c, old: str, new: str | None = None) -> list[tuple]:
    sql = "SELECT decision, status, new_memory_id FROM memory_relations WHERE old_memory_id = ?"
    return [tuple(r) for r in c.execute(sql, (old,)) if new is None or r[2] == new]


def run_one(env: dict, seed: int) -> dict:
    work = build(seed)
    clock = Clock()
    with tempfile.TemporaryDirectory() as temp:
        base = {"MEMORYSAFE_DB_PATH": str(Path(temp) / "d" / "m.sqlite3"), "MEMORYSAFE_STATE_DIR": str(Path(temp) / "s")}
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("MEMORYSAFE_GATE_") or k == "MEMORYSAFE_MAX_ACTIVE"}
        gates._INDEXES.clear()
        try:
            with patch.dict(os.environ, {**base, **env}), patch.object(storage, "_now", clock):
                store = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"]))
                return play(store, work, clock)
        finally:
            os.environ.update(saved)


def play(store: MemoryStore, work: dict, clock: Clock) -> dict:
    ids: dict[tuple[str, str], str] = {}
    ms: list[float] = []
    last_day = -1
    tick = 0
    for day, kind, key, text in work["events"]:
        tick = tick + 1 if day == last_day else 0
        last_day = day
        clock.now = START + timedelta(days=day, minutes=7 * tick)
        cat = "personal" if kind == "durable" else ("other" if kind == "filler" else "project")
        t0 = time.perf_counter()
        out = store.remember(text, cat)
        ms.append((time.perf_counter() - t0) * 1000)
        mid = out.get("memory_id")
        ids.setdefault((kind, key), mid)
        if kind == "kb_b":
            a, b = ids[("kb_a", key)], mid
            with store._session() as c:
                row = c.execute("SELECT id FROM memory_relations WHERE (old_memory_id=? AND new_memory_id=?) OR "
                                "(old_memory_id=? AND new_memory_id=?) ORDER BY id DESC LIMIT 1", (a, b, b, a)).fetchone()
            if row is not None:
                store.resolve_conflict(int(row[0]), "keep_both", confirm=True)
            else:   # the user's KEEP_BOTH decision, recorded the way resolve_conflict records it
                from memorysafe_chatgpt.lifecycle import record_relation
                with store._session() as c:
                    record_relation(c, old_memory_id=a, new_memory_id=b, decision="KEEP_BOTH", status="resolved",
                                    reason="user: both true", evidence="bench", confidence=1.0, source="manual",
                                    created_at=clock())
    clock.now = START + timedelta(days=END_DAY)
    store.remember("End of bench marker note", "other")   # gives the once-a-day sweep its last chance
    items = work["items"]

    def top(q: str, k: int = 3) -> list[dict]:
        return store.find(q, k)

    def flagged(c, mid: str) -> bool:   # a standing flag: status 'flagged' and not voided by a later reconfirmation
        return c.execute("SELECT 1 FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id WHERE "
                         "r.decision = 'STALE_FLAG' AND r.status = 'flagged' AND r.old_memory_id = ? AND "
                         "r.created_at >= m.updated_at", (mid,)).fetchone() is not None

    def state(c, mid: str) -> str:
        r = c.execute("SELECT state FROM memories WHERE id = ?", (mid,)).fetchone()
        return r[0] if r else "missing"

    bad = {"WEAKEN", "PROPOSE_WEAKEN", "EXTINGUISH", "SUPERSEDE", "STALE_FLAG"}
    out: dict = {}
    with store._session() as c:
        ch = [k for k in items if k.startswith("c")]
        lc = st = 0
        for k in ch:
            r = top(items[k]["q"], 1)
            first = r[0]["memory_id"] if r else None
            lc += first == ids[("chain_new", k)]
            st += first == ids[("chain_old", k)]
        out["status_latest_correct"] = lc / len(ch)
        out["status_stale_top1"] = st / len(ch)
        out["chain_old_flagged"] = sum(flagged(c, ids[("chain_old", k)]) for k in ch) / len(ch)
        out["chain_old_extinguished"] = sum(state(c, ids[("chain_old", k)]) != "active" for k in ch) / len(ch)
        ts = [k for k in items if k.startswith("t")]
        out["timestale_flagged"] = sum(flagged(c, ids[("timestale", k)]) for k in ts) / len(ts)
        out["timestale_found_top3"] = sum(ids[("timestale", k)] in [x["memory_id"] for x in top(items[k]["q"])] for k in ts) / len(ts)
        rc = [k for k in items if k.startswith("r")]
        out["reconfirmed_flagged"] = sum(flagged(c, ids[("reconf", k)]) for k in rc) / len(rc)
        du = [k for k in items if k.startswith("d")]
        out["durable_top1"] = sum((top(items[k]["q"], 1) or [{}])[0].get("memory_id") == ids[("durable", k)] for k in du) / len(du)
        out["durable_flagged_n"] = sum(flagged(c, ids[("durable", k)]) for k in du)
        out["durable_demoted_n"] = sum(flagged(c, ids[("durable", k)]) or state(c, ids[("durable", k)]) != "active" for k in du)
        un = [k for k in items if k.startswith("u")]
        fs = 0
        for k in un:
            a, b = ids[("unrel_a", k)], ids[("unrel_b", k)]
            fs += any(d in bad and s not in ("undone", "cleared") for d, s, _n in _rel(c, a, b)) or state(c, a) != "active"
        out["false_supersession"] = fs / len(un)
        tp = [k for k in items if k.startswith("p")]
        out["tentative_supersession"] = sum(
            any(d in bad - {"STALE_FLAG"} and s not in ("undone", "cleared") for d, s, _n in _rel(c, ids[("tent_a", k)], ids[("tent_b", k)]))
            or state(c, ids[("tent_a", k)]) != "active" for k in tp) / len(tp)
        out["unrelated_top1"] = sum((top(items[k]["q"], 1) or [{}])[0].get("memory_id") == ids[("unrel_a", k)] for k in un) / len(un)
        kb = [k for k in items if k.startswith("k")]
        ok = 0
        for k in kb:
            a, b = ids[("kb_a", k)], ids[("kb_b", k)]
            via = [x for x in _rel(c, a, b) + _rel(c, b, a) if x[0] in bad and x[1] not in ("undone", "cleared")]
            alive = state(c, a) == "active" and state(c, b) == "active"
            found = a in [x["memory_id"] for x in top(items[k]["qa"])] and b in [x["memory_id"] for x in top(items[k]["qb"])]
            ok += (not via) and alive and found
        out["keep_both_respected"] = ok / len(kb)
        out["stale_flags_total"] = c.execute("SELECT COUNT(*) FROM memory_relations WHERE decision='STALE_FLAG'").fetchone()[0]
    out["write_ms"] = sum(ms) / len(ms)
    out["writes"] = len(ms)
    return out
