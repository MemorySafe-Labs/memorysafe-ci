"""Round 10 (opt-in, MEMORYSAFE_GATE_STALE): "possibly stale" flags for status claims.

Kept apart from the extinction gate (gates_r6), which never uses age. A flag here:
  - is only ever put on a note whose wording reads as a PRESENT status (pending, unsigned, installed,
    current version...; round 13: gates_r6.status_kind -- "was installed ... in 1980" is historical, and
    age alone never flags a note that also holds other facts). This is lexical: a durable fact worded
    like a present status can still be flagged;
  - has a logged reason: newer activity on the same uncommon name (>= STALE_NEWER_DAYS later), or
    not reconfirmed for STALE_DAYS;
  - only lowers the memory in recall (STALE_PENALTY, and never above the newer memory that flagged
    it). It does not weaken, extinguish, evict or delete anything, and does not touch the keep order;
  - is voided by saying the same thing again, cleared by restore, and never raised between two
    memories the user resolved as keep-both;
  - (round 14) is only ever a demotion for an ALL-present status note; a mixed or uncertain note gets a
    review indication instead (STALE_REVIEW, see below), and age alone never touches it.
Relation rows (decision STALE_FLAG) and events (STALE_FLAG / STALE_CLEAR) hold ids and reasons only.
"""
from __future__ import annotations

from typing import Any

from .gates_r6 import (STALE_STATE_FLAG, entities, initials, is_status_claim, keep_both_pair, params,
                       present_status_names, review_status_names, status_kind)

# Round 14 (reviewer A finding 4): a "mixed" note (a present status clause plus other facts or history) or an
# "uncertain" one (the status only in quoted / reported words) is never demoted. When its present or
# reported clause names the subject of much newer activity it gets a REVIEW indication instead: a
# relation row (decision STALE_REVIEW, status 'review', ids and reason only) and an event; find shows it
# with the note (`status_review`); recall order, keep order and state are untouched.
STALE_REVIEW_STATE = "review"


def _days(later: str, earlier: str) -> float:
    from datetime import datetime as _dt
    try:
        return (_dt.fromisoformat(later) - _dt.fromisoformat(earlier)).total_seconds() / 86400.0
    except ValueError:
        return 0.0


def _flag(store: Any, connection: Any, memory_id: str, evidence_id: str, why: str, stamp: str) -> None:
    from .lifecycle import record_relation

    record_relation(connection, old_memory_id=memory_id, new_memory_id=evidence_id, decision="STALE_FLAG",
                    status=STALE_STATE_FLAG, reason="Possibly stale status claim (demoted in recall, nothing else changed).",
                    evidence=why, confidence=0.5, source="gate:stale", created_at=stamp)
    store._record_event(connection, "STALE_FLAG", memory_id,
                        f"Possibly stale: {why}. Only ranked lower in recall; not deleted, not evicted for this. "
                        "A new mention of the same status, or restore, clears it.", source="gate:stale")


def _review(store: Any, connection: Any, memory_id: str, evidence_id: str, why: str, stamp: str) -> None:
    from .lifecycle import record_relation

    record_relation(connection, old_memory_id=memory_id, new_memory_id=evidence_id, decision="STALE_REVIEW",
                    status=STALE_REVIEW_STATE,
                    reason="Part of this note may be out of date (review indication only; nothing demoted or changed).",
                    evidence=why, confidence=0.5, source="gate:stale", created_at=stamp)
    store._record_event(connection, "STALE_REVIEW", memory_id,
                        f"Worth a review: {why}. The note holds other facts or reported words too, so it is NOT "
                        "demoted; only this indication is shown with it.", source="gate:stale")


def _reviewed(connection: Any, memory_id: str, evidence_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id WHERE r.decision = 'STALE_REVIEW' "
        "AND r.status = ? AND r.old_memory_id = ? AND r.new_memory_id = ? AND r.created_at >= m.updated_at LIMIT 1",
        (STALE_REVIEW_STATE, memory_id, evidence_id)).fetchone() is not None


def _flagged(connection: Any, memory_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id WHERE r.decision = 'STALE_FLAG' "
        "AND r.status = ? AND r.old_memory_id = ? AND r.created_at >= m.updated_at LIMIT 1",
        (STALE_STATE_FLAG, memory_id)).fetchone() is not None


def stale_after_write(store: Any, connection: Any, new_id: str, new_content: str, others: list[Any], stamp: str) -> list[str]:
    """Flag status claims made stale by much newer activity on their entity, and (at most once per
    simulated day) status claims not reconfirmed for STALE_DAYS. Returns the ids flagged."""

    p = params()
    flagged: list[str] = []
    reviewed: set[str] = set()
    mid_ents, first_ents = entities(new_content), initials(new_content)
    ents = mid_ents | first_ents
    if ents and others:
        limit = max(3.0, p["ENTITY_DF_MAX"] * len(others))
        for ent in sorted(ents):
            holders = [r for r in others if ent in entities(str(r["content"]))
                       or (ent in mid_ents and ent in initials(str(r["content"])))]
            if not holders or len(holders) > limit:
                continue
            ids = [str(r["id"]) for r in holders]
            times = dict(connection.execute(
                f"SELECT id, updated_at FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall())
            for r in holders:
                mid = str(r["id"])
                content = str(r["content"])
                if mid == new_id or mid in flagged or mid in reviewed or not is_status_claim(content):
                    continue
                # round 13: the name must sit in a PRESENT status clause (not a historical one, not
                # another fact of a mixed note). Round 14: only an all-present note is demoted; a mixed or
                # uncertain note whose present / reported clause has the name gets a review indication.
                kind = status_kind(content)
                demote = kind == "present" and ent in present_status_names(content)
                review = kind in ("mixed", "uncertain") and ent in review_status_names(content)
                if not (demote or review):
                    continue
                age = _days(stamp, str(times.get(mid, stamp)))
                if age < p["STALE_NEWER_DAYS"] or keep_both_pair(connection, mid, new_id):
                    continue
                why = f"its subject '{ent}' has newer activity ({age:.0f} days later, memory {new_id})"
                if demote:
                    _flag(store, connection, mid, new_id, why, stamp)
                    flagged.append(mid)
                elif not _reviewed(connection, mid, new_id):
                    _review(store, connection, mid, new_id, why, stamp)
                    reviewed.add(mid)
    from .gate_flags import store_state

    state = store_state(store)
    last = state.get("_r10_last_sweep")
    if last is None or _days(stamp, last) >= 1.0:
        state["_r10_last_sweep"] = stamp
        from datetime import datetime as _dt, timedelta as _td
        try:
            cutoff = (_dt.fromisoformat(stamp) - _td(days=p["STALE_DAYS"])).isoformat(timespec="seconds")
        except ValueError:
            return flagged
        rows = connection.execute(
            "SELECT id, content, updated_at FROM memories WHERE state = 'active' AND updated_at < ? AND id != ?",
            (cutoff, new_id)).fetchall()
        for r in rows:
            mid = str(r[0])
            # round 13: age alone flags only a note that is entirely a present status claim
            if mid in flagged or status_kind(str(r[1])) != "present" or _flagged(connection, mid):
                continue
            _flag(store, connection, mid, mid + "#age",
                  f"a status claim not reconfirmed for {_days(stamp, str(r[2])):.0f} days", stamp)
            flagged.append(mid)
    return flagged


def stale_ids(connection: Any) -> set[str]:
    """Ids with a standing "possibly stale" flag (not voided by a later reconfirmation)."""
    return {str(r[0]) for r in connection.execute(
        "SELECT r.old_memory_id FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id "
        "WHERE r.decision = 'STALE_FLAG' AND r.status = ? AND m.state = 'active' AND r.created_at >= m.updated_at",
        (STALE_STATE_FLAG,)).fetchall()}


def clear_stale(store: Any, connection: Any, memory_id: str, stamp: str, evidence_from: str | None = None,
                why: str = "restored by the user") -> int:
    # round 14: review indications (STALE_REVIEW) are withdrawn the same way as flags
    sql = ("UPDATE memory_relations SET status = 'cleared', resolved_at = ? WHERE ((decision = 'STALE_FLAG' AND status = ?)"
           " OR (decision = 'STALE_REVIEW' AND status = ?)) AND old_memory_id = ?")
    args: list[Any] = [stamp, STALE_STATE_FLAG, STALE_REVIEW_STATE, memory_id]
    if evidence_from is not None:
        sql += " AND new_memory_id = ?"
        args.append(evidence_from)
    count = int(connection.execute(sql, args).rowcount or 0)
    if count:
        store._record_event(connection, "STALE_CLEAR", memory_id, f"Possibly-stale flag cleared ({why}).", source="gate:stale")
    return count


def stale_newer(connection: Any) -> dict[str, set[str]]:
    """Standing newer-activity flags: flagged id -> ids of the newer memories that flagged it."""
    out: dict[str, set[str]] = {}
    for old, new in connection.execute(
            "SELECT r.old_memory_id, r.new_memory_id FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id "
            "WHERE r.decision = 'STALE_FLAG' AND r.status = ? AND m.state = 'active' AND r.created_at >= m.updated_at "
            "AND r.new_memory_id NOT LIKE '%#age'", (STALE_STATE_FLAG,)):
        out.setdefault(str(old), set()).add(str(new))
    return out


def stale_reorder(scored: list, newer: dict[str, set[str]]) -> None:
    """In place: a flagged status claim never ranks above a newer memory that flagged it, when both
    matched the query. Round 13 (reviewer A P2): enforced over the whole relation graph (chains, several
    flaggers, ties, cycles broken deterministically), see gates_order."""
    from . import gates_order

    if newer:
        gates_order.enforce(scored, gates_order.stale_edges(newer))


def stale_reasons(connection: Any) -> dict[str, str]:
    """Round 13 (host visibility): standing flag -> its logged reason (newest first; ids and reasons
    only, as the relation rows hold them)."""
    out: dict[str, str] = {}
    for old, why in connection.execute(
            "SELECT r.old_memory_id, r.evidence FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id "
            "WHERE r.decision = 'STALE_FLAG' AND r.status = ? AND m.state = 'active' AND r.created_at >= m.updated_at "
            "ORDER BY r.created_at DESC, r.id DESC", (STALE_STATE_FLAG,)):
        out.setdefault(str(old), f"Possibly stale: {why}")
    return out


def status_reviews(connection: Any) -> dict[str, str]:
    """Round 14: standing review indications (mixed / uncertain notes): id -> logged reason, newest first."""
    out: dict[str, str] = {}
    for old, why in connection.execute(
            "SELECT r.old_memory_id, r.evidence FROM memory_relations r JOIN memories m ON m.id = r.old_memory_id "
            "WHERE r.decision = 'STALE_REVIEW' AND r.status = ? AND m.state = 'active' AND r.created_at >= m.updated_at "
            "ORDER BY r.created_at DESC, r.id DESC", (STALE_REVIEW_STATE,)):
        out.setdefault(str(old), f"Worth a review: {why}")
    return out
