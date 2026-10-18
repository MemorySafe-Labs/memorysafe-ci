"""Round 13 (reviewer A P2, "one-pass ordering does not enforce chains"): one ordering over the whole
relation graph for find(). PROTOTYPE; used only when a gate flag is on.

An edge (low, high) says: when both memories are among the results, `high` must rank above `low`.
Edges come from
  * possibly-stale status claims: (flagged claim, the newer memory that flagged it) -- always,
    when MEMORYSAFE_GATE_STALE is on (ORDER_RULE does not switch these off);
  * extinction evidence (ORDER_RULE on): (older, newer) when the newer's confidence >= the older's;
  * open conflicts (the shipped rule): (older, newer) when the newer's confidence >= the older's.

`enforce` makes every edge hold at once, over ALL scored candidates (not only a window), so the
invariant also holds at the edge of the returned window: a memory is never returned above, or
instead of, a memory that must rank above it and matched the same query. Chains (a < b < c) and
several flaggers are handled because scores are propagated in topological order. A cycle (possible
only through contradictory evidence) is broken deterministically: inside a strongly connected
component, newest `updated_at` first, then memory id; edges that disagree with that order are dropped.
Scores are only ever LOWERED, by the smallest step that restores the order (eps below the lowest
memory that must outrank it), so memories without relations keep their scores.
"""

from __future__ import annotations

from typing import Any, Iterable

EPS = 1e-4


def _field(row: Any, name: str, default: Any = "") -> Any:
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _sccs(nodes: list[str], succ: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan, iterative (no recursion limit); nodes visited in the given order (deterministic)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on.add(node)
            children = succ.get(node, [])
            if i < len(children):
                work.append((node, i + 1))
                child = children[i]
                if child not in index:
                    work.append((child, 0))
                elif child in on:
                    low[node] = min(low[node], index[child])
                continue
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    top = stack.pop()
                    on.discard(top)
                    comp.append(top)
                    if top == node:
                        break
                out.append(comp)
    return out


def consistent_edges(scored: list, edges: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """The edges among scored candidates, with cycles broken deterministically."""
    rows = {str(_field(row, "id")): row for _s, row in scored}
    pairs = sorted({(str(a), str(b)) for a, b in edges if str(a) in rows and str(b) in rows and str(a) != str(b)})
    if not pairs:
        return []
    succ: dict[str, list[str]] = {}
    for low, high in pairs:
        succ.setdefault(high, []).append(low)
    nodes = sorted({n for p in pairs for n in p})
    comp_of: dict[str, int] = {}
    rank: dict[str, tuple] = {}
    for c, comp in enumerate(_sccs(nodes, succ)):
        for n in comp:
            comp_of[n] = c
        if len(comp) > 1:
            # newest first, then id: position 0 ranks highest
            ordered = sorted(comp, key=lambda m: (_neg(str(_field(rows[m], "updated_at"))), m))
            for pos, m in enumerate(ordered):
                rank[m] = pos
    kept = []
    for low, high in pairs:
        if comp_of[low] == comp_of[high] and rank[high] >= rank[low]:
            continue  # inside a cycle and disagrees with the deterministic order: dropped
        kept.append((low, high))
    return kept


def _neg(text: str) -> tuple:
    # sort key for "descending string" without reversing the whole sort
    return tuple(-ord(ch) for ch in text) + (1,)


def enforce(scored: list, edges: Iterable[tuple[str, str]], eps: float = EPS) -> bool:
    """Mutates `scored` [(score, row)] in place so every kept edge holds (low ranks strictly below
    high). Returns whether any score changed. The caller re-sorts."""
    kept = consistent_edges(scored, edges)
    if not kept:
        return False
    pos = {str(_field(row, "id")): i for i, (_s, row) in enumerate(scored)}
    highs: dict[str, list[str]] = {}
    lows: dict[str, list[str]] = {}
    indeg: dict[str, int] = {}
    for low, high in kept:
        highs.setdefault(low, []).append(high)
        lows.setdefault(high, []).append(low)
        indeg[low] = indeg.get(low, 0) + 1
        indeg.setdefault(high, indeg.get(high, 0))
    ready = sorted(n for n, d in indeg.items() if d == 0)
    changed = False
    done = 0
    while ready:
        node = ready.pop(0)
        done += 1
        if node in highs:
            cap = min(scored[pos[h]][0] for h in highs[node]) - eps
            score, row = scored[pos[node]]
            if score > cap:
                scored[pos[node]] = (cap, row)
                changed = True
        for low in lows.get(node, []):
            indeg[low] -= 1
            if indeg[low] == 0:
                ready.append(low)
        ready.sort()
    return changed


def violations(scored: list, edges: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Kept edges whose low ranks at or above its high in the (score, updated_at) desc order."""
    order = sorted(scored, key=lambda item: (item[0], str(_field(item[1], "updated_at"))), reverse=True)
    place = {str(_field(row, "id")): i for i, (_s, row) in enumerate(order)}
    return [(low, high) for low, high in consistent_edges(scored, edges) if place[low] < place[high]]


def confidence_edges(scored: list, pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """(older, newer) pairs that become edges: the newer's confidence >= the older's (shipped rule)."""
    rows = {str(_field(row, "id")): row for _s, row in scored}
    out = []
    for old, new in pairs:
        a, b = rows.get(str(old)), rows.get(str(new))
        if a is not None and b is not None and float(_field(b, "confidence", 0.0)) >= float(_field(a, "confidence", 0.0)):
            out.append((str(old), str(new)))
    return out


def stale_edges(newer: dict) -> list[tuple[str, str]]:
    return [(str(old), str(n)) for old, news in (newer or {}).items() for n in news]
