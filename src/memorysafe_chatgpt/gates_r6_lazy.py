"""Round 14: the lazily loaded part of `gates_r6` -- NOT a module of its own. `gates_r6._load_lazy()` executes
this file's code in `gates_r6`'s namespace on first use of one of its names (see the end of gates_r6.py), so
every name below resolves exactly as it did when this code sat in gates_r6.py."""
from __future__ import annotations
# ruff: noqa: F821  (names come from gates_r6's namespace)

def _goal_profiles(fc: _FeatureCache, connection: Any, rows: dict[str, tuple], p: dict) -> list[frozenset]:
    """For each active goal memory: its KEY words -- its content words minus the words that only
    say it is a goal ("want", "need", "before" ...) and minus words that are common in the store
    (in more than max(5, GOAL_DF_MAX x active) memories: a person's own name, "team" ...).
    Recomputed when the goal set changes and otherwise every 128 active-count steps."""

    if fc.goal_ids is None:
        fc.goal_ids = set()
        maybe = [mid for mid, row in rows.items() if _maybe_goal(row[2])]
        for start in range(0, len(maybe), 500):
            chunk = maybe[start:start + 500]
            for mid, content in connection.execute(
                    f"SELECT id, content FROM memories WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall():
                if _GOAL_RE.search(str(content)):
                    fc.goal_ids.add(mid)
    fc.goal_ids &= rows.keys()
    goals = sorted(fc.goal_ids)
    if not goals:
        fc.goal_profiles = []
        return []
    key = (tuple((g, rows[g][2]) for g in goals), len(rows) // 128, p["GOAL_DF_MAX"])
    if key == fc.goal_key:
        return fc.goal_profiles
    _feats(fc, connection, goals, rows)
    frame = gates.terms_of(_GOAL_FRAME_TEXT)
    wanted = frozenset().union(*(fc.by_id[g][1][3] for g in goals)) - frame
    df = dict.fromkeys(wanted, 0)
    term_map = gates._TERM_MAP
    for row in rows.values():
        seen = {term_map[w] for w in row[2].split()} & wanted
        for t in seen:
            df[t] += 1
    limit = max(5.0, p["GOAL_DF_MAX"] * len(rows))
    common = {t for t, n in df.items() if n > limit}
    fc.goal_profiles = [fc.by_id[g][1][3] - frame - common for g in goals]
    fc.goal_key = key
    return fc.goal_profiles


def _goal2_state(fc: _FeatureCache, connection: Any, rows: dict[str, tuple], p: dict) -> Any:
    """Round 11: goal profiles by names (gates_goal.profiles) plus the token document frequencies
    that decide which names are too common to count. Recomputed when the goal set changes and
    otherwise every 128 active-count steps (as the round-7 profiles)."""
    from . import gates_goal

    goals = sorted(fc.goal_ids or ())
    if not goals:
        fc.goal2 = ([], {}, 0.0, frozenset())
        return fc.goal2
    key = (tuple((g, rows[g][2]) for g in goals), len(rows) // 128, p["GOAL_DF_MAX"])
    if key == fc.goal2_key:
        return fc.goal2
    texts = {}
    for start in range(0, len(goals), 500):
        chunk = goals[start:start + 500]
        texts.update({str(m): str(c) for m, c in connection.execute(
            f"SELECT id, content FROM memories WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()})
    _feats(fc, connection, goals, rows)
    df: dict[str, int] = {}
    for row in rows.values():
        for t in set(row[2].split()):
            df[t] = df.get(t, 0) + 1
    limit = max(5.0, p["GOAL_DF_MAX"] * len(rows))
    frame_words = frozenset(_GOAL_FRAME_TEXT.split())
    frame = gates.terms_of(_GOAL_FRAME_TEXT)
    keys = {g: fc.by_id[g][1][3] for g in texts}
    profs = [(g, frozenset(n for n in names if n not in frame_words), k)
             for g, names, k in gates_goal.profiles(texts, df, limit, frame, keys)]
    allnames = frozenset().union(*(n for _g, n, _k in profs)) if profs else frozenset()
    fc.goal2 = (profs, df, limit, allnames)
    fc.goal2_key = key
    return fc.goal2


def _goal2_prepare(fc: _FeatureCache, connection: Any, rows: dict[str, tuple], p: dict) -> None:
    if not gates._flag("MEMORYSAFE_GATE_GOAL2"):   # round 13: same as gates_goal.enabled(), no import
        return
    from . import gates_goal

    if gates_goal.enabled():
        p["_GOAL2"] = 1.0
        _goal2_state(fc, connection, rows, p)


def _goal_tag_live(fc: _FeatureCache, connection: Any, rows: dict[str, tuple], goal_id: str) -> bool:
    """A caller's goal tag counts while its goal memory exists and was not forgotten or superseded by
    the user (a goal moved out by the capacity gate still counts: the caller said this serves it)."""
    if goal_id in rows:
        return True
    memo = fc.__dict__.setdefault("goal_live", {})
    if goal_id not in memo:
        row = connection.execute("SELECT state FROM memories WHERE id = ?", (goal_id,)).fetchone()
        memo[goal_id] = row is not None and str(row[0]) not in ("forgotten", "superseded")
    return memo[goal_id]


def _is_head(fc: _FeatureCache, connection: Any, rows: dict[str, tuple], mid: str) -> bool:
    """True when `mid` is the newest active memory for at least one of its slots (kind, subject).
    Only newer rows whose text could hold that slot (subject word and a word the slot pattern
    needs) are read; the answer is remembered with the newest rowid checked and the rows that
    blocked it, and only re-checked past that point while those rows are still active."""

    slots = fc.by_id[mid][1][0]
    if not slots:
        return False
    row = rows[mid]
    book = fc.book
    newest = book.rowids[-1] if book.rowids else 0
    memo = fc.head_memo.get(mid)
    blocked, start = {}, row[0]
    if memo is not None and memo[0] == row[2]:
        _text, upto, was = memo
        if all((r := rows.get(b)) is not None and r[2] == text for b, text in was.values()):
            blocked, start = dict(was), upto  # every blocker is still there, unchanged: only newer rows can matter
    open_keys = [k for k in slots if k not in blocked]
    if open_keys and newest > start:
        pos = bisect_right(book.rowids, start)
        newer = [fc.rid2mid[r] for r in book.rowids[pos:]]
        for key in open_keys:
            kind, subject = key
            words = [w for w in subject.split() if len(w) > 1]
            word = max(words, key=len) if words else ""
            hints = _KIND_HINT.get(kind, ("",))
            cands = [m for m in newer if word in rows[m][2] and any(h in f" {rows[m][2]} " for h in hints)]
            if cands:
                _feats(fc, connection, cands, rows)
                for m in reversed(cands):
                    if key in fc.by_id[m][1][0]:
                        blocked[key] = (m, rows[m][2])
                        break
    fc.head_memo[mid] = (row[2], newest, blocked)
    return any(k not in blocked for k in slots)


def _rank_useful_book(store: Any, connection: Any, count: int, cap: int, keep_id: str | None = None,
                      index_rows: Any = None) -> list[dict[str, Any]]:
    """The `count` lowest keep-priority movable memories under the usefulness rule.

    Exact, but lazy (round 7): every row gets a lower bound on its keep priority from columns
    the database already holds (category, importance, recalls, extinction weakening); rows are
    visited from the lowest bound up, and a row's text features, slot-head test, goal test and
    neighbour count are worked out only when its bound is still low enough to matter. The
    answer is the same as computing everything for every row (tests compare the two); a cold
    process no longer reads and parses the text of every memory on its first write."""

    p = params()
    fc = _feature_cache(store)
    book = _snapshot(connection, fc, keep_id)
    rows = book.rows
    weak = weak_strengths(connection) if extinction_enabled() else {}
    profiles = _goal_profiles(fc, connection, rows, p)
    _goal2_prepare(fc, connection, rows, p)
    cut = 1.0 - p["WEAK_CUT"]
    excluded = gates.unresolved_ids(connection)  # round 13 (reviewer A P1): open-conflict sides never move
    heap: list[tuple] = []
    for mid, row in rows.items():
        if row[5] or mid == keep_id or mid in excluded:
            continue
        lb = _base_u(p, row[3], row[4], row[6])
        if mid in weak:
            lb *= cut
        heap.append((lb, row[0], 0, mid))
    heapq.heapify(heap)
    best: list[tuple] = []
    state: dict[str, Any] = {}
    min_mult = 1.0 + p["RARITY_BONUS"] / gates.CLUSTER_CAP

    def index() -> gates.ActiveIndex:
        if "index" not in state:
            ix = gates.index_for(store)
            ix.no_save = False
            gates._current_book(connection, ix, keep_id)
            fc.stats["index_syncs"] += 1
            state["index"] = ix
        return state["index"]

    def in_quota(mid: str) -> bool:
        if "quota" not in state:
            items = _full_items(fc, connection, rows, keep_id, p, weak, profiles, excluded)
            ix = index()
            rare = []
            for m2, (u2, ev2, _h, _g) in items.items():
                if ev2:
                    c2 = _cluster(ix, m2)
                    if c2 <= p["RARE_CLUSTER"]:
                        rare.append((u2 * (1 + p["RARITY_BONUS"] / c2), rows[m2][0], m2))
            rare.sort(reverse=True)
            state["quota"] = {m for *_x, m in rare[: math.ceil(p["RARE_QUOTA"] * cap)]}
        return mid in state["quota"]

    info: dict[str, tuple] = {}
    while heap:
        key, rowid, stage, mid = heap[0]
        # Round 13: stop on (bound, rowid), the order `best` is kept in. Exact (a row's priority is never
        # below its bound and heap order is (bound, rowid)), and it no longer visits every row that merely
        # TIES the last kept bound -- a store of many same-category, same-importance rows made the cold
        # first write evaluate hundreds of rows (reviewer B's 2.2x).
        if len(best) >= count and (key, rowid) > (best[-1][0], best[-1][1]):
            break
        heapq.heappop(heap)
        row = rows[mid]
        if stage == 0:
            fc.stats["evaluated"] += 1
            _feats(fc, connection, [mid], rows)
            f = fc.by_id[mid][1]
            head = _is_head(fc, connection, rows, mid)
            goal, mattered, gev = _goal_signal(fc, connection, rows, mid, f, profiles, p)
            u, ev = _usefulness(p, row, head, f, goal, weak.get(mid, 0), mattered, gev)
            info[mid] = (u, ev, head, f[1], goal, f[4])
            if ev:
                heapq.heappush(heap, (u * min_mult, rowid, 1, mid))
                continue
            priority, cluster = u, None
        else:
            u, ev, head, _a, _g, _h = info[mid]
            cluster = _cluster(index(), mid)
            priority = u * (1 + p["RARITY_BONUS"] / cluster)
        if len(best) >= count and (priority, rowid) > (best[-1][0], best[-1][1]):
            continue
        if cluster is not None and cluster <= p["RARE_CLUSTER"] and in_quota(mid):
            continue
        u, ev, head, anchored, goal, hazard = info[mid]
        best.append((priority, rowid, {"id": mid, "u": round(u, 3), "evidence": ev, "head": head,
                                       "anchored": anchored, "goal": goal, "hazard": hazard, "cluster": cluster,
                                       "priority": round(priority, 4), "row": row, "weak": weak.get(mid, 0)}))
        best.sort(key=lambda e: (e[0], e[1]))
        if len(best) > count:
            best.pop()
    if index_rows is not None:
        index_rows.update(state)
    return [item for _p, _r, item in best]


def rank_useful_full(store: Any, connection: Any, count: int, cap: int, keep_id: str | None = None) -> list[str]:
    """Reference: the same rule computed for every row (for tests; not used on the write path)."""

    p = params()
    fc = _feature_cache(store)
    rows = _snapshot(connection, fc, keep_id).rows
    weak = weak_strengths(connection) if extinction_enabled() else {}
    profiles = _goal_profiles(fc, connection, rows, p)
    _goal2_prepare(fc, connection, rows, p)
    items = _full_items(fc, connection, rows, keep_id, p, weak, profiles, gates.unresolved_ids(connection))
    ix = gates.index_for(store)
    gates._current_book(connection, ix, keep_id)
    prio = {}
    for mid, (u, ev, _h, _g) in items.items():
        prio[mid] = (u * (1 + p["RARITY_BONUS"] / _cluster(ix, mid)), _cluster(ix, mid)) if ev else (u, None)
    rare = sorted(((prio[m][0], rows[m][0], m) for m in items if prio[m][1] is not None and prio[m][1] <= p["RARE_CLUSTER"]),
                  reverse=True)
    quota = {m for *_x, m in rare[: math.ceil(p["RARE_QUOTA"] * cap)]}
    order = sorted(((prio[m][0], rows[m][0], m) for m in items if m not in quota))
    return [m for *_x, m in order[:count]]


def after_resolution(store: Any, connection: Any, action: str, old_id: str, new_id: str, stamp: str) -> None:
    """resolve_conflict hook. supersede (extinction flag on): weaken active duplicates of the loser.
    revert / keep_both (always, round 13): the extinction and stale evidence the pair gave against each
    other is withdrawn."""

    from .lifecycle import classify_pair

    if action in ("revert", "keep_both"):
        undo_weakening(store, connection, old_id, stamp, evidence_from=new_id,
                       why=f"conflict resolved with '{action}': the evidence from {new_id} is withdrawn")
        # Round 13 (reviewer A P2): withdraw the PAIR's stale evidence on revert as well as keep_both, whatever
        # the current flags (a relation written while STALE was on must not come back when it is
        # re-enabled). Age evidence ('<id>#age') and evidence from other memories are left alone.
        from .gates_stale import clear_stale

        for a, b in ((old_id, new_id), (new_id, old_id)):
            clear_stale(store, connection, a, stamp, evidence_from=b,
                        why=f"the pair was resolved with '{action}': the evidence from {b} is withdrawn")
        return
    if action != "supersede":
        return
    others = connection.execute("SELECT id, content, confidence FROM memories WHERE state = 'active' AND id != ?",
                                (new_id,)).fetchall()
    for mid, found in _duplicates_of(connection, old_id, others, classify_pair).items():
        _apply(store, connection, mid, new_id, found, stamp)


def undo_weakening(store: Any, connection: Any, memory_id: str, stamp: str, evidence_from: str | None = None,
                   why: str = "restored by the user") -> int:
    sql = ("UPDATE memory_relations SET status = 'undone', resolved_at = ? "
           "WHERE status IN ('weakened', 'proposed') AND old_memory_id = ?")
    args: list[Any] = [stamp, memory_id]
    if evidence_from is not None:
        sql += " AND new_memory_id = ?"
        args.append(evidence_from)
    count = int(connection.execute(sql, args).rowcount or 0)
    if count:
        store._record_event(connection, "UNWEAKEN", memory_id,
                            f"Extinction undone ({why}): {count} piece(s) of evidence cleared.", source="gate:extinction")
    return count


def order_pairs(connection: Any) -> list[tuple[str, str]]:
    """(older, newer) pairs where the newer memory gave standing extinction evidence (weakened or
    proposed) against the older one. find() lists the newer first when both are returned and its
    confidence is at least the older's -- the rule shipped find() applies to an open conflict."""

    if not params()["ORDER_RULE"]:
        return []
    return [(str(a), str(b)) for a, b in connection.execute(
        "SELECT old_memory_id, new_memory_id FROM memory_relations WHERE status IN ('weakened', 'proposed')").fetchall()]


def apply_order(scored: list[tuple], limit: int, pairs: list[tuple[str, str]]) -> bool:
    """Mutates `scored` (score, row) in place; returns whether anything moved. Round 13 (reviewer A P2):
    over the whole relation graph and all candidates (not a window), see gates_order; `limit` unused."""

    from . import gates_order

    if not pairs or not scored:
        return False
    return gates_order.enforce(scored, gates_order.confidence_edges(scored, pairs))


def pending_weakening(connection: Any, memory_id: str) -> int:
    return int(connection.execute(
        "SELECT COUNT(*) FROM memory_relations WHERE status = 'weakened' AND old_memory_id = ?", (memory_id,)
    ).fetchone()[0])


def weaken(store: Any, memory_id: str, reason: str = "user confirmed", confirm: bool = False) -> dict[str, Any]:
    """Manual weakening; the only way a protected memory is weakened. Needs confirm=True."""

    from . import storage

    if not confirm:
        return {"weakened": False, "needs_confirmation": True,
                "reason": "Nothing was changed. Call again with confirm=true after the user agrees."}
    stamp = storage._now()
    with store._session() as connection:
        result = _apply(store, connection, memory_id, memory_id + "#manual", [("manual", reason)], stamp, confirm=True)
    return {"weakened": bool(result.get("applied")), **result}
