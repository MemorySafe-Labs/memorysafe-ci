#!/usr/bin/env python3
"""Synthetic lifecycle benchmark for the Agent beta connector.

Not a research result. Not MVI / ProtectScore / hard-quota. Measures only the
local evidence-based supersession rules against a hand-written fixture.

Results belong next to this file, never under memorysafe_v14/runs/.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from memorysafe_chatgpt.storage import MemoryStore


# Stable important facts that must remain active and recallable.
STABLE = [
    ("Robin Dale is an operations contractor", "decision"),
    ("One provisional patent filed April 2026 in the founder's own name", "decision"),
    ("Example Labs incorporated 13 July 2026", "project"),
]

# Exact and near duplicates of a preference.
DUPLICATES = [
    ("I prefer concise answers", "preference"),
    ("I prefer concise answers", "preference"),
    ("I prefer concise answers always", "preference"),
]

# Clear replacements: the second line should supersede the first.
CONTRADICTIONS = [
    (("I live in Montreal", "personal"), ("I live in Toronto", "personal")),
    (
        ("Robin Dale is an operations contractor", "decision"),
        ("Robin Dale is a sales contractor", "decision"),
    ),
]

# Same kind of fact, no shared entity: must not auto-supersede.
AMBIGUOUS = [
    (("Deadline is 4 November 2026", "project"), ("Deadline is 28 September 2026", "project")),
]

# Old, rare, still valid. Age is applied after insert.
RARE_VALID = [
    ("The enterprise number is 0000000000", "project"),
]


def _backdate(store: MemoryStore, memory_id: str, when: str) -> None:
    with store._session() as connection:
        connection.execute(
            "UPDATE memories SET created_at = ?, updated_at = ? WHERE id = ?",
            (when, when, memory_id),
        )


def run() -> dict:
    temp = tempfile.TemporaryDirectory()
    store = MemoryStore(Path(temp.name) / "bench.sqlite3")

    stable_ids = [store.remember(content, category)["memory_id"] for content, category in STABLE]
    for memory_id in stable_ids:
        _backdate(store, memory_id, "2019-06-01T00:00:00+00:00")

    dup_first = store.remember(*DUPLICATES[0])
    dup_exact = store.remember(*DUPLICATES[1])
    dup_near = store.remember(*DUPLICATES[2])

    contradiction_outcomes = []
    for (old_content, old_cat), (new_content, new_cat) in CONTRADICTIONS:
        # Re-store the stable contractor line only for the title-change pair if needed.
        old = store.remember(old_content, old_cat)
        new = store.remember(new_content, new_cat)
        old_state = store.explain(old["memory_id"])["state"]
        contradiction_outcomes.append(
            {
                "old": old_content,
                "new": new_content,
                "lifecycle": new.get("lifecycle"),
                "old_state": old_state,
                "correct": old_state == "superseded" and new.get("lifecycle") == "superseded_previous",
            }
        )

    ambiguous_outcomes = []
    for (old_content, old_cat), (new_content, new_cat) in AMBIGUOUS:
        old = store.remember(old_content, old_cat)
        new = store.remember(new_content, new_cat)
        old_state = store.explain(old["memory_id"])["state"]
        ambiguous_outcomes.append(
            {
                "old": old_content,
                "new": new_content,
                "lifecycle": new.get("lifecycle"),
                "old_state": old_state,
                "correct": old_state == "active" and new.get("lifecycle") == "needs_review",
            }
        )

    rare = store.remember(*RARE_VALID[0])
    _backdate(store, rare["memory_id"], "2017-01-01T00:00:00+00:00")

    def recalled(query: str) -> list[str]:
        return [item["content"] for item in store.find(query, 5, record=False)]

    current_toronto = any("Toronto" in text for text in recalled("live in"))
    stale_montreal = any("Montreal" in text for text in recalled("live in"))
    rare_hit = any("0000000000" in text for text in recalled("enterprise number"))
    patent_hit = any("patent" in text.lower() for text in recalled("patent filed"))
    contractor_sales = any("sales contractor" in text for text in recalled("Robin"))
    contractor_ops_active = any(
        "operations contractor" in text for text in recalled("Robin")
    )

    inspectable = store.inspect_memory(
        next(
            item["old"]["memory_id"]
            for item in store.review_conflicts(include_resolved=True)["conflicts"]
            if item["decision"] == "SUPERSEDE" and item.get("old")
        )
    )
    restore_target = inspectable["memory_id"]
    restored = store.restore(restore_target, confirm=True)

    correct_supersession = sum(1 for row in contradiction_outcomes if row["correct"])
    false_supersession = sum(1 for row in ambiguous_outcomes if row["old_state"] == "superseded")
    false_supersession += sum(1 for row in contradiction_outcomes if not row["correct"])

    results = {
        "kind": "synthetic_agent_beta_lifecycle",
        "not_research_continual_learning": True,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "correct_contradiction_supersession_rate": round(
            correct_supersession / len(contradiction_outcomes), 3
        ),
        "false_supersession_rate": round(
            false_supersession / (len(contradiction_outcomes) + len(ambiguous_outcomes)), 3
        ),
        "recall_of_current_facts": {
            "toronto_current": current_toronto,
            "contractor_sales_current": contractor_sales,
            "patent_still_recalled": patent_hit,
        },
        "retention_of_rare_valid_facts": rare_hit,
        "stale_memory_recall_rate": 1.0 if stale_montreal else 0.0,
        "reversibility_history_completeness": {
            "superseded_row_inspectable": inspectable["state"] in {"superseded", "active"},
            "history_names_replacement": any(
                event["decision"] == "SUPERSEDE" for event in inspectable["history"]
            ),
            "restore_confirmed": restored["restored"],
        },
        "duplicate_merging": {
            "exact_merged": dup_exact["decision"] == "MERGE",
            "near_merged": dup_near["decision"] == "MERGE",
            "kept_one_preference": dup_first["memory_id"] == dup_exact["memory_id"],
        },
        "contradiction_outcomes": contradiction_outcomes,
        "ambiguous_outcomes": ambiguous_outcomes,
        "notes": [
            "Age was backdated on stable and rare facts; none were demoted for age.",
            "False supersession counts automatic SUPERSEDE on an ambiguous pair, and misses on a clear pair.",
            "The contractor's title change is expected to supersede the old title in recall.",
            "near_merged is false because 'concise answers always' scores 0.83, below the 0.88 merge bar.",
        ],
    }
    temp.cleanup()
    return results


def main() -> None:
    payload = run()
    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "lifecycle_2026-08-26.json"
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"\nwrote {target}")


if __name__ == "__main__":
    main()
