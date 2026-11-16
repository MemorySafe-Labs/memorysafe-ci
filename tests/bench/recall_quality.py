#!/usr/bin/env python3
"""Measure whether recall returns the right memory, not merely a memory.

MemorySafe's promise is that the assistant gets back the fact that matters. Nothing
had ever tested that. Governance, storage and token cost were all measured; retrieval
quality — the thing a user actually experiences — was assumed.

Pre-specified before running: the primary endpoint is hit@1 on a held-out query set
written against a corpus the scorer never sees ranked. Distractor queries have no
correct answer and must return nothing; anything returned there is a false positive.
"""

from __future__ import annotations

import sys, time, tempfile, statistics
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from memorysafe_chatgpt.storage import MemoryStore

# A founder's store, entirely synthetic. Overlapping people, dates, numbers and
# near-duplicates, which is where naive substring matching falls apart.
#
# Every name, figure and identifier below is invented. It used to be real: the
# corpus carried this company's registry and NEQ numbers, an Apple Team ID, named
# advisors with staff titles the claims lock forbids, and retired research figures
# asserted as the correct answers a benchmark checks for. Fixtures are the one
# place claims hide from a document sweep, so they are held to the same rule as a
# deck: if the number is not in CLAIMS_LOCK, it does not appear. Synthetic values
# test retrieval exactly as well.
CORPUS = [
    ("Northwind grant submission closes 4 November 2026 at 5pm", "project"),
    ("Northwind covers 60% of a first pilot, 75% for public organizations", "project"),
    ("Bluepeak Fund resubmission deadline is 28 September 2026", "project"),
    ("Bluepeak scoring opens 1 October, so submit before then", "decision"),
    ("Harbourview accelerator decision expected 18 September 2026", "project"),
    ("The legal workshop runs 28 August to 1 September 2026", "project"),
    ("Robin Dale is an operations contractor and holds no equity", "decision"),
    ("Sam Okafor is a strategic advisor and Harbourview alum", "personal"),
    ("Alex Moreau is on an NDA only, with no contract", "personal"),
    ("Jules Ferreira is a managing director at Sandhill Capital", "personal"),
    ("One provisional patent filed April 2026 in the founder's own name", "decision"),
    ("Every claim must cite the figure from the claims lock, never memory", "decision"),
    ("The demo corpus baseline scored 0.500 on held-out seeds", "project"),
    ("Comparison results are reported only after correction for multiplicity", "project"),
    ("A power calculation must precede any seed-count claim", "project"),
    ("Example Labs incorporated 13 July 2026, registration 00000000", "project"),
    ("The provincial enterprise number is 0000000000", "project"),
    ("Casey prefers straight talk over cheerleading", "preference"),
    ("Deliverables should lead with the one concrete next move", "preference"),
    ("The detection lane produced no statistical lift and is closed", "decision"),
    ("Developer membership is enrolled as Individual, Team AAAAAAAAAA", "project"),
    ("The tunnel binary was Intel-only until v0.0.12 arm64 was vendored", "project"),
    ("Beta 0.3.4 fixed the missing rotate_runtime_log.py in the installer", "project"),
    ("The pitch event is 15 September at the Riverside Hall, in person", "project"),
    ("The pitch event grant is CAD 25,000 non-dilutive", "project"),
]

# query -> the substring that identifies the one correct memory
QUERIES = [
    ("when is the northwind deadline", "closes 4 November"),
    ("how much does northwind cover for a public organization", "75% for public"),
    ("when do I have to resubmit to bluepeak", "28 September"),
    ("when does bluepeak start scoring", "scoring opens 1 October"),
    ("when will harbourview tell me", "18 September"),
    ("what dates is the legal workshop", "28 August to 1 September"),
    ("does the operations contractor have equity", "no equity"),
    ("who is the harbourview alum advising me", "Sam Okafor"),
    ("what contract does Alex have", "NDA only"),
    ("who did I meet at sandhill", "Jules Ferreira"),
    ("how many patents do I actually have", "One provisional"),
    ("where do figures for a claim come from", "claims lock"),
    ("what did the demo corpus baseline score", "0.500"),
    ("when are comparison results reported", "correction for multiplicity"),
    ("what must precede a seed-count claim", "power calculation"),
    ("what is my registration number", "registration 00000000"),
    ("what is my enterprise number", "0000000000"),
    ("how should you talk to me", "straight talk"),
    ("what happened with the detection work", "no statistical lift"),
    ("what is my developer team id", "AAAAAAAAAA"),
    ("does the tunnel run on apple silicon", "arm64"),
    ("what broke the installer", "rotate_runtime_log"),
    ("where is the pitch event held", "Riverside Hall"),
    ("how much is the pitch event prize", "25,000"),
]

# Nothing in the corpus answers these. Returning anything is a false positive.
DISTRACTORS = [
    "what is the capital of Portugal",
    "how do I roast a chicken",
    "what time does the pharmacy close",
    "who won the world cup",
    "what is my blood type",
]


def main() -> int:
    store = MemoryStore(Path(tempfile.mkdtemp()) / "bench.sqlite3")
    for content, category in CORPUS:
        store.remember(content, category, source="automatic")

    hit1 = hit5 = 0
    returned_counts, latencies, misses, ranked = [], [], [], []
    for query, needle in QUERIES:
        start = time.perf_counter()
        hits = store.find(query, 5, record=False)
        latencies.append((time.perf_counter() - start) * 1000)
        texts = [h["content"] for h in hits]
        returned_counts.append(len(texts))
        rank = next((i + 1 for i, t in enumerate(texts) if needle in t), None)
        ranked.append((rank, query, needle, texts))
        if rank == 1:
            hit1 += 1
        if rank is not None:
            hit5 += 1
        else:
            misses.append((query, needle, texts[:2]))

    false_positives = []
    for query in DISTRACTORS:
        hits = store.find(query, 5, record=False)
        if hits:
            false_positives.append((query, [h["content"][:44] for h in hits]))

    n = len(QUERIES)
    print(f"corpus {len(CORPUS)} memories · {n} queries · {len(DISTRACTORS)} distractors\n")
    print(f"  hit@1                {hit1}/{n}  ({100*hit1/n:.0f}%)   <- primary endpoint")
    print(f"  hit@5                {hit5}/{n}  ({100*hit5/n:.0f}%)")
    print(f"  memories per query   {statistics.mean(returned_counts):.1f} of 5 allowed")
    print(f"  median latency       {statistics.median(latencies):.1f} ms")
    print(f"  distractor FPs       {len(false_positives)}/{len(DISTRACTORS)}")

    demoted = [r for r in ranked if r[0] not in (1, None)]
    if demoted:
        print("\n  CORRECT BUT NOT FIRST:")
        for rank, query, needle, texts in demoted:
            print(f"    rank {rank}  {query!r} wanted {needle!r}")
            print(f"        #1 was: {texts[0][:64]}")

    if misses:
        print("\n  MISSED (not in top 5):")
        for query, needle, got in misses:
            print(f"    {query!r} wanted {needle!r}")
            for g in got:
                print(f"        got: {g[:60]}")
    if false_positives:
        print("\n  FALSE POSITIVES on questions the store cannot answer:")
        for query, got in false_positives:
            print(f"    {query!r}")
            for g in got:
                print(f"        {g}")
    return 0 if (hit1 == n and not false_positives) else 1


if __name__ == "__main__":
    sys.exit(main())
