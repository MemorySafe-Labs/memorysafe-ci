"""Round 14: the lazily loaded part of `gates` -- NOT a module of its own. `gates._load_lazy()` executes
this file's code in `gates`'s namespace on first use of one of its names (see the end of gates.py), so
every name below resolves exactly as it did when this code sat in gates.py."""
from __future__ import annotations
# ruff: noqa: F821  (names come from gates's namespace)

# --- order and meaning checks (surprise gate) -----------------------------------------


@lru_cache(maxsize=65536)
def order_pairs(normalized: str) -> frozenset[tuple[str, str]]:
    """Adjacent pairs of content words, in order. "Ana thanked Bruno" and "Bruno thanked
    Ana" have the same words (Jaccard 1.0) and no pair in common."""

    sequence = [stem for stem in map(_TERM_MAP.__getitem__, normalized.split()) if stem is not None]
    return frozenset(zip(sequence, sequence[1:]))


def order_differs(left: str, right: str) -> bool:
    """True when two texts that share (almost) the same words put them in a different
    order. Each differing word explains up to two differing adjacent pairs (the pair
    before it and the pair after it); a swapped actor or a reversed relation leaves pairs
    differing that no changed word explains. Two or more unexplained pairs = different order."""

    word_diff = len(terms_of(left) ^ terms_of(right))
    pair_diff = len(order_pairs(left) ^ order_pairs(right))
    return pair_diff - 2 * word_diff >= 2


# Contractions are split by normalisation ("don't" -> "don", "t"), so a lone "t" after one
# of these stems is a negation.
_CONTRACTION_STEMS = frozenset({"don", "doesn", "didn", "isn", "aren", "wasn", "weren", "hasn", "haven", "hadn",
                                "won", "can", "couldn", "wouldn", "shouldn", "mustn", "needn", "ain"})
_NEGATION_WORDS = frozenset({"not", "no", "never", "without", "none", "cannot", "nor", "neither", "nothing",
                             "nobody", "nowhere", "unable", "isnt", "dont", "wont"})

# Words whose swap reverses a statement. A short, hand-written list: it catches the plain
# cases, not paraphrase ("moved earlier" vs "pushed back").
_OPPOSITE_PAIRS = (
    ("before", "after"), ("enable", "disable"), ("enabled", "disabled"), ("open", "closed"), ("open", "close"),
    ("allow", "forbid"), ("allowed", "forbidden"), ("allowed", "banned"), ("increase", "decrease"),
    ("raise", "lower"), ("start", "stop"), ("accept", "reject"), ("accepted", "rejected"),
    ("approve", "deny"), ("approved", "denied"), ("up", "down"), ("on", "off"), ("first", "last"),
    ("always", "never"), ("lock", "unlock"), ("locked", "unlocked"), ("true", "false"), ("pass", "fail"),
    ("passed", "failed"), ("win", "lose"), ("buy", "sell"), ("hire", "fire"), ("arrive", "leave"),
    ("early", "late"), ("safe", "unsafe"), ("present", "absent"), ("add", "remove"), ("added", "removed"),
    ("yes", "no"), ("gave", "received"), ("sent", "received"), ("won", "lost"), ("with", "without"),
)


def negation_count(normalized: str) -> int:
    tokens = normalized.split()
    count = sum(1 for token in tokens if token in _NEGATION_WORDS)
    count += sum(1 for before, token in zip(tokens, tokens[1:]) if token == "t" and before in _CONTRACTION_STEMS)
    return count


def meaning_flipped(left: str, right: str) -> bool:
    """A negation word appears in one text more often than in the other, or one text has a
    word and the other has that word's listed opposite. Both args are normalised text."""

    if negation_count(left) != negation_count(right):
        return True
    left_tokens, right_tokens = set(left.split()), set(right.split())
    for first, second in _OPPOSITE_PAIRS:
        if (first in left_tokens and second in right_tokens and first not in right_tokens and second not in left_tokens) or (
            second in left_tokens and first in right_tokens and second not in right_tokens and first not in left_tokens
        ):
            return True
    return False


def merge_veto(left: str, right: str) -> str:
    """Why two near-identical texts must NOT be merged, or "" when they may be. Used by
    remember() to stop the shipped near-duplicate merge (character similarity, blind to
    word order and to a lone "not") from folding "Bruno thanked Ana" into "Ana thanked
    Bruno" when the surprise gate is on. Both args are normalised text."""

    if meaning_flipped(left, right):
        return "Surprise gate: not merged with the closest memory because it differs by a negation or an opposite word."
    if order_differs(left, right):
        return "Surprise gate: not merged with the closest memory because it uses the same words in a different order (who did what to whom may differ)."
    return ""



# --- surprise write gate ---------------------------------------------------------------


def assess_surprise(
    normalized: str, category: str, content: str, importance: float, active: list[Any],
    load_content: Any = None,
) -> dict[str, Any]:
    """Decide what the surprise gate does with an incoming, not-yet-stored memory.

    `active` rows need: id, normalized_content, and content (or pass `load_content(id)`
    to fetch the nearest row's text only). Returns a dict with `action` one of
    none / merge / downweight / boost, plus the evidence behind it.

    novelty = 1 - (highest content-word Jaccard against any active memory).
    - similarity >= LOW_SIM: low surprise. If either text has an extracted slot or a typed
      value -- date, time, amount, version (it may be a changed fact) -- or the pair differs
      in negation or by a listed opposite word, or the same words come in a different
      ORDER (a swapped actor, a reversed relation), do nothing and leave it to the
      lifecycle check at full weight. Otherwise merge when the numbers match and
      similarity >= MERGE_SIM (0.85), and down-weight in every other case (stored,
      importance capped at 0.5, never auto-protected).
      Safety and decision memories are never touched by this branch.
    - novelty >= HIGH_NOVELTY: high surprise. Importance +0.1; protected when the
      protected share of the store allows (decided by the caller).
    Too few active memories means novelty carries no information, so nothing happens.
    """

    from . import storage
    from .lifecycle import extract_slots, extract_values

    low_sim = _number("MEMORYSAFE_GATE_SURPRISE_LOW_SIM", 0.70)
    merge_sim = _number("MEMORYSAFE_GATE_SURPRISE_MERGE_SIM", 0.85)
    high_novelty = _number("MEMORYSAFE_GATE_SURPRISE_HIGH_NOVELTY", 0.80)
    minimum = int(_number("MEMORYSAFE_GATE_SURPRISE_MIN_ACTIVE", 10))
    result: dict[str, Any] = {"action": "none", "novelty": None, "nearest_id": None, "similarity": None}
    terms = terms_of(normalized)
    if len(active) < minimum or not terms:
        result["why"] = "too few active memories to judge novelty" if terms else "no content words"
        return result
    best, nearest = 0.0, None
    size = len(terms)
    for row in active:
        theirs = terms_of(row["normalized_content"])
        overlap = len(terms & theirs)
        if overlap:
            similarity = overlap / (size + len(theirs) - overlap)
            if similarity > best:
                best, nearest = similarity, row
    novelty = round(1.0 - best, 3)
    result.update(novelty=novelty, similarity=round(best, 3), nearest_id=str(nearest["id"]) if nearest else None)
    if nearest is not None and best >= low_sim and category not in CRITICAL_CATEGORIES:
        nearest_text = nearest["normalized_content"]
        same_numbers = storage._number_tokens(normalized) == storage._number_tokens(nearest_text)
        try:
            nearest_content = nearest["content"]
        except (IndexError, KeyError):
            nearest_content = load_content(nearest["id"]) if load_content else nearest_text
        typed = extract_values(content)[0] or extract_values(nearest_content)[0]
        if extract_slots(content) or extract_slots(nearest_content) or typed:
            # Could be a changed fact. That is lifecycle's call (supersede or review), and
            # it must see the write at full weight.
            result["why"] = "it may restate a changed fact, left to the lifecycle check"
            return result
        if meaning_flipped(normalized, nearest_text):
            result["why"] = "it differs from the closest memory by a negation or opposite word, so it may restate a changed fact, left to the lifecycle check"
            return result
        if order_differs(normalized, nearest_text):
            result["why"] = "it uses the closest memory's words in a different order (who did what to whom may differ), left to the lifecycle check"
            return result
        if same_numbers and best >= merge_sim:
            result["action"] = "merge"
        else:
            result["action"] = "downweight"
            result["why"] = (
                "only its numbers differ" if not same_numbers
                else "it is close to an existing memory but not close enough to merge safely"
            ) + ", so it is stored as a separate, lower-importance entry"
        return result
    if novelty >= high_novelty:
        result["action"] = "boost"
    return result


# --- reversible surprise merge (round 4/5) -------------------------------------------------
#
# The surprise gate's MERGE used to fold a write into the closest memory in place (longer
# wording kept, importance/confidence raised, protection OR-ed) and store nothing for the
# write itself, so there was no row for `restore` to bring back and the earlier wording lived
# only in the history text. Now the same merge is made reversible by store-and-link:
#   * the incoming write is kept as its own row in state MERGE_COPY_STATE ('merge_copy') —
#     hidden from recall, distinct from capacity-gate 'evicted' so health and prune can tell
#     them apart; nothing is lost until pruned;
#   * a memory_relations row (decision MERGE, status 'merged', old = the kept memory, new = the
#     hidden copy) links the two and holds the kept memory's values from before and after the
#     merge in its evidence column;
#   * `restore <copy id>` (storage.restore calls `undo_merge`) brings the copy back as an
#     ordinary active memory and, when the kept memory is still exactly as the merge left it,
#     puts it back exactly as it was before the merge. If the kept memory has been touched
#     since (another merge, protect, edit, forget...) it is left alone and the reply says so;
#     the copy is restored either way.
#   * `prune_merge_copies` (dry-run by default) can delete old copies once the age/count window
#     says so; after that, restore of that id refuses. Default window is empty → prune plans
#     nothing (no surprise deletion).
# What the active set looks like after a merge is unchanged from round 3 (same kept memory,
# same wording, same numbers), so decisions and benchmark results do not move.

MERGE_COPY_STATE = "merge_copy"
MERGE_UNDO_COLUMNS = ("content", "normalized_content", "importance", "confidence", "protected", "updated_at")
MERGE_EVIDENCE_PREFIX = "Undo data for restore (kept memory before and after the merge): "


def keep_merge_copy(
    store: Any, connection: Any, *, target: Any, incoming: dict[str, Any], after: dict[str, Any],
    similarity: float, source: str, timestamp: str, content_bytes: int = 0,
) -> str:
    """Store the merged-away write as a hidden, restorable row and link it to the kept memory.
    Call BEFORE the kept memory is updated (`target` still holds the values from before).
    Returns the copy's id."""

    import uuid
    from datetime import datetime, timezone

    from .lifecycle import record_relation

    copy_id = f"MS-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
    connection.execute(
        """
        INSERT INTO memories(
            id, content, normalized_content, category, importance, confidence,
            protected, state, source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (copy_id, incoming["content"], incoming["normalized_content"], incoming["category"],
         incoming["importance"], incoming["confidence"], int(incoming["protected"]), MERGE_COPY_STATE,
         source, timestamp, timestamp),
    )
    before = {column: target[column] for column in MERGE_UNDO_COLUMNS}
    evidence = MERGE_EVIDENCE_PREFIX + json.dumps({"before": before, "after": after}, sort_keys=True)
    record_relation(
        connection,
        old_memory_id=str(target["id"]),
        new_memory_id=copy_id,
        decision="MERGE",
        status="merged",
        reason=(
            f"Surprise gate merged a write into {target['id']}. The write itself is kept as hidden "
            f"copy {copy_id} (state {MERGE_COPY_STATE}); `memorysafe restore {copy_id} --confirm` "
            "brings it back and undoes the merge."
        ),
        evidence=evidence,
        confidence=float(similarity),
        source="gate:surprise",
        created_at=timestamp,
    )
    store._record_event(
        connection, "MERGE_COPY", copy_id,
        f"Surprise gate: this write was merged into {target['id']} and is kept here as state "
        f"'{MERGE_COPY_STATE}' (out of recall, restorable until pruned). "
        "Restoring it brings it back and undoes the merge.",
        content_bytes, source,
    )
    return copy_id


def plan_undo(connection: Any, memory_id: str) -> dict[str, Any] | None:
    """What `restore` would do to the kept memory if `memory_id` is a merge copy. Read-only.
    None when it is not (no open 'merged' link). Otherwise
    {relation_id, kept_id, will_revert: bool, columns: {column: [now, after_undo]}, why}."""

    link = connection.execute(
        "SELECT id, old_memory_id, evidence FROM memory_relations "
        "WHERE new_memory_id = ? AND decision = 'MERGE' AND status = 'merged' ORDER BY id DESC LIMIT 1",
        (memory_id,),
    ).fetchone()
    if link is None or not str(link["evidence"]).startswith(MERGE_EVIDENCE_PREFIX):
        return None
    try:
        data = json.loads(str(link["evidence"])[len(MERGE_EVIDENCE_PREFIX):])
        before, after = data["before"], data["after"]
    except (ValueError, KeyError, TypeError):
        return None
    plan: dict[str, Any] = {"relation_id": int(link["id"]), "kept_id": str(link["old_memory_id"]),
                            "will_revert": False, "columns": {}, "before": before}
    kept = connection.execute("SELECT * FROM memories WHERE id = ?", (plan["kept_id"],)).fetchone()
    if kept is None:
        plan["why"] = "the memory it was merged into no longer exists, so only the copy is restored"
    elif kept["state"] != "active":
        plan["why"] = f"the memory it was merged into is now {kept['state']}, so it is left as it is and only the copy is restored"
    elif any(kept[column] != after[column] for column in MERGE_UNDO_COLUMNS):
        plan["why"] = ("the memory it was merged into has been changed since the merge, so it is left as it is "
                       "and only the copy is restored")
    else:
        plan["will_revert"] = True
        plan["columns"] = {column: [kept[column], before[column]] for column in MERGE_UNDO_COLUMNS if kept[column] != before[column]}
        plan["why"] = f"the memory it was merged into ({plan['kept_id']}) is put back as it was before the merge"
    return plan


def undo_merge(store: Any, connection: Any, memory_id: str, timestamp: str) -> dict[str, Any] | None:
    """Carry out `plan_undo` (called by storage.restore after the copy is active again).
    Closes the link ('undone' when the kept memory was put back, 'detached' otherwise)."""

    plan = plan_undo(connection, memory_id)
    if plan is None:
        return None
    if plan["will_revert"]:
        before = plan["before"]
        connection.execute(
            "UPDATE memories SET content = ?, normalized_content = ?, importance = ?, confidence = ?, "
            "protected = ?, updated_at = ? WHERE id = ?",
            (*(before[column] for column in MERGE_UNDO_COLUMNS), plan["kept_id"]),
        )
    connection.execute(
        "UPDATE memory_relations SET status = ?, resolved_at = ? WHERE id = ?",
        ("undone" if plan["will_revert"] else "detached", timestamp, plan["relation_id"]),
    )
    return plan


def _prune_window(days: int | None, keep: int | None) -> tuple[int | None, int | None]:
    """Resolve the prune window from arguments and env. Both None → safe no-op."""

    if days is None:
        raw = os.environ.get("MEMORYSAFE_GATE_MERGE_PRUNE_DAYS", "").strip()
        days = int(raw) if raw.isdigit() else None
    if keep is None:
        raw = os.environ.get("MEMORYSAFE_GATE_MERGE_PRUNE_KEEP", "").strip()
        keep = int(raw) if raw.isdigit() else None
    if days is not None and days < 0:
        days = None
    if keep is not None and keep < 0:
        keep = None
    return days, keep


def prune_merge_copies(
    store: Any, apply: bool = False, days: int | None = None, keep: int | None = None,
) -> dict[str, Any]:
    """Remove old surprise-merge copies (state `merge_copy`) past an age and/or count window.

    Dry-run unless apply=True; a dry run writes nothing. Default window comes from
    MEMORYSAFE_GATE_MERGE_PRUNE_DAYS / _KEEP; with both unset (and not passed), the plan is
    empty — nothing is deleted by surprise. Age is measured from the copy's created_at against
    storage._now(). Copies stay restorable until pruned; after prune the row is `deleted`,
    the MERGE link is closed as `pruned`, and `restore` of that id refuses.
    Returns {applied, days, keep, plan: [{id, created_at, kept_id, reason}], pruned: [ids]}.
    """

    from . import storage

    days, keep = _prune_window(days, keep)
    stamp = storage._now()
    plan: list[dict[str, Any]] = []
    with store._session() as connection:
        rows = connection.execute(
            """
            SELECT m.id, m.created_at, r.old_memory_id AS kept_id, r.id AS relation_id
            FROM memories m
            JOIN memory_relations r
              ON r.new_memory_id = m.id AND r.decision = 'MERGE' AND r.status = 'merged'
            WHERE m.state = ?
            ORDER BY m.created_at ASC, m.rowid ASC
            """,
            (MERGE_COPY_STATE,),
        ).fetchall()
        doomed: set[str] = set()
        if days is not None:
            from datetime import datetime, timedelta, timezone

            try:
                now = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                now = datetime.now(timezone.utc)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
            cutoff = now - timedelta(days=days)
            for row in rows:
                try:
                    created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created <= cutoff:
                    doomed.add(str(row["id"]))
        if keep is not None and len(rows) > keep:
            for row in rows[: len(rows) - keep]:
                doomed.add(str(row["id"]))
        by_id = {str(row["id"]): row for row in rows}
        for memory_id in [str(row["id"]) for row in rows if str(row["id"]) in doomed]:
            row = by_id[memory_id]
            plan.append({
                "id": memory_id,
                "created_at": str(row["created_at"]),
                "kept_id": str(row["kept_id"]),
                "relation_id": int(row["relation_id"]),
                "reason": (
                    f"Merge copy {memory_id} (of {row['kept_id']}) is past the prune window "
                    f"(days={days!r}, keep={keep!r}) and will be deleted; restore will refuse."
                ),
            })
        pruned: list[str] = []
        if apply:
            for item in plan:
                changed = connection.execute(
                    "UPDATE memories SET state = 'deleted', deleted_at = ?, updated_at = ? "
                    "WHERE id = ? AND state = ?",
                    (stamp, stamp, item["id"], MERGE_COPY_STATE),
                ).rowcount
                if not changed:
                    continue
                connection.execute(
                    "UPDATE memory_relations SET status = 'pruned', resolved_at = ? WHERE id = ?",
                    (stamp, item["relation_id"]),
                )
                store._record_event(
                    connection, "PRUNE_MERGE_COPY", item["id"], item["reason"], source="gate:prune",
                )
                pruned.append(item["id"])
    return {
        "applied": apply,
        "days": days,
        "keep": keep,
        "plan": plan,
        "pruned": pruned if apply else [],
    }


def is_pruned_merge_copy(connection: Any, memory_id: str) -> bool:
    """True when this id was a merge copy that `prune_merge_copies` has already removed."""

    row = connection.execute(
        "SELECT 1 FROM memory_relations WHERE new_memory_id = ? AND decision = 'MERGE' AND status = 'pruned' LIMIT 1",
        (memory_id,),
    ).fetchone()
    return row is not None


def protect_budget_allows(store: Any, connection: Any, cap: int | None) -> bool:
    """The surprise gate may protect only while protected memories are under a share of
    the store (default 20% of capacity, or of the active count when there is no cap).
    Without this, a cold store would protect everything because everything is novel."""

    share = _number("MEMORYSAFE_GATE_PROTECT_SHARE", 0.20)
    protected, active = connection.execute(
        "SELECT COALESCE(SUM(protected), 0), COUNT(*) FROM memories WHERE state = 'active'"
    ).fetchone()
    return protected < share * (cap or active)


# --- consolidation (sleep replay) ----------------------------------------------------------


def consolidate(
    store: Any, apply: bool = False, capacity: int | None = None, propose_merges: bool = False
) -> dict[str, Any]:
    """Offline pass: replay active memories in keep-priority order, keep the top
    `capacity`, move the rest out of recall, and optionally propose merges.

    Dry-run unless apply=True; a dry run writes nothing at all, not even history.
    Merge proposals are never applied: with apply=True they are only written to the
    history so a person can see them. Protected memories are always kept, even if that
    leaves the store above capacity (reported as `protected_over_capacity`).
    """

    from . import storage

    cap = capacity if capacity else gate_capacity()
    with store._session() as connection:
        rows = _active_rows(connection)
        with _INDEX_LOCK:
            index = index_for(store)
            ranked = rank_rows(rows, index)
            if not apply:
                index.no_save = True  # a dry run writes nothing, not even the sidecar file
            elif index.no_save:
                index.no_save = False
        # Replay order is highest keep-priority first: protected, critical, then rarest /
        # most important / most recalled / most recent.
        replay = list(reversed(ranked))
        moves: list[dict[str, Any]] = []
        if cap and len(replay) > cap:
            unresolved = unresolved_ids(connection)  # round 13: open-conflict sides stay
            movable = [item for item in ranked if item["tier"] < PROTECTED_TIER and item["id"] not in unresolved]
            for item in movable[: len(replay) - cap]:
                moves.append({**_public(item), "reason": _why_moved(item, cap, len(replay), "Consolidation")})
        moved_ids = {move["id"] for move in moves}
        kept = [item for item in replay if item["id"] not in moved_ids]
        proposals = _merge_proposals(kept) if propose_merges else []
        stamp = storage._now()
        if apply:
            by_id = {item["id"]: item for item in ranked}
            for move in moves:
                _move_out(store, connection, by_id[move["id"]], move["reason"], "gate:consolidate", stamp)
            for proposal in proposals:
                reason = proposal["reason"]
                seen = connection.execute(
                    "SELECT 1 FROM decision_events WHERE decision = 'PROPOSE_MERGE' AND memory_id = ? AND reason = ?",
                    (proposal["merge_in"], reason),
                ).fetchone()
                if not seen:
                    store._record_event(connection, "PROPOSE_MERGE", proposal["merge_in"], reason, source="gate:consolidate")
        protected_total = sum(1 for item in ranked if item["tier"] == PROTECTED_TIER)
    return {
        "applied": apply,
        "capacity": cap,
        "active_before": len(replay),
        "active_after": len(kept),
        "keep": [_public(item) for item in kept],
        "move": moves,
        "merge_proposals": proposals,
        "protected_over_capacity": bool(cap and protected_total > cap),
    }


def _public(item: dict[str, Any]) -> dict[str, Any]:
    return {k: item[k] for k in ("id", "category", "tier", "score", "rarity", "similar", "importance", "recalls")}


def _merge_proposals(kept: list[dict[str, Any]], minimum: float = 0.6) -> list[dict[str, Any]]:
    """Pairs among the lower half of the kept, unprotected memories that look like one
    thing said twice. Each memory appears in at most one proposal. Proposal only."""

    # `kept` is highest priority first, so the lower half is its tail.
    candidates = [item for item in kept[len(kept) // 2 :] if item["tier"] == 0]
    used: set[str] = set()
    proposals = []
    for index, keeper in enumerate(candidates):
        if keeper["id"] in used:
            continue
        for other in candidates[index + 1 :]:
            if other["id"] in used or other["category"] != keeper["category"] or other["numbers"] != keeper["numbers"]:
                continue
            similarity = jaccard(keeper["terms"], other["terms"])
            if similarity >= minimum:
                used.update((keeper["id"], other["id"]))
                proposals.append(
                    {
                        "keep": keeper["id"],
                        "merge_in": other["id"],
                        "similarity": round(similarity, 2),
                        "reason": (
                            f"Consolidation proposes merging this memory into {keeper['id']} "
                            f"(content-word overlap {similarity:.2f}, same category and numbers). "
                            "Nothing was changed; merge by hand if you agree."
                        ),
                    }
                )
                break
    return proposals
