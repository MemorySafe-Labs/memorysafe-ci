"""Harder synthetic benchmark for the opt-in gates. DESIGN EVIDENCE ONLY.

One stream per seed (400 slots, capacity 40) mixing:
  commons        routine worded rounds (70%) and numbered log lines (30%), category task
  useful-rare    8 of 12 one-off facts; each is asked about again later (2 recall queries,
                 25-70 slots after the write and again 30-70 slots after that)
  useless-rare   8 of 12 throwaway remarks that are never asked about
  reworded dups  4 of the useful facts are restated in other words 20-60 slots later; and
                 10 common rounds are restated later (half same order with filler dropped,
                 half passive/reordered)
  changing facts 4 "lives in" / "works at" facts, each changed twice (3 versions)
  probes         6 pairs of opposite-meaning rewordings (swapped actor, negation, listed
                 opposite) placed a few slots after the original -- a MERGE of the second
                 into the first is a wrong merge
Three settings: "tell" (useful facts filed under project, remarks under other), "notell"
(both drawn from the same category mix, so only recall evidence separates useful from
useless) and "notell_late" (as notell, but the first recall comes 130-250 slots after the
write, to show what the optional age fade costs a fact that is asked for late).

Arms: fifo (simulated oldest-out window), shipped (MEMORYSAFE_MAX_ACTIVE eviction),
gate_cap, gate_cap+fade (MEMORYSAFE_GATE_AGE_GRACE=3), gate_cap+sur, and the same surprise
gate with novelty-protection off (np, MEMORYSAFE_GATE_SURPRISE_PROTECT=0), with and without fade. Recall queries go through the real store.find(), so recall counts
accrue exactly as in use; a query for a memory that is already gone simply misses.

Metrics (means over seeds): useful retained (any row of the fact still active at the end),
recall hit rate (scheduled queries that found the fact), useless dropped (not active at the
end), latest fact correct (newest value active, no older value active), reworded duplicates
folded (MERGE), wrong merges on the probes, mean remember() wall-clock ms.

Product-direction rule (owner): an outcome counts only if it is faster AND smarter than the
current default (shipped). Here: faster = mean write ms <= shipped's; smarter = no quality
metric worse than shipped by more than 0.05 and at least one better by 0.05 or more.
Anything else is exploratory. The verdict is printed per setting; it is a toy-stream
verdict, not evidence about real memories.

Seeds 0..7 are the reported seeds. Seeds 100..104 (--tune) were used while setting
GATE_FLOOR and the weights, so reported numbers are not from the seeds used to choose them.

Run: PYTHONPATH=src python tests/bench/gates_hard_benchmark.py [--seeds 8] [--tune] [--json out.json]
Temp dirs only; never touches a live store or port 8765.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from memorysafe_chatgpt.storage import MemoryStore  # noqa: E402

LENGTH, CAPACITY = 400, 40

# (fact, query, reworded restatement kept in the same order, passive/reordered restatement)
USEFUL = [
    ("The spare server room key is under the reception desk", "spare server room key",
     "Spare server room key: under reception desk", "Under the reception desk is where the spare server room key stays"),
    ("Our backup generator only starts if the blue switch is held for ten seconds", "backup generator blue switch",
     "Backup generator starts only when blue switch is held ten seconds", "Holding the blue switch for ten seconds is the only way our backup generator starts"),
    ("Vendor Kestrel Optics invoices through a founder's personal email", "Kestrel Optics invoices",
     "Kestrel Optics vendor invoices through founder's personal email", "A founder's personal email is how vendor Kestrel Optics invoices"),
    ("The fire exit behind pharmacy storage is bolted shut on weekends", "fire exit pharmacy storage",
     "Fire exit behind pharmacy storage bolted shut weekends", "On weekends the fire exit behind pharmacy storage is bolted shut"),
    ("Call customer Okafor before any delivery change", "Okafor delivery change",
     "Call customer Okafor before delivery change", "Before any delivery change customer Okafor gets a call"),
    ("The label printer in the dispatch office needs the grey ribbon, not the black one", "label printer ribbon",
     "Dispatch office label printer needs grey ribbon, not black", "The grey ribbon, not the black one, is what the dispatch office label printer needs"),
    ("Dr Lindqvist signs controlled-drug orders only on Wednesdays", "Lindqvist controlled-drug orders",
     "Dr Lindqvist signs controlled-drug orders only Wednesdays", "Controlled-drug orders get signed by Dr Lindqvist only on Wednesdays"),
    ("The freezer alarm code is written inside the lid of the first aid box", "freezer alarm code",
     "Freezer alarm code is written inside first aid box lid", "Inside the lid of the first aid box is the freezer alarm code"),
    ("Pallet jack battery charges in the annex closet behind the mop sink", "pallet jack battery",
     "Pallet jack battery charges in annex closet behind mop sink", "Behind the mop sink in the annex closet the pallet jack battery charges"),
    ("Harbourview clinic wants two-person sign-off for sample couriers", "Harbourview sample couriers sign-off",
     "Harbourview clinic wants two-person sign-off sample couriers", "Sample couriers need two-person sign-off at Harbourview clinic"),
    ("The backup router password lives in the sealed envelope in the safe", "backup router password envelope",
     "Backup router password lives in sealed envelope in safe", "In the safe, the sealed envelope holds the backup router password"),
    ("Interpreter bookings for Tigrinya need to be made three days ahead", "Tigrinya interpreter bookings",
     "Tigrinya interpreter bookings need three days ahead", "Three days ahead is when interpreter bookings for Tigrinya need to be made"),
]

USELESS = [
    "The weather smelled like rain at lunch today", "The vending machine in the lobby played an odd jingle this morning",
    "Someone left a green umbrella near the stairwell", "The coffee tasted slightly burnt at the morning break",
    "A pigeon sat on the window ledge during the meeting", "The elevator music was a saxophone cover of a pop song",
    "Lunch conversation drifted to a documentary about bees", "The new lanyards are a slightly different shade of teal",
    "A visitor asked whether the plants by the entrance are real", "The printer made a funny squeak after the break",
    "The parking lot sign has a sticker of a cartoon fox", "Somebody hummed a lullaby in the supply closet",
]

# (subject query, versions oldest -> newest, value words to look for in the answer)
FACTS = [
    ("where does Dana live", ["Dana lives in Montreal", "Dana lives in Toronto", "Dana lives in Vancouver"], "Vancouver"),
    ("where does Tomas live", ["Tomas lives in Lisbon", "Tomas lives in Porto", "Tomas lives in Faro"], "Faro"),
    ("where does Priya work", ["Priya works at Northwind", "Priya works at Contoso", "Priya works at Fabrikam"], "Fabrikam"),
    ("where does Mei work", ["Mei works at Initech", "Mei works at Globex", "Mei works at Hooli"], "Hooli"),
]

# (original, opposite-meaning rewording). A MERGE of the second into the first is wrong.
PROBES = [
    ("Margarida reported Bartolomeu", "Bartolomeu reported Margarida"),
    ("Lindqvist trained Okonkwo on the new intake form", "Okonkwo trained Lindqvist on the new intake form"),
    ("Ana thanked Bruno", "Bruno thanked Ana"),
    ("Dalia hired Emre", "Emre hired Dalia"),
    ("The loading dock gate stays unlocked during the evening shift", "The loading dock gate stays locked during the evening shift"),
    ("Harbourview clinic accepts walk-in sample couriers", "Harbourview clinic never accepts walk-in sample couriers"),
]

NAMES = ["Ana", "Bruno", "Chen", "Dalia", "Emre", "Farah", "Gil", "Hana", "Ivo", "Jun"]
THINGS = ["oxygen", "linen", "supply", "crash", "medication", "laundry"]
WINGS = ["east", "west", "north", "south", "annex", "lobby"]
SHIFTS = ["early", "late", "night", "weekend", "holiday", "split", "float", "relief"]
VERBS = ["checked", "restocked", "counted", "audited", "cleaned", "inspected", "sealed", "logged"]


def build_events(seed: int, setting: str) -> list[list[tuple]]:
    """Events per slot. Event kinds: ("write", text, category, tag, kind), ("recall", query, tag)."""
    rng = random.Random(seed)
    slots: list[list[tuple]] = [[] for _ in range(LENGTH)]

    def put(slot: int, event: tuple) -> None:
        slots[max(0, min(LENGTH - 1, slot))].append(event)

    def category(role: str) -> str:
        if setting == "tell":
            return "project" if role == "useful" else "other"
        return rng.choice(["project", "other", "task"])

    late = setting.endswith("_late")

    useful = rng.sample(USEFUL, 8)
    restated = set(rng.sample(range(8), 4))
    for index, (text, query, reword, passive) in enumerate(useful):
        tag = f"U{index}"
        slot = rng.randint(10, int(LENGTH * 0.55))
        cat = category("useful")
        put(slot, ("write", text, cat, tag, "useful"))
        first = slot + (rng.randint(130, 250) if late else rng.randint(25, 70))
        put(first, ("recall", query, tag))
        put(first + rng.randint(30, 70), ("recall", query, tag))
        if index in restated:
            put(slot + rng.randint(20, 60), ("write", reword if index % 2 == 0 else passive, cat, tag, "dup"))
    for index, text in enumerate(rng.sample(USELESS, 8)):
        put(rng.randint(10, int(LENGTH * 0.8)), ("write", text, category("useless"), f"X{index}", "useless"))
    for index, (query, versions, _) in enumerate(FACTS):
        tag = f"F{index}"
        for version, share in zip(versions, (0.10, 0.45, 0.75)):
            put(int(LENGTH * share) + rng.randint(-10, 10), ("write", version, "personal" if "live" in query else "other", tag, "fact"))
        put(int(LENGTH * 0.45) + 25 + rng.randint(0, 5), ("recall", query, tag))
    for index in range(10):
        n, t, w = rng.choice(NAMES), rng.choice(THINGS), rng.choice(WINGS)
        slot = rng.randint(20, int(LENGTH * 0.7))
        put(slot, ("write", f"Routine round: {n} checked the {t} cart in the {w} wing", "task", f"C{index}", "common_dup_original"))
        later = (f"{n} checked {t} cart {w} wing today" if index % 2 == 0
                 else f"In the {w} wing, the {t} cart was checked by {n}")
        put(slot + rng.randint(5, 40), ("write", later, "task", f"C{index}", "dup"))
    for index, (original, flipped) in enumerate(PROBES):
        slot = rng.randint(int(LENGTH * 0.3), int(LENGTH * 0.7))
        put(slot, ("write", original, "task", f"P{index}", "probe_original"))
        put(slot + rng.randint(4, 10), ("write", flipped, "task", f"P{index}", "probe_flipped"))
    counter = 0
    for slot in range(LENGTH):
        if rng.random() < 0.7:
            text = (f"Routine round: {rng.choice(NAMES)} {rng.choice(VERBS)} the {rng.choice(THINGS)} cart in the "
                    f"{rng.choice(WINGS)} wing on the {rng.choice(SHIFTS)} shift")
        else:
            counter += 1
            text = f"Routine item {counter}: checked cart in room {counter}"
        slots[slot].append(("write", text, "task", None, "common"))
    return slots


class FifoArm:
    def __init__(self, capacity: int):
        self.window: deque = deque(maxlen=capacity)
        self.merged = 0
        self.wrong_merges = 0
        self.ms = []

    def write(self, text: str, category: str, tag: str | None, kind: str) -> None:
        self.window.append((text, tag, kind))

    def recall(self, query: str, tag: str) -> bool:
        return any(t == tag for _, t, _ in self.window)

    def alive(self, tag: str, kind: str | None = None) -> list[str]:
        return [text for text, t, k in self.window if t == tag and (kind is None or k == kind)]

    def answerable(self, query: str, tag: str) -> bool:
        return self.recall(query, tag)

    def latest_correct(self, tag: str, versions: list[str]) -> bool:
        live = [text for text, t, _ in self.window if t == tag]
        return versions[-1] in live and not any(old in live for old in versions[:-1])

    def active(self) -> int:
        return len(self.window)

    def close(self) -> None:
        pass


class StoreArm:
    def __init__(self, env: dict[str, str], temp: str):
        self.temp = temp
        base = {"MEMORYSAFE_DB_PATH": str(Path(temp) / "d" / "m.sqlite3"), "MEMORYSAFE_STATE_DIR": str(Path(temp) / "s")}
        for name in list(os.environ):
            if name.startswith("MEMORYSAFE_GATE_") or name == "MEMORYSAFE_MAX_ACTIVE":
                os.environ.pop(name)
        self.patch = patch.dict(os.environ, {**base, "MEMORYSAFE_MAX_ACTIVE": str(CAPACITY), **env})
        self.patch.start()
        self.store = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"]))
        self.ids: dict[str, set[str]] = {}
        self.merged = 0
        self.wrong_merges = 0
        self.ms: list[float] = []
        self.probe_original: dict[str, str] = {}

    def write(self, text: str, category: str, tag: str | None, kind: str) -> None:
        start = time.perf_counter()
        result = self.store.remember(text, category)
        self.ms.append(1000 * (time.perf_counter() - start))
        if tag:
            self.ids.setdefault(tag, set()).add(result["memory_id"])
        if kind == "dup" and result["decision"] == "MERGE":
            self.merged += 1
        if kind == "probe_original":
            self.probe_original[tag] = result["memory_id"]
        if kind == "probe_flipped" and result["decision"] == "MERGE" and result["memory_id"] == self.probe_original.get(tag):
            self.wrong_merges += 1

    def _live(self, tag: str) -> list[str]:
        return [i for i in self.ids.get(tag, ()) if self.store.explain(i)["state"] == "active"]

    def recall(self, query: str, tag: str) -> bool:
        hits = self.store.find(query, 3)
        return any(h["memory_id"] in self.ids.get(tag, ()) for h in hits)

    def alive(self, tag: str, kind: str | None = None) -> list[str]:
        return self._live(tag)

    def answerable(self, query: str, tag: str) -> bool:
        return any(h["memory_id"] in self.ids.get(tag, ()) for h in self.store.find(query, 3, record=False))

    def latest_correct(self, tag: str, versions: list[str]) -> bool:
        live = [self.store.explain(i)["content"] for i in self._live(tag)]
        return versions[-1] in live and not any(old in live for old in versions[:-1])

    def active(self) -> int:
        return self.store.health()["active_memories"]

    def close(self) -> None:
        self.patch.stop()


ARMS = {
    "fifo": None,
    "shipped": {},
    "gate_cap": {"MEMORYSAFE_GATE_CAPACITY": "1"},
    "gate_cap+fade": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_AGE_GRACE": "3"},
    "gate_cap+sur": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_SURPRISE": "1"},
    "gate_cap+sur(np)": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_SURPRISE": "1",
                         "MEMORYSAFE_GATE_SURPRISE_PROTECT": "0"},
    "gate_cap+sur(np)+fade": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_SURPRISE": "1",
                              "MEMORYSAFE_GATE_SURPRISE_PROTECT": "0", "MEMORYSAFE_GATE_AGE_GRACE": "3"},
}


def run_one(arm_name: str, seed: int, setting: str) -> dict:
    slots = build_events(seed, setting)
    with tempfile.TemporaryDirectory() as temp:
        arm = FifoArm(CAPACITY) if ARMS[arm_name] is None else StoreArm(ARMS[arm_name], temp)
        try:
            recalls = hits = 0
            kinds: dict[str, str] = {}
            for slot in slots:
                for event in slot:
                    if event[0] == "write":
                        _, text, category, tag, kind = event
                        arm.write(text, category, tag, kind)
                        if tag:
                            kinds.setdefault(tag, kind)
                    else:
                        recalls += 1
                        hits += bool(arm.recall(event[1], event[2]))
            useful = [t for t, k in kinds.items() if k == "useful"]
            useless = [t for t, k in kinds.items() if k == "useless"]
            queries = {e[2]: e[1] for slot in slots for e in slot if e[0] == "recall"}
            facts = [(f"F{i}", v) for i, (_, v, _) in enumerate(FACTS)]
            dups_total = sum(1 for slot in slots for e in slot if e[0] == "write" and e[4] == "dup")
            return {
                "useful_retained": sum(bool(arm.alive(t)) for t in useful) / len(useful),
                "useful_answerable": sum(arm.answerable(queries[t], t) for t in useful) / len(useful),
                "recall_hit": hits / recalls,
                "useless_dropped": sum(not arm.alive(t) for t in useless) / len(useless),
                "latest_fact_correct": sum(arm.latest_correct(t, v) for t, v in facts) / len(facts),
                "dups_folded": arm.merged / dups_total,
                "wrong_merges": arm.wrong_merges,
                "write_ms": (sum(arm.ms) / len(arm.ms)) if arm.ms else None,
                "active": arm.active(),
            }
        finally:
            arm.close()


METRICS = ["useful_retained", "useful_answerable", "recall_hit", "useless_dropped", "latest_fact_correct", "dups_folded"]


def run(seeds: list[int], settings: tuple[str, ...] = ("tell", "notell", "notell_late"), arms: list[str] | None = None) -> dict:
    report: dict = {"length": LENGTH, "capacity": CAPACITY, "seeds": seeds, "settings": {}}
    for setting in settings:
        table = {}
        for arm in (arms or ARMS):
            runs = [run_one(arm, seed, setting) for seed in seeds]
            row = {m: round(sum(r[m] for r in runs) / len(runs), 3) for m in METRICS}
            row["wrong_merges_total"] = sum(r["wrong_merges"] for r in runs)
            row["write_ms"] = None if runs[0]["write_ms"] is None else round(sum(r["write_ms"] for r in runs) / len(runs), 2)
            row["active_at_end"] = round(sum(r["active"] for r in runs) / len(runs), 1)
            row["useful_retained_per_seed"] = [round(r["useful_retained"] * 8) for r in runs]
            table[arm] = row
        report["settings"][setting] = {"arms": table, "verdict": verdict(table)}
    return report


def verdict(table: dict) -> dict:
    """The owner's rule against the shipped default."""
    base = table["shipped"]
    out = {}
    for arm in [a for a in table if a.startswith("gate")]:
        row = table[arm]
        diffs = {m: round(row[m] - base[m], 3) for m in METRICS if m != "dups_folded"}
        worse = [m for m, d in diffs.items() if d < -0.05]
        better = [m for m, d in diffs.items() if d >= 0.05]
        faster = row["write_ms"] <= base["write_ms"]
        smarter = bool(better) and not worse
        out[arm] = {"faster_than_shipped": faster, "smarter_than_shipped": smarter, "better": better, "worse": worse,
                    "product_direction": faster and smarter}
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--tune", action="store_true", help="use the tuning seeds 100..104 instead of 0..N-1")
    parser.add_argument("--arms", nargs="*", help="subset of arms (shipped is always included)")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    seeds = list(range(100, 105)) if args.tune else list(range(args.seeds))
    report = run(seeds, arms=(list(dict.fromkeys(['shipped'] + args.arms)) if args.arms else None))
    print(f"Harder synthetic benchmark: {LENGTH} slots, capacity {CAPACITY}, seeds {seeds[0]}..{seeds[-1]} ({len(seeds)}). Design evidence only.\n")
    for setting, block in report["settings"].items():
        print({"tell": "setting: tell  (useful facts filed under 'project', remarks under 'other')",
               "notell": "setting: notell  (facts and remarks drawn from the same category mix)",
               "notell_late": "setting: notell_late  (as notell, first recall 130-250 slots after the write)"}[setting])
        header = f"  {'arm':21s} {'useful kept':>11s} {'answerable':>10s} {'recall hit':>10s} {'useless drop':>12s} {'fact ok':>8s} {'dups fold':>9s} {'wrong mrg':>9s} {'write ms':>9s}"
        print(header)
        for arm, r in block["arms"].items():
            ms = "-" if r["write_ms"] is None else f"{r['write_ms']:.2f}"
            print(f"  {arm:21s} {r['useful_retained']:>11.1%} {r['useful_answerable']:>10.1%} {r['recall_hit']:>10.1%} "
                  f"{r['useless_dropped']:>12.1%} {r['latest_fact_correct']:>8.1%} {r['dups_folded']:>9.1%} {r['wrong_merges_total']:>9d} {ms:>9s}")
        for arm, v in block["verdict"].items():
            print(f"  verdict {arm}: faster={v['faster_than_shipped']} smarter={v['smarter_than_shipped']} "
                  f"(better: {v['better'] or '-'}; worse: {v['worse'] or '-'}) -> "
                  f"{'product direction' if v['product_direction'] else 'exploratory'}")
        print()
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
