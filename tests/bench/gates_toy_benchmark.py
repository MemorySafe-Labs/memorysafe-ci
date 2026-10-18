"""Toy oddball-retention benchmark for the opt-in gates. DESIGN EVIDENCE ONLY.

Mirrors the shape of the research PoC (memorysafe_pocs/run_foundation_door_poc.py): a long
stream of common items with a few rare "oddball" items mixed in, and a capacity far
smaller than the stream. It asks one question: how many oddballs are still in active
recall at the end? It is synthetic text written for this file. It says nothing about real
memories, real users or the research stack, and the gates were
not fitted to it, with one disclosed exception: after the first run showed the surprise
gate merging 224 of 300 writes, the merge threshold was raised from 0.70 to 0.85 (see
README_GATES.txt). The numbers below are from after that change.

Arms
  fifo          oldest memory out first (simulated here, not a store): the plain recency baseline
  default       the store as shipped: MEMORYSAFE_MAX_ACTIVE eviction, no gates
  gate_cap      MEMORYSAFE_GATE_CAPACITY on
  gate_cap+sur  capacity gate + surprise write gate

Four streams
  numbered      common items are numbered log lines ("Routine item 7: checked cart in room 7").
                The shipped eviction already recognises these as routine: its best case.
  worded_samecat  as worded, but every oddball is filed under the same category as the common
                items ("task"), so category gives the gate no hint. The hardest arm here.
  overlap       as worded_samecat, but each oddball is the common sentence plus a few extra
                words ("... and found the lock broken"): the adversarial case for a lexical
                gate, where an oddball is only slightly different from the crowd.
  worded        common items are reworded without digits ("Routine round: Ana checked the
                oxygen cart in the east wing"), which the shipped numeric-template check
                cannot see as a series.

Run from the repo root:  PYTHONPATH=src python tests/bench/gates_toy_benchmark.py [--json out.json]
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

ODDBALLS = [
    ("The spare server room key is under the reception desk", "other"),
    ("Margarida prefers printed agendas and never uses the shared tablet", "preference"),
    ("The fire exit behind pharmacy storage is bolted shut on weekends", "safety"),
    ("Our backup generator only starts if the blue switch is held for ten seconds", "project"),
    ("Vendor Kestrel Optics invoices through the personal email of their founder", "other"),
    ("The lobby piano is locked and the key lives with the night janitor", "personal"),
    ("Parking validation stamps are kept in the cafeteria freezer drawer", "other"),
    ("Dr Okafor is allergic to latex and needs the nitrile gloves from supply B", "safety"),
]
# Oddballs worded like the common items, differing by a few words (the adversarial case).
OVERLAP_ODDBALLS = [
    "Routine round: {n} checked the {t} cart in the {w} wing and found the lock broken",
    "Routine round: {n} checked the {t} cart in the {w} wing and found a leaking valve",
    "Routine round: {n} checked the {t} cart in the {w} wing and found it missing",
    "Routine round: {n} checked the {t} cart in the {w} wing and found the sensor beeping",
    "Routine round: {n} checked the {t} cart in the {w} wing and found mould on the shelf",
    "Routine round: {n} checked the {t} cart in the {w} wing and found the wheels jammed",
    "Routine round: {n} checked the {t} cart in the {w} wing and found the drawer sticking",
    "Routine round: {n} checked the {t} cart in the {w} wing and found no power",
]
NAMES = ["Ana", "Bruno", "Chen", "Dalia", "Emre", "Farah", "Gil", "Hana", "Ivo", "Jun"]
THINGS = ["oxygen", "linen", "supply", "crash", "medication", "laundry"]
WINGS = ["east", "west", "north", "south", "annex", "lobby"]


def build_stream(kind: str, length: int, odd: int, seed: int, odd_category: str | None = None) -> list[tuple[str, str, bool]]:
    rng = random.Random(seed)
    items: list[tuple[str, str, bool]] = []
    for i in range(length - odd):
        if kind == "numbered":
            text = f"Routine item {i}: checked cart in room {i}"
        else:
            text = f"Routine round: {rng.choice(NAMES)} checked the {rng.choice(THINGS)} cart in the {rng.choice(WINGS)} wing"
        items.append((text, "task", False))
    # Oddballs arrive in the first 60% of the stream, so a plain recency baseline has had
    # time to push them out; positions are random but fixed by the seed.
    positions = sorted(rng.sample(range(5, int(length * 0.6)), odd))
    pool = ODDBALLS[:odd]
    if kind == "overlap":
        pool = [(t.format(n=rng.choice(NAMES), t=rng.choice(THINGS), w=rng.choice(WINGS)), "task")
                for t in OVERLAP_ODDBALLS[:odd]]
    for position, (text, category) in zip(positions, pool):
        items.insert(position, (text, odd_category or category, True))
    return items


def run_fifo(stream: list[tuple[str, str, bool]], capacity: int) -> dict:
    window: deque[tuple[str, bool]] = deque(maxlen=capacity)
    for text, _category, is_odd in stream:
        window.append((text, is_odd))
    return {"retained_oddballs": sum(1 for _, odd in window if odd), "active": len(window),
            "write_ms_mean": None, "merged_writes": 0, "gate_moves": 0}


def run_store(stream: list[tuple[str, str, bool]], capacity: int, env: dict[str, str]) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        base = {"MEMORYSAFE_DB_PATH": str(Path(temp) / "d" / "m.sqlite3"),
                "MEMORYSAFE_STATE_DIR": str(Path(temp) / "s")}
        for flag in ("MEMORYSAFE_GATE_CAPACITY", "MEMORYSAFE_GATE_SURPRISE", "MEMORYSAFE_GATE_CONSOLIDATE",
                     "MEMORYSAFE_GATE_MAX_ACTIVE", "MEMORYSAFE_MAX_ACTIVE"):
            os.environ.pop(flag, None)
        with patch.dict(os.environ, {**base, "MEMORYSAFE_MAX_ACTIVE": str(capacity), **env}):
            store = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"]))
            odd_ids: set[str] = set()
            merged = 0
            elapsed = 0.0
            for text, category, is_odd in stream:
                start = time.perf_counter()
                result = store.remember(text, category)
                elapsed += time.perf_counter() - start
                merged += result["decision"] == "MERGE"
                if is_odd and result["decision"] != "MERGE":
                    odd_ids.add(result["memory_id"])
            health = store.health()
            states = {i: store.explain(i)["state"] for i in odd_ids}
            return {
                "retained_oddballs": sum(1 for s in states.values() if s == "active"),
                "active": health["active_memories"],
                "write_ms_mean": round(1000 * elapsed / len(stream), 2),
                "merged_writes": merged,
                "gate_moves": health["decision_counts"].get("EVICT", 0),
            }


ARMS = {
    "default": {},
    "gate_cap": {"MEMORYSAFE_GATE_CAPACITY": "1"},
    "gate_cap+sur": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_SURPRISE": "1"},
}


def run(length: int = 300, odd: int = 8, capacity: int = 30, seeds: int = 5) -> dict:
    report: dict = {"length": length, "oddballs": odd, "capacity": capacity, "seeds": seeds, "streams": {}}
    for kind in ("numbered", "worded", "worded_samecat", "overlap"):
        rows: dict[str, list[dict]] = {"fifo": [], **{name: [] for name in ARMS}}
        for seed in range(seeds):
            stream = build_stream(kind.split("_")[0], length, odd, seed, "task" if kind.endswith("samecat") else None)
            rows["fifo"].append(run_fifo(stream, capacity))
            for name, env in ARMS.items():
                rows[name].append(run_store(stream, capacity, env))
        summary = {}
        for name, results in rows.items():
            summary[name] = {
                "oddballs_retained_per_seed": [r["retained_oddballs"] for r in results],
                "oddball_retention": round(sum(r["retained_oddballs"] for r in results) / (odd * seeds), 3),
                "mean_active_at_end": round(sum(r["active"] for r in results) / seeds, 1),
                "mean_write_ms": None if results[0]["write_ms_mean"] is None
                else round(sum(r["write_ms_mean"] for r in results) / seeds, 2),
                "mean_writes_merged": round(sum(r["merged_writes"] for r in results) / seeds, 1),
                "mean_moved_out_of_recall": round(sum(r["gate_moves"] for r in results) / seeds, 1),
            }
        report["streams"][kind] = summary
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--length", type=int, default=300)
    parser.add_argument("--oddballs", type=int, default=8)
    parser.add_argument("--capacity", type=int, default=30)
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    report = run(args.length, args.oddballs, args.capacity, args.seeds)
    print(f"Toy benchmark: {report['length']} writes, {report['oddballs']} oddballs, capacity {report['capacity']}, "
          f"{report['seeds']} seeds. Synthetic; design evidence only.\n")
    for kind, summary in report["streams"].items():
        print(f"stream: {kind}")
        print(f"  {'arm':14s} {'oddball retention':>18s} {'per seed':>22s} {'active':>7s} {'write ms':>9s} {'merged':>7s} {'moved':>6s}")
        for name, s in summary.items():
            ms = "-" if s["mean_write_ms"] is None else f"{s['mean_write_ms']:.2f}"
            print(f"  {name:14s} {s['oddball_retention']:>18.1%} {str(s['oddballs_retained_per_seed']):>22s} "
                  f"{s['mean_active_at_end']:>7} {ms:>9s} {s['mean_writes_merged']:>7} {s['mean_moved_out_of_recall']:>6}")
        print()
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
