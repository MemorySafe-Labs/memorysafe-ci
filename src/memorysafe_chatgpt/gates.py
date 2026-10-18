"""Opt-in memory "gates": a capacity gate, a surprise write gate and an offline
consolidation pass. PROTOTYPE -- every gate is OFF unless its environment flag is set.

This is not the research continual-learning stack. There is no MVI, no ProtectScore, no
embeddings, no learned weights and no network call. Everything below is lexical
(content-word overlap, the slot extractor and the numeric-template check that
`storage.py` already uses) and every threshold is a hand-set constant that can be
overridden by an environment variable. Whether any of it is better than the default
eviction is something `tests/bench/gates_toy_benchmark.py` measures on a toy stream;
it is design evidence, not a claim about real memories.

Flags (all default off; truthy = 1/true/yes/on):

  MEMORYSAFE_GATE_CAPACITY     rank by keep-priority instead of the default eviction order
  MEMORYSAFE_GATE_SURPRISE     score each write for novelty against the active memories
  MEMORYSAFE_GATE_CONSOLIDATE  allow `memorysafe consolidate`
  MEMORYSAFE_GATE_MAX_ACTIVE   capacity for the gates; falls back to MEMORYSAFE_MAX_ACTIVE
  MEMORYSAFE_GATE_SURPRISE_PROTECT=0  surprise gate raises importance but does not protect
  (round 13) The round-4 sidecar file `<database>.gates-index` is GONE: nothing is ever written next
                               to the database, MEMORYSAFE_GATE_PERSIST no longer exists, and opening a
                               store deletes any such file an older prototype left behind (`remove_sidecar`).
  MEMORYSAFE_GATE_MERGE_PRUNE_DAYS=<n> age window (days) used by `prune_merge_copies` when not passed
                               explicitly; unset means prune will not delete by age alone
  MEMORYSAFE_GATE_MERGE_PRUNE_KEEP=<n> keep at most this many newest merge copies when pruning;
                               unset means no count cap. With both unset, prune plans nothing (safe default)
  MEMORYSAFE_GATE_AGE_GRACE=<n>       optional fade of the rarity bonus of never-recalled
                               memories after n x (active count) newer writes (default off)

Tuning (optional): MEMORYSAFE_GATE_SURPRISE_LOW_SIM (0.70), _MERGE_SIM (0.85),
_HIGH_NOVELTY (0.80), _MIN_ACTIVE (10), MEMORYSAFE_GATE_PROTECT_SHARE (0.20).

Nothing here deletes a row on the write path. A memory that loses its place to the
capacity gate moves to state `evicted`, the state the existing capacity bound already uses,
so `restore` brings it back. Every decision is written to `decision_events` with a
plain-language reason.

The surprise gate's MERGE is reversible (round 4/5): the merged-away write is kept as a
hidden row in state `merge_copy` (distinct from capacity-evicted), linked to the kept
memory; `restore <that id>` brings it back and puts the kept memory back as it was
(see "reversible surprise merge" below). `prune_merge_copies` can later remove old copies
(dry-run by default; env window; restore refuses after prune).
"""

from __future__ import annotations

import heapq
import json
import os
import sqlite3
import threading
import time
from bisect import bisect_left, insort
from collections import Counter, OrderedDict, deque
from functools import lru_cache
from operator import itemgetter
from typing import Any

_TRUE = frozenset({"1", "true", "yes", "on"})

# Categories whose loss is costly out of proportion to their number. They are kept
# before any other unprotected memory, and the surprise gate never merges them.
CRITICAL_CATEGORIES = frozenset({"safety", "decision"})

# Two memories this close (Jaccard over content words) count as "the same kind of
# thing" when measuring how common a memory is.
CLUSTER_SIM = 0.5
_SIM_Q = 2  # CLUSTER_SIM as a fraction _SIM_P / _SIM_Q, for exact integer comparisons
_SIM_P = 1

# A memory is never counted as having more than CLUSTER_CAP - 1 near neighbours (its "cluster",
# itself included, tops out at CLUSTER_CAP). Rarity is 1 / cluster, so past this point the
# rarity term is already under 0.3 / CLUSTER_CAP in the score and a bigger count changes
# nothing that matters; what it buys is speed: counting stops at the cap, which is what
# made a dense routine cluster cost O(cluster) per memory. Round 3.
CLUSTER_CAP = 64

# How much deeper than the cap a neighbour count looks when an earlier capped count had to be
# redone (round 4): the surplus lets later departures from a big cluster be absorbed without
# another recount. Only the capped number is ever used, so this changes cost, never a score.
CLUSTER_SLACK = 32

# Categories a person states on purpose and comes back to. They earn a category bonus and
# a larger share of the rarity bonus; "task" earns a small one and "other" none.
DURABLE_CATEGORIES = frozenset({"preference", "personal", "project"})

# A memory recalled this many times is treated as used: it ranks above every memory that
# has not been, whatever their rarity. Rarity alone never makes a one-off outrank it.
REPEAT_RECALLS = 2

# Keep-priority weights; they sum to 1.0. Rarity is the largest single weight but is
# scaled by how much evidence of use a memory has (see `_gate`), so a rare
# throwaway remark does not outrank a repeated, genuinely useful fact.
PROTECTED_TIER = 2

W_RARITY, W_IMPORTANCE, W_RECALL, W_RECENCY, W_CATEGORY = 0.30, 0.20, 0.20, 0.15, 0.15

# Share of the rarity bonus a memory with no evidence of use (never recalled, a non-durable
# category) still gets. Too low and a rare fact that has not been asked for yet loses to a
# pile of newer routine entries; too high and a rare throwaway is as safe as a rare fact.
GATE_FLOOR = 0.5

# Optional age fade (MEMORYSAFE_GATE_AGE_GRACE=<multiplier>, default 0 = off): a rare memory
# nobody has asked for loses its rarity bonus as newer memories pile up behind it -- full
# bonus while fewer than <multiplier> x (active count) memories have been stored after it,
# none once twice that many have. A recalled memory keeps its bonus. Off by default
# because it trades the prototype's original promise (a rare fact outlives routine
# entries) for dropping throwaway remarks, and the benchmark's "late recall" setting shows
# what it costs a useful fact whose first recall comes after the grace period.


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def fast_enabled() -> bool:
    """Round 11 (opt-in): exact-equivalent speed-ups of the write and read paths. Same decisions,
    less work: in-process caches keyed by text (nothing persisted) and bounded similarity checks."""
    return _flag("MEMORYSAFE_GATE_FAST")


def capacity_gate_enabled() -> bool:
    return _flag("MEMORYSAFE_GATE_CAPACITY")


def surprise_gate_enabled() -> bool:
    return _flag("MEMORYSAFE_GATE_SURPRISE")


def consolidate_enabled() -> bool:
    return _flag("MEMORYSAFE_GATE_CONSOLIDATE")


def surprise_protect_enabled() -> bool:
    """High surprise protects a memory unless MEMORYSAFE_GATE_SURPRISE_PROTECT is set to
    0/false/no/off (importance is still raised). Protection by novelty alone also protects
    novel throwaway remarks; the benchmark shows that cost."""

    return os.environ.get("MEMORYSAFE_GATE_SURPRISE_PROTECT", "").strip().lower() not in {"0", "false", "no", "off"}


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def gate_capacity() -> int | None:
    """Capacity the gates enforce. None means unbounded (the gates then only score).

    `MEMORYSAFE_GATE_MAX_ACTIVE=0` (or any value that is not a positive integer) means the gates
    enforce no capacity of their own; storage then falls back to the shipped MEMORYSAFE_MAX_ACTIVE
    eviction, so switching the capacity gate on can never switch that bound off. storage never asks
    on behalf of a store whose backend wants rank-only gates (`gate_flags.rank_only`)."""

    from . import storage

    raw = os.environ.get("MEMORYSAFE_GATE_MAX_ACTIVE", "").strip()
    if raw:
        try:
            return int(raw) if int(raw) > 0 else None
        except ValueError:
            return None
    return storage._max_active()


# --- lexical helpers -----------------------------------------------------------------


class _TermMap(dict):
    """token -> its stem, or None for a token that is not a content word (stopword, single
    character, anything with a digit). A vocabulary is small next to the memories that use
    it, so each distinct word is stemmed once per process instead of once per memory --
    stemming was most of the cost of building the index on a cold start."""

    def __missing__(self, token: str) -> str | None:
        from . import storage

        if len(self) > 200_000:
            self.clear()
        value = None
        if token not in storage._STOPWORDS and len(token) > 1 and not any(c.isdigit() for c in token):
            value = storage._stem(token)
        self[token] = value
        return value


_TERM_MAP = _TermMap()


class _TemplateMap(dict):
    """token -> the token as `storage._template` writes it ('#' for anything with a digit)."""

    def __missing__(self, token: str) -> str:
        from . import storage

        if len(self) > 200_000:
            self.clear()
        value = "#" if storage._NUMBERISH.search(token) else token
        self[token] = value
        return value


_TEMPLATE_MAP = _TemplateMap()


@lru_cache(maxsize=65536)
def terms_of(normalized: str) -> frozenset[str]:
    """Content words of a normalised memory, lightly stemmed, with numbers left out.

    Numbers are left out on purpose: "item 7, room 7" and "item 27, room 27" are the
    same kind of entry, and whether their numbers make them different facts is decided
    separately (storage._number_tokens), as the near-duplicate merge already does.
    """

    return frozenset(map(_TERM_MAP.__getitem__, normalized.split())).difference((None,))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = len(left | right)
    return len(left & right) / union if union else 0.0



# --- active-memory index ----------------------------------------------------------------

# Row layout used inside this module (plain tuples are fastest to loop over):
#   (rowid, id, normalized_content, category, importance, protected, recall_count)


def _tuples(rows: list[Any]) -> list[tuple]:
    if rows and type(rows[0]) is tuple:
        return rows
    return [
        (r["rowid"], str(r["id"]), r["normalized_content"], r["category"], float(r["importance"]),
         1 if r["protected"] else 0, int(r["recall_count"] or 0))
        for r in rows
    ]


class _Lazy(dict):
    """A dict that fills a missing entry from the owning index's text for that id."""

    __slots__ = ("owner", "make")

    def __init__(self, owner: "ActiveIndex", make: Any) -> None:
        super().__init__()
        self.owner = owner
        self.make = make

    def __missing__(self, memory_id: str) -> Any:
        value = self.make(self.owner.norm[memory_id])  # KeyError for an id the index does not hold
        self[memory_id] = value
        return value



# Bit packs of content-word sets (round 5). The neighbour-count hot path was spending most of
# its time on frozenset intersections over large candidate sets of rare memories. Mapping each
# word to a bit position once and testing Jaccard with integer & / bit_count is exact (same
# inequality) and much cheaper. The map only grows; dropped words leave unused bits.
_WORD_BIT: dict[str, int] = {}
_WORD_BIT_NEXT = 0


def _terms_bit(terms: frozenset[str]) -> tuple[int, int]:
    """(bit pack, bit_count) for a content-word set. Word indices are process-global and stable."""

    global _WORD_BIT_NEXT
    bit = 0
    index = _WORD_BIT
    for word in terms:
        position = index.get(word)
        if position is None:
            position = _WORD_BIT_NEXT
            _WORD_BIT_NEXT = position + 1
            index[word] = position
        bit |= 1 << position
    return bit, bit.bit_count()


def _terms_of_text(text: str) -> frozenset[str]:
    return frozenset(map(_TERM_MAP.__getitem__, text.split())).difference((None,))


def _template_of_text(text: str) -> str:
    return " ".join(map(_TEMPLATE_MAP.__getitem__, text.split()))  # == storage._template(text)


class ActiveIndex:
    """Content-word index of the active memories, kept up to date by DIFFING against the
    rows just read from the database, so it is correct however the rows changed (another
    process, restore, forget, merge) and never trusted blindly.

    What it saves: the first version re-tokenised every active memory and re-counted every
    memory's near neighbours on every over-capacity write. Here a write only adds or drops
    the entries that actually changed (and when nothing changed the check is one dict
    comparison). A neighbour count is worked out for a memory only when the ranking needs
    it (see `select_lowest`), stops at CLUSTER_CAP, and is then remembered (`memo`). It is
    NOT adjusted eagerly when memories come and go -- that needed the full neighbour list
    of every added and dropped memory, the dearest step of a write in a dense cluster.
    Instead each change is logged (`log`, with its words) and a remembered count is brought
    up to date, when it is next asked for, by testing only the changes made since it was
    taken; one older than the log (or a full count that lost a neighbour) is recounted."""

    LOG_KEEP = 512

    def __init__(self, path: str | None = None) -> None:
        self.norm: dict[str, str] = {}
        # terms / template of a memory are worked out from its text the first time they are
        # asked for, so an index restored from the sidecar file (see `load`) does not
        # re-tokenise rows it already knows.
        self.terms: dict[str, frozenset[str]] = _Lazy(self, _terms_of_text)
        self.template: dict[str, str] = _Lazy(self, _template_of_text)
        self._postings: dict[str, set[str]] | None = {}  # None = not built yet (see `postings`)
        self._bits: dict[str, int] | None = {}  # None = not built yet (see `_ensure_bits`); bit pack per memory
        self._bit_n: dict[str, int] = {}  # cached bit_count of each pack (avoids re-counting in the inner loop)
        self.templates: Counter[str] = Counter()
        self.memo: dict[str, int] = {}
        self.memo_gen: dict[str, int] = {}
        self.floor: dict[str, int] = {}  # memory -> lower bound on its true count, for memos at the cap
        self.gen = 0
        self.log: deque[tuple[int, int, frozenset[str]]] = deque()
        self.book: "Book | None" = None  # snapshot of the active rows the capacity gate keeps between writes
        self.path = path  # sidecar file for `load` / `save`; None = never persisted
        self.restored = False  # True when the index started from the sidecar file
        self.no_save = False  # a dry-run consolidation ran and nothing has written since: leave the disk alone
        self.unsaved = 0  # changes since the sidecar file was read or written
        self.recounted = 0  # neighbour counts worked out from scratch since then (the file lacked them)

    @property
    def postings(self) -> dict[str, set[str]]:
        """word -> ids of the memories that use it. Built on first use when the index was
        restored from the sidecar file (most cold writes never need it: every neighbour count
        they ask for is already remembered)."""

        postings = self._postings
        if postings is None:
            postings = {}
            for memory_id in self.norm:
                for word in self.terms[memory_id]:
                    bucket = postings.get(word)
                    if bucket is None:
                        postings[word] = {memory_id}
                    else:
                        bucket.add(memory_id)
            self._postings = postings
        return postings

    def _record(self, sign: int, terms: frozenset[str]) -> None:
        self.gen += 1
        self.unsaved += 1
        log = self.log
        log.append((self.gen, sign, terms))
        if len(log) > self.LOG_KEEP:
            log.popleft()

    def sync(self, rows: list[Any]) -> None:
        self.book = None  # a snapshot is only valid for the rows it was built from
        norm = self.norm
        current = dict(map(itemgetter(1, 2), _tuples(rows)))
        if current == norm:
            return
        for memory_id in norm.keys() - current.keys():
            self._drop(memory_id)
        for memory_id in [i for i, text in norm.items() if current[i] != text]:  # reworded in place
            self._drop(memory_id)
        for memory_id in current.keys() - norm.keys():
            self._add(memory_id, current[memory_id])

    def _add(self, memory_id: str, text: str) -> None:
        tokens = text.split()
        terms = frozenset(map(_TERM_MAP.__getitem__, tokens)).difference((None,))
        template = " ".join(map(_TEMPLATE_MAP.__getitem__, tokens))  # == storage._template(text)
        self.norm[memory_id] = text
        self.terms[memory_id] = terms
        self.template[memory_id] = template
        self.templates[template] += 1
        postings = self._postings
        if postings is not None:
            for word in terms:
                bucket = postings.get(word)
                if bucket is None:
                    postings[word] = {memory_id}
                else:
                    bucket.add(memory_id)
        bits = self._bits
        if bits is not None:
            pack, width = _terms_bit(terms)
            bits[memory_id] = pack
            self._bit_n[memory_id] = width
        self._record(1, terms)

    def apply(self, added: list[tuple], removed: list[str]) -> None:
        """Follow a known change (rows just read, ids just moved out) without diffing every row."""

        for memory_id in removed:
            if memory_id in self.norm:
                self._drop(memory_id)
        for row in added:
            if row[1] in self.norm:
                self._drop(row[1])
            self._add(row[1], row[2])

    def _drop(self, memory_id: str) -> None:
        self.memo.pop(memory_id, None)
        self.memo_gen.pop(memory_id, None)
        self.floor.pop(memory_id, None)
        bits = self._bits
        if bits is not None:
            bits.pop(memory_id, None)
        self._bit_n.pop(memory_id, None)
        terms = self.terms[memory_id]  # worked out from the text if it was never asked for
        template = self.template[memory_id]
        del self.terms[memory_id]
        del self.template[memory_id]
        del self.norm[memory_id]
        postings = self._postings
        if postings is not None:
            for word in terms:
                bucket = postings[word]
                bucket.discard(memory_id)
                if not bucket:
                    del postings[word]
        self.templates[template] -= 1
        if not self.templates[template]:
            del self.templates[template]
        self._record(-1, terms)

    def _near(self, memory_id: str, minimum: float = CLUSTER_SIM) -> list[str]:
        """The OTHER memories with content-word Jaccard >= `minimum` to this one.

        Exact. A pair with Jaccard >= 0.5 must share at least ceil(n/2) of this memory's
        n words, so it cannot miss all of any floor(n/2)+1 of them; only the postings of the
        rarest floor(n/2)+1 words are visited, not those of every word."""

        terms = self.terms[memory_id]
        size = len(terms)
        if not size:
            return []
        postings = self.postings
        probe = sorted(terms, key=lambda word: len(postings[word]))[: size // 2 + 1]
        candidates: set[str] = set()
        for word in probe:
            candidates |= postings[word]
        candidates.discard(memory_id)
        all_terms = self.terms
        near = []
        for other in candidates:
            theirs = all_terms[other]
            overlap = len(terms & theirs)
            if overlap and overlap / (size + len(theirs) - overlap) >= minimum:
                near.append(other)
        return near

    def _ensure_bits(self) -> dict[str, int]:
        """Bit packs for every active memory. Built on first use when the index was restored
        from the sidecar (terms are filled lazily as packs are made)."""

        bits = self._bits
        if bits is None:
            bits = {}
            widths = self._bit_n
            for memory_id in self.norm:
                pack, width = _terms_bit(self.terms[memory_id])
                bits[memory_id] = pack
                widths[memory_id] = width
            self._bits = bits
        return bits

    def _near_count(self, memory_id: str, limit: int) -> int:
        """`len(_near(memory_id))`, but counting stops at `limit` (same candidate set, same
        test, written with integers: overlap / union >= p / q  <=>  (p + q) * overlap >= p * (size + other)).

        Round 5: the Jaccard test uses bit packs (`&` / `bit_count`) instead of frozenset
        intersections. Same candidates, same inequality, same answer; cheaper on the cold
        no-file path where many rare memories each probe hundreds of non-neighbours."""

        terms = self.terms[memory_id]
        size = len(terms)
        if not size or limit <= 0:
            return 0
        postings = self.postings
        bits = self._ensure_bits()
        widths = self._bit_n
        mine = bits[memory_id]
        probe = sorted(terms, key=lambda word: len(postings[word]))[: size // 2 + 1]
        scale, want = _SIM_P + _SIM_Q, _SIM_P
        # The memory itself passes the test (it is in the first word's postings), so count
        # `limit + 1` and subtract it. The first word's postings are tried on their own: in a
        # dense cluster they reach the limit and the rest is never looked at.
        first = postings[probe[0]]
        stop = limit + 1
        count = 0
        for other in first:
            other_bit = bits[other]
            overlap = (mine & other_bit).bit_count()
            if overlap and scale * overlap >= want * (size + widths[other]):
                count += 1
                if count >= stop:
                    return limit
        if len(probe) > 1:
            rest = set().union(*[postings[word] for word in probe[1:]])
            rest -= first
            for other in rest:
                other_bit = bits[other]
                overlap = (mine & other_bit).bit_count()
                if overlap and scale * overlap >= want * (size + widths[other]):
                    count += 1
                    if count >= stop:
                        return limit
        return count - 1

    def fill_memo(self) -> None:
        """Neighbour counts for every memory that has none yet. Used when the whole store is
        being ranked (consolidation)."""

        for memory_id in list(self.norm):
            if memory_id not in self.memo:
                self.neighbours(memory_id)

    def neighbours(self, memory_id: str, minimum: float = CLUSTER_SIM) -> int:
        """Number of near neighbours (content-word Jaccard >= CLUSTER_SIM), at most
        CLUSTER_CAP - 1. Any other `minimum` is counted afresh and uncapped.

        `memo` holds that capped number. A memory whose count reached the cap also has a
        `floor`: a lower bound on its true count (>= the cap), kept up to date by the change
        log (+1 for a neighbour that arrived, -1 for one that left). While the floor is still
        at or above the cap the capped number is exactly right, so a neighbour leaving a big
        cluster no longer forces a recount; only a floor that has run below the cap does, and
        that recount looks CLUSTER_SLACK deeper so the next several departures are absorbed
        too (round 4)."""

        if minimum != CLUSTER_SIM:
            return len(self._near(memory_id, minimum))
        limit = CLUSTER_CAP - 1
        count = self.memo.get(memory_id)
        deeper = False
        if count is not None:
            taken = self.memo_gen[memory_id]
            if taken == self.gen:
                return count
            log = self.log
            if log and log[0][0] <= taken + 1:  # every change since `taken` is still in the log
                floor = self.floor.get(memory_id)
                value = count if floor is None else floor
                terms = self.terms[memory_id]
                size = len(terms)
                for stamp, sign, theirs in reversed(log):
                    if stamp <= taken:
                        break
                    overlap = len(terms & theirs)
                    if overlap and overlap / (size + len(theirs) - overlap) >= CLUSTER_SIM:
                        value += sign
                if value >= limit:
                    # exact below the cap before this call, or a lower bound that is still at the cap
                    self.memo[memory_id] = limit
                    self.floor[memory_id] = min(value, limit + CLUSTER_SLACK)
                    self.memo_gen[memory_id] = self.gen
                    return limit
                if floor is None:  # an exact count below the cap, corrected by the log
                    self.memo[memory_id] = value
                    self.memo_gen[memory_id] = self.gen
                    return value
                deeper = True  # the floor ran below the cap: the true count is unknown
            else:
                deeper = memory_id in self.floor
        counted = self._near_count(memory_id, limit + CLUSTER_SLACK if deeper else limit)
        self.memo[memory_id] = min(counted, limit)
        if counted >= limit:
            self.floor[memory_id] = counted
        else:
            self.floor.pop(memory_id, None)
        self.memo_gen[memory_id] = self.gen
        self.recounted += 1
        return self.memo[memory_id]

    # --- no sidecar file (round 13) -----------------------------------------------------------
    #
    # Rounds 4-12 could save this index (normalised memory TEXT included) to `<database>.gates-index`
    # and read it back in a later process. reviewer A's and reviewer B's round-10 reviews showed that the
    # exit save could write the text of a memory forgotten in the same process. Round 13 removes
    # persistence: the index lives in this process's memory only, is rebuilt from the database when
    # a process needs it, and `remove_sidecar` deletes any file an older prototype left behind.

    def load(self) -> bool:
        """Kept for callers of the round-4 API; never reads anything (round 13)."""
        return False

    def prefetch(self, count: int = None) -> int:
        return 0

    def should_save(self) -> bool:
        return False

    def save(self) -> bool:
        """Kept for callers of the round-4 API; never writes anything (round 13)."""
        return False


def _persist_enabled() -> bool:
    """Round 13: persistence was removed; always False (MEMORYSAFE_GATE_PERSIST is ignored)."""

    return False


def cache_file(store: Any) -> str | None:
    """Where rounds 4-12 put the sidecar file (only used to delete it)."""
    database = getattr(store, "database_path", None)
    return f"{database}.gates-index" if database else None


def remove_sidecar(database_path: Any) -> int:
    """See gate_flags.remove_sidecar (round 13 upgrade step)."""
    from .gate_flags import remove_sidecar as _remove

    return _remove(database_path)


def forget_in_process(store: Any, memory_ids: list[str]) -> None:
    """Round 13: after forget/delete, drop what this process's gate caches hold about those
    memories (the index's normalised text and word data, the usefulness gate's features), so no
    cache of this process keeps the text of a forgotten memory. Other processes drop it at their
    next gated call: their snapshot follows the database change log (see `changes_since`). The
    text-keyed lru caches cannot be emptied per id, so they are cleared."""

    key = _cache_key(store)
    with _INDEX_LOCK:
        index = _INDEXES.get(key)
        if index is not None:
            for memory_id in memory_ids:
                if memory_id in index.norm:
                    index._drop(memory_id)
            index.log.clear()   # the change log holds the dropped memory's words
            index.memo.clear()
            index.memo_gen.clear()
            index.floor.clear()
            index.book = None
    from . import gates_r6

    gates_r6.forget_in_process(store, memory_ids)


_INDEXES: "OrderedDict[str, ActiveIndex]" = OrderedDict()
_INDEX_LOCK = threading.RLock()
_INDEX_KEEP = 4


def _cache_key(store: Any) -> str:
    from .gate_flags import cache_key

    return cache_key(store)


def index_for(store: Any) -> ActiveIndex:
    """The process-wide index for this store's rows (a few are kept), in memory only. Keyed by
    `gate_flags.cache_key`: the database file locally; backend, user and layers on a server backend."""

    key = _cache_key(store)
    with _INDEX_LOCK:
        index = _INDEXES.pop(key, None)
        if index is None:
            index = ActiveIndex(None)
        _INDEXES[key] = index
        while len(_INDEXES) > _INDEX_KEEP:
            _INDEXES.popitem(last=False)
        return index


def save_indexes(force: bool = False, prefetch: bool = True) -> int:
    """Round 13: nothing is ever saved (kept for the round-4 API and tests). Returns 0."""

    return 0


# --- keep-priority ---------------------------------------------------------------------

_CATEGORY_WEIGHT = {**{c: 1.0 for c in CRITICAL_CATEGORIES}, **{c: 0.8 for c in DURABLE_CATEGORIES}, "task": 0.3}


def _base(category: str, importance: float, recalls: int, recency: float) -> float:
    """The part of the keep score that needs no neighbour count. score = base +
    W_RARITY * rarity * gate, so `base` is an exact lower bound on the score and the
    ranking can skip the neighbour count for memories that cannot reach the bottom.
    The same expression is inlined in `select_lowest`'s loop; keep the two identical."""

    return (
        W_IMPORTANCE * importance + W_RECALL * (recalls if recalls < 5 else 5) / 5
        + W_CATEGORY * _CATEGORY_WEIGHT.get(category, 0.0)
    ) + W_RECENCY * recency


def _gate(category: str, recalls: int, age: float, grace: float) -> float:
    """Share of the rarity bonus a memory has earned: half with no evidence of use, all of
    it once recalled about three times or filed under a durable category and recalled."""

    evidence = min(1.0, recalls / 3 + 0.5 * _CATEGORY_WEIGHT.get(category, 0.0))
    gate = GATE_FLOOR + (1.0 - GATE_FLOOR) * evidence
    if recalls == 0 and grace and age > grace:
        gate *= max(0.0, 1.0 - (age - grace) / grace)
    return gate


def _tier(category: str, protected: int, recalls: int) -> int:
    if protected:
        return PROTECTED_TIER
    return 1 if (category in CRITICAL_CATEGORIES or recalls >= REPEAT_RECALLS) else 0


def _score(row: tuple, recency: float, age: float, grace: float, index: ActiveIndex) -> float:
    """`_finish(...)["score"]` without building the result (the arithmetic is the same, in
    the same order). Used to compare candidates; only the ones chosen are finished."""

    memory_id, category, importance, recalls = row[1], row[3], row[4], row[6]
    siblings = index.templates[index.template[memory_id]] - 1
    if siblings + 1 >= CLUSTER_CAP:
        cluster = CLUSTER_CAP
    else:
        cluster = min(CLUSTER_CAP, 1 + max(index.neighbours(memory_id), siblings))
    weight = _CATEGORY_WEIGHT.get(category, 0.0)
    evidence = recalls / 3 + 0.5 * weight
    if evidence > 1.0:
        evidence = 1.0
    gate = GATE_FLOOR + (1.0 - GATE_FLOOR) * evidence
    if recalls == 0 and grace and age > grace:
        gate *= max(0.0, 1.0 - (age - grace) / grace)
    base = (
        W_IMPORTANCE * importance + W_RECALL * (recalls if recalls < 5 else 5) / 5 + W_CATEGORY * weight
    ) + W_RECENCY * recency
    return round(base + W_RARITY * (1.0 / cluster) * gate, 4)


def _finish(row: tuple, recency: float, age: float, grace: float, index: ActiveIndex) -> dict[str, Any]:
    rowid, memory_id, _norm, category, importance, protected, recalls = row
    siblings = index.templates[index.template[memory_id]] - 1
    if siblings + 1 >= CLUSTER_CAP:
        cluster = CLUSTER_CAP  # no need to count word neighbours: the cap is already reached
    else:
        cluster = min(CLUSTER_CAP, 1 + max(index.neighbours(memory_id), siblings))
    rarity = 1.0 / cluster
    score = _base(category, importance, recalls, recency) + W_RARITY * rarity * _gate(category, recalls, age, grace)
    return {
        "id": memory_id, "rowid": rowid, "tier": _tier(category, protected, recalls), "score": round(score, 4),
        "rarity": round(rarity, 3), "similar": cluster - 1, "importance": importance,
        "recalls": recalls, "category": category, "terms": index.terms[memory_id],
    }


def _by_age(rows: list[tuple]) -> list[tuple]:
    """`rows` oldest first (by rowid). Rows read with ORDER BY rowid already are, and the
    check is one C-speed pass, so the usual case costs no sort."""

    keys = list(map(itemgetter(0), rows))
    if keys == sorted(keys):
        return rows
    return sorted(rows, key=itemgetter(0))


def rank_rows(rows: list[Any], index: ActiveIndex | None = None) -> list[dict[str, Any]]:
    """Score active memories for keeping. Lowest keep-priority first.

    `rows` need: id, rowid, normalized_content, category, importance, protected,
    recall_count. Order is (tier, score, arrival), lowest keep-priority first. `tier` is
    the keep tier: 2 protected; 1 critical category or recalled at least REPEAT_RECALLS
    times; 0 everything else. A higher tier always outranks a lower one, so a repeated,
    used memory is never ranked below a never-recalled one-off.
    score = base + 0.30 * rarity * gate: importance 0.20, recall count 0.20, recency 0.15,
    category 0.15, and rarity 0.30 scaled by how much evidence of use the memory has
    (recalled, durable category) -- a never-recalled one-off in a non-durable category
    earns half of it, and, only if MEMORYSAFE_GATE_AGE_GRACE is set, loses it as newer
    memories pile up behind it.
    """

    from . import storage

    rows = _tuples(rows)
    if not rows:
        return []
    if index is None:
        index = ActiveIndex()
    index.sync(rows)
    index.fill_memo()
    total = len(rows)
    rows = _by_age(rows)
    newest = rows[-1][0]
    grace = _number("MEMORYSAFE_GATE_AGE_GRACE", 0.0) * total
    ranked = []
    for rank, row in enumerate(rows):
        item = _finish(row, (rank + 1) / total, newest - row[0], grace, index)
        item["numbers"] = storage._number_tokens(row[2])
        ranked.append(item)
    ranked.sort(key=lambda item: (item["tier"], item["score"], item["rowid"]))
    return ranked


def _entry_key(entry: tuple) -> tuple:
    return entry[0], entry[1]


class Book:
    """The active rows, grouped so that the lowest keep-priority ones can be found without
    visiting every row.

    Rows with the same category, importance, capped recall count and protected flag have the
    same score except for recency, and recency only grows with age, so each group is a list
    ordered by age whose FIRST unvisited member is its lowest bound. Selecting the lowest
    `count` memories then walks a heap of at most a few dozen group heads instead of every
    row. The book is exact, not an estimate: `select` returns what `rank_rows` would put
    first (a test compares them on random stores and after random changes).

    It also carries running sums over its rows (`fingerprint`) so the capacity gate can check
    in one aggregate query that the database still holds exactly these rows."""

    __slots__ = ("rows", "rowids", "groups", "sums", "by_rowid", "seq")

    def __init__(self, rows: list[tuple]) -> None:
        self.rows: dict[str, tuple] = {}
        self.rowids: list[int] = []
        self.groups: dict[tuple, list[tuple]] = {}
        self.sums = [0, 0, 0, 0, 0]  # rowid, recalls, protected, importance (x1000), text length
        self.by_rowid: dict[int, str] = {}
        self.seq = 0  # round 13: position in the database change log this book reflects
        # Round 13 (cold first write): one bulk pass instead of add() per row; same result.
        rows = _by_age(rows)
        self.rows = {row[1]: row for row in rows}
        if len(self.rows) != len(rows):  # duplicate ids cannot come from the table; keep add()'s meaning
            self.rows, self.rowids = {}, []
            for row in rows:
                self.add(row)
            return
        self.rowids = [row[0] for row in rows]
        self.by_rowid = {row[0]: row[1] for row in rows}
        groups = self.groups
        key = self._key
        for row in rows:
            k = key(row)
            members = groups.get(k)
            if members is None:
                groups[k] = [row]
            else:
                members.append(row)
        self.sums = [sum(self.rowids), sum(row[6] for row in rows), sum(row[5] for row in rows),
                     sum(int(row[4] * 1000 + 0.5) for row in rows), sum(len(row[2]) for row in rows)]

    @staticmethod
    def _key(row: tuple) -> tuple:
        return (row[3], row[4], row[6] if row[6] < 5 else 5, row[5])

    def add(self, row: tuple) -> None:
        self.rows[row[1]] = row
        self.by_rowid[row[0]] = row[1]
        rowids = self.rowids
        if not rowids or row[0] > rowids[-1]:
            rowids.append(row[0])
            self.groups.setdefault(self._key(row), []).append(row)
        else:
            insort(rowids, row[0])
            insort(self.groups.setdefault(self._key(row), []), row)
        sums = self.sums
        sums[0] += row[0]
        sums[1] += row[6]
        sums[2] += row[5]
        sums[3] += int(row[4] * 1000 + 0.5)
        sums[4] += len(row[2])

    def remove(self, memory_id: str) -> None:
        row = self.rows.pop(memory_id)
        self.by_rowid.pop(row[0], None)
        del self.rowids[bisect_left(self.rowids, row[0])]
        key = self._key(row)
        members = self.groups[key]
        members.remove(row)
        if not members:
            del self.groups[key]
        sums = self.sums
        sums[0] -= row[0]
        sums[1] -= row[6]
        sums[2] -= row[5]
        sums[3] -= int(row[4] * 1000 + 0.5)
        sums[4] -= len(row[2])

    def fingerprint(self, extra: list[tuple] = ()) -> tuple:
        """(count, sum of rowids, newest rowid, recalls, protected, importance, text length) of
        these rows plus `extra` rows that are newer than all of them."""

        count, newest = len(self.rows), (self.rowids[-1] if self.rowids else 0)
        total = list(self.sums)
        for row in extra:
            count += 1
            newest = max(newest, row[0])
            total[0] += row[0]
            total[1] += row[6]
            total[2] += row[5]
            total[3] += int(row[4] * 1000 + 0.5)
            total[4] += len(row[2])
        return (count, total[0], newest, total[1], total[2], total[3], total[4])

    def select(self, count: int, index: ActiveIndex, exclude_id: str | None = None,
               excluded: frozenset = frozenset()) -> list[dict[str, Any]]:
        """The `count` lowest keep-priority unprotected memories, in the order `rank_rows`
        would put them, without ranking all of them.

        A group head is visited in order of a lower bound on its score: the score without
        the rarity term, plus the least rarity can add (rarity is at least 1 / CLUSTER_CAP,
        and the share of it a memory has earned is at least what its category gives). The
        exact score, which needs a neighbour count, is worked out only until the next head's
        bound is above the `count`-th best exact score."""

        total = len(self.rowids)
        if not total or count <= 0:
            return []
        rowids, newest = self.rowids, self.rowids[-1]
        grace = _number("MEMORYSAFE_GATE_AGE_GRACE", 0.0) * total
        weights = _CATEGORY_WEIGHT
        w_imp, w_rec, w_cat, w_new = W_IMPORTANCE, W_RECALL, W_CATEGORY, W_RECENCY
        rarity_floor = W_RARITY / CLUSTER_CAP if not grace else 0.0
        offset: dict[tuple, float] = {}
        heaps: tuple[list, list] = ([], [])
        for key, members in self.groups.items():
            category, importance, recalls, protected = key
            if protected:
                continue
            w = weights.get(category, 0.0)
            offset[key] = (
                (w_imp * importance + w_rec * recalls / 5 + w_cat * w)
                + (rarity_floor * (GATE_FLOOR + (1.0 - GATE_FLOOR) * 0.5 * w) - 1e-9 if rarity_floor else 0.0)
            )
            tier = 1 if (recalls >= REPEAT_RECALLS or category in CRITICAL_CATEGORIES) else 0
            head = members[0][0]
            heaps[tier].append((offset[key] + w_new * ((bisect_left(rowids, head) + 1) / total), head, key, 0))
        chosen: list[dict[str, Any]] = []
        for heap in heaps:
            need = count - len(chosen)
            if need <= 0:
                break
            heapq.heapify(heap)
            best: list[tuple] = []  # (score, rowid, row, recency, age), lowest first
            while heap:
                lower, _rowid, key, position = heap[0]
                if len(best) >= need and round(lower, 4) > best[-1][0]:
                    break
                members = self.groups[key]
                row = members[position]
                if position + 1 < len(members):
                    following = members[position + 1][0]
                    heapq.heapreplace(
                        heap,
                        (offset[key] + w_new * ((bisect_left(rowids, following) + 1) / total), following, key, position + 1),
                    )
                else:
                    heapq.heappop(heap)
                if row[1] == exclude_id or row[1] in excluded:  # round 13: open-conflict sides stay
                    continue
                recency = (bisect_left(rowids, row[0]) + 1) / total
                age = newest - row[0]
                entry = (_score(row, recency, age, grace, index), row[0], row, recency, age)
                if len(best) < need:
                    insort(best, entry, key=_entry_key)
                elif entry[:2] < best[-1][:2]:
                    best.pop()
                    insort(best, entry, key=_entry_key)
            chosen.extend(_finish(row, recency, age, grace, index) for _score_, _id, row, recency, age in best)
        return chosen


def select_lowest(
    rows: list[Any], count: int, index: ActiveIndex, exclude_id: str | None = None
) -> list[dict[str, Any]]:
    """`Book(rows).select(...)` for a list of rows (the index is synced to them first).
    Same answer as ranking everything with `rank_rows` (a test compares them). Neighbour
    counts are remembered in `index` between calls."""

    rows = _tuples(rows)
    index.sync(rows)
    return Book(rows).select(count, index, exclude_id)


def _why_moved(item: dict[str, Any], cap: int, active: int, origin: str) -> str:
    kind = "critical-category" if item["category"] in CRITICAL_CATEGORIES else "ordinary"
    return (
        f"{origin}: {active} active memories exceed the capacity of {cap}. Moved out of recall "
        f"(restorable) as one of the lowest keep-priority unprotected memories: "
        f"{kind} ({item['category']}), resembles {item['similar']} other memor{'y' if item['similar'] == 1 else 'ies'} "
        f"(rarity {item['rarity']:.2f}), importance {item['importance']:.2f}, "
        f"recalled {item['recalls']} time(s), keep score {item['score']:.2f}. "
        "Protected, critical-category and repeatedly recalled memories are kept first. Restore brings it back."
    )


def _is_sqlite(connection: Any) -> bool:
    """The local file (sqlite3) or a server backend. Same test as backends.insert_returning_id."""
    return isinstance(connection, sqlite3.Connection)


def not_indexed(connection: Any) -> str:
    """SQLite's `NOT INDEXED` hint (a full scan beats the state index for "every active row"), or
    nothing on a server backend, which has no such hint and plans the scan itself."""
    return " NOT INDEXED" if _is_sqlite(connection) else ""


def tuple_rows(connection: Any, sql: str, params: Any = ()) -> list[tuple]:
    """Rows as plain tuples. On SQLite through a cursor with no row factory (the ranking loops over
    every active memory, and building sqlite3.Row objects for them costs more than the loop); a
    server backend's rows are turned into tuples."""
    if _is_sqlite(connection):
        cursor = connection.cursor()
        cursor.row_factory = None
        return cursor.execute(sql, params).fetchall()
    return [tuple(row) for row in connection.execute(sql, params).fetchall()]


def _active_rows(connection: Any) -> list[tuple]:
    return tuple_rows(
        connection,
        "SELECT rowid, id, normalized_content, category, importance, protected, "
        "COALESCE(recall_count, 0) AS recall_count "
        f"FROM memories{not_indexed(connection)} WHERE state = 'active' ORDER BY rowid",
    )


def _move_out(store: Any, connection: Any, item: dict[str, Any], reason: str, source: str, stamp: str) -> bool:
    """Move one memory out of recall. The UPDATE re-checks that it is still active and
    unprotected, so a ranking built from rows that changed underneath it cannot move a
    protected memory. Returns whether it moved."""

    changed = connection.execute(
        "UPDATE memories SET state = 'evicted', updated_at = ? WHERE id = ? AND state = 'active' AND protected = 0 "
        # round 13 (reviewer A P1): never move a side of an unresolved conflict, whatever the ranking said
        "AND id NOT IN (SELECT old_memory_id FROM memory_relations WHERE status = 'open' "
        "UNION SELECT new_memory_id FROM memory_relations WHERE status = 'open')",
        (stamp, item["id"]),
    ).rowcount
    if changed:
        store._record_event(connection, "EVICT", item["id"], reason, source=source)
    return bool(changed)


def unresolved_ids(connection: Any) -> frozenset:
    """Round 13 (reviewer A P1): both sides of every open conflict. A confidence review promises both stay
    available until the user resolves it, so no gate moves them (the shipped eviction does the same)."""

    return frozenset(str(r[0]) for r in connection.execute(
        "SELECT old_memory_id FROM memory_relations WHERE status = 'open' "
        "UNION SELECT new_memory_id FROM memory_relations WHERE status = 'open'"))


# --- round 13: database change log (reviewer A P2, cache invalidation) ----------------------------
#
# Rounds 4-12 checked a cached snapshot of the active rows against ONE aggregate (count, sums of
# rowids, recalls, protected, importance, text length). A change that kept those sums -- a category
# edit, an equal-length rewording, offsetting changes, a write by another process -- went unseen.
# Round 13 keeps a change log IN THE DATABASE: three triggers append the rowid of every inserted,
# updated or deleted memory row to `memorysafe_gate_changes` (rowids and sequence numbers only, no
# text), whoever writes -- this process, another assistant, a raw SQL edit. A cached snapshot remembers
# the last sequence number it reflects and re-reads exactly the rows changed since; if the log no longer
# reaches back that far (it keeps the newest LOG_KEEP entries), the snapshot is rebuilt from scratch.
# The table and triggers are created the first time a gate needs a snapshot (flags off: never).

LOG_TABLE = "memorysafe_gate_changes"
LOG_KEEP = 4096
# Round 14 (reviewer A, change-log lifecycle): every trigger carries this version marker in its SQL (SQLite keeps
# the text in sqlite_master), so a database can be checked object by object. See GATES_CHANGELOG_LIFECYCLE.md.
LOG_VERSION = 2
_LOG_MARK = f"/* memorysafe-gate-log v{LOG_VERSION} */"
_LOG_TRIGGERS = (f"{LOG_TABLE}_ins", f"{LOG_TABLE}_upd", f"{LOG_TABLE}_del", f"{LOG_TABLE}_trim")
_LOG_DDL = (
    f"CREATE TABLE IF NOT EXISTS {LOG_TABLE} (seq INTEGER PRIMARY KEY AUTOINCREMENT, row_id INTEGER NOT NULL)",
    f"CREATE TRIGGER IF NOT EXISTS {LOG_TABLE}_ins AFTER INSERT ON memories BEGIN {_LOG_MARK} "
    f"INSERT INTO {LOG_TABLE}(row_id) VALUES (NEW.rowid); END",
    f"CREATE TRIGGER IF NOT EXISTS {LOG_TABLE}_upd AFTER UPDATE ON memories BEGIN {_LOG_MARK} "
    f"INSERT INTO {LOG_TABLE}(row_id) VALUES (NEW.rowid); END",
    f"CREATE TRIGGER IF NOT EXISTS {LOG_TABLE}_del AFTER DELETE ON memories BEGIN {_LOG_MARK} "
    f"INSERT INTO {LOG_TABLE}(row_id) VALUES (OLD.rowid); END",
    f"CREATE TRIGGER IF NOT EXISTS {LOG_TABLE}_trim AFTER INSERT ON {LOG_TABLE} BEGIN {_LOG_MARK} "
    f"DELETE FROM {LOG_TABLE} WHERE seq <= NEW.seq - {LOG_KEEP}; END",
)
_ROW_SQL = ("SELECT rowid, id, normalized_content, category, importance, protected, COALESCE(recall_count, 0), state "
            "FROM memories WHERE rowid IN ({})")
_LOG_COLUMNS = (("seq", "INTEGER", 1), ("row_id", "INTEGER", 0))


def _schema_sql(sql: str) -> str:
    # SQLite omits IF NOT EXISTS from sqlite_master. Compare the complete
    # definitions, including constraints and bodies, not just version comments.
    return " ".join(sql.lower().replace("if not exists ", "").split()).rstrip(";")


def _log_objects(connection: Any) -> dict:
    """{reserved name: (type, sql)} of what occupies the change log's names. Triggers have a namespace of
    their own in SQLite, so for a trigger name only a trigger counts: a user's table or index that happens
    to be called `memorysafe_gate_changes_ins` is not ours, is never touched, and does not stop the log
    from being installed (it used to make every gated write raise). The table name shares its namespace
    with indexes and views, so whatever holds it is reported, and anything but a table is refused."""

    names = (LOG_TABLE, *_LOG_TRIGGERS)
    found: dict = {}
    for name, kind, sql in connection.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE name IN (?, ?, ?, ?, ?)", names):
        name, kind = str(name), str(kind)
        if name != LOG_TABLE and kind != "trigger":
            continue
        found[name] = (kind, str(sql or ""))
    return found


def _valid_log_object(found: dict, name: str, statement: str) -> bool:
    kind = "table" if name == LOG_TABLE else "trigger"
    actual = found.get(name)
    return actual is not None and actual[0] == kind and _schema_sql(actual[1]) == _schema_sql(statement)


def change_log_status(connection: Any) -> dict[str, Any]:
    """Inspect definitions and report validity separately from version labels."""
    import re

    if not _is_sqlite(connection):
        # A server backend's log is part of its migrated schema (see `_server_position`).
        return {"installed": True, "version": LOG_VERSION, "table": False, "table_ok": True,
                "triggers": {}, "rows": 0, "kind": "schema"}
    found = _log_objects(connection)
    expected = dict(zip((LOG_TABLE, *_LOG_TRIGGERS), _LOG_DDL))
    triggers = {}
    for name in _LOG_TRIGGERS:
        kind, sql = found.get(name, ("", ""))
        version = None
        if kind == "trigger":
            match = re.search(r"/\* memorysafe-gate-log v(\d+) \*/", sql)
            version = int(match.group(1)) if match else 1
        triggers[name] = version
    table = found.get(LOG_TABLE, ("", ""))[0] == "table"
    table_ok = _valid_log_object(found, LOG_TABLE, expected[LOG_TABLE])
    versions = [v for v in triggers.values() if v is not None]
    return {"installed": all(_valid_log_object(found, n, ddl) for n, ddl in expected.items()),
            "version": max(versions) if versions else None, "table": table, "table_ok": table_ok,
            "triggers": triggers,
            "rows": int(connection.execute(f"SELECT COUNT(*) FROM {LOG_TABLE}").fetchone()[0]) if table else 0}


def _restart_position(connection: Any) -> int:
    """Where a repaired log starts numbering: above anything the old table held and above the current
    time in microseconds. A reader holds a position it read from some earlier log; positions advance one
    per logged write, so no reader can be ahead of the clock in microseconds, and the reader therefore
    sees a gap and rebuilds -- even when the table it read was replaced by one holding lower numbers
    (which used to restart numbering at or below the reader and hide the changes in between). Values
    that are not numbers (a hand-made table with a text `seq`) are ignored rather than raising."""

    high = 0
    if any(str(r[1]) == "seq" for r in connection.execute(f"PRAGMA table_info({LOG_TABLE})")):
        value = connection.execute(
            f"SELECT MAX(seq) FROM {LOG_TABLE} WHERE typeof(seq) IN ('integer', 'real')").fetchone()[0]
        if value is not None:
            high = int(value)
    return max(high + 2, time.time_ns() // 1000)


def ensure_change_log(connection: Any, key: str = "") -> None:
    """Validate full definitions; repair atomically and force cached readers to rebuild. Raises
    RuntimeError, and changes nothing, when something that is not ours holds the table's name (a view,
    an index); callers that only want a cache use `log_usable`, which treats that as "no log"."""
    if not _is_sqlite(connection):
        return   # a server backend's log comes with its schema; the service role cannot create DDL
    found = _log_objects(connection)
    expected = dict(zip((LOG_TABLE, *_LOG_TRIGGERS), _LOG_DDL))
    if all(_valid_log_object(found, n, ddl) for n, ddl in expected.items()):
        return
    # Never drop an unrelated object that happens to occupy a reserved name.
    for name, (kind, _sql) in found.items():
        if kind != ("table" if name == LOG_TABLE else "trigger"):
            raise RuntimeError(f"Unexpected object type for gate schema object {name}: {kind}")
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    restart = _restart_position(connection) if LOG_TABLE in found else time.time_ns() // 1000
    for name in _LOG_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    if LOG_TABLE in found and not _valid_log_object(found, LOG_TABLE, expected[LOG_TABLE]):
        connection.execute(f"DROP TABLE {LOG_TABLE}")
    for ddl in _LOG_DDL:
        connection.execute(ddl)
    connection.execute(f"DELETE FROM {LOG_TABLE}")
    connection.execute(f"INSERT INTO {LOG_TABLE}(seq, row_id) VALUES (?, -1)", (restart,))


def log_usable(connection: Any) -> bool:
    """`ensure_change_log`, for a cache: True when the log can be followed, False when a foreign object
    holds its name. Then the cache keeps nothing between writes and every gated call reads the rows as
    they are, so the user's own object is left alone and the write still succeeds."""
    try:
        ensure_change_log(connection)
    except RuntimeError:
        return False
    return True


def change_log_installed(connection: Any) -> bool:
    if not _is_sqlite(connection):
        return True
    found = _log_objects(connection)
    return all(_valid_log_object(found, name, ddl)
               for name, ddl in zip((LOG_TABLE, *_LOG_TRIGGERS), _LOG_DDL))


_GATED_WRITES: dict[str, int] = {}


def prepare_write(store: Any, connection: Any) -> None:
    """Called by remember() before a gated write prepares its statements. Round 14 (cold first write): the
    change log only serves caches kept BETWEEN writes, so a process's first gated write on a database does
    not install it (a one-write CLI process never needs it); the second one does, once per database, before
    its statements are prepared (a schema change mid-transaction makes them all re-prepare). Until then the
    usefulness gate keeps nothing between writes (see gates_r6._stream_sync)."""

    if not _is_sqlite(connection):
        return
    key = _cache_key(store)
    seen = _GATED_WRITES.get(key, 0)
    if seen:
        log_usable(connection)
    elif len(_GATED_WRITES) > 64:
        _GATED_WRITES.clear()
    _GATED_WRITES[key] = seen + 1


def remove_change_log(connection: Any) -> bool:
    """Round 14: the COORDINATED removal path (an explicit admin step, never automatic -- one client with
    the flags off is no reason, other clients may rely on it). Drops the four triggers FIRST, then the
    table, in one transaction, so no write can ever find a trigger without its table. Run it with every
    gate-enabled process stopped (quiescent); a gate-enabled process started later simply re-creates it.
    Returns True when anything was removed. A server backend's log is part of its schema: nothing to do."""

    if not _is_sqlite(connection):
        return False
    status = change_log_status(connection)
    if not status["table"] and not any(v is not None for v in status["triggers"].values()):
        return False
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    for name in _LOG_TRIGGERS:
        connection.execute(f"DROP TRIGGER IF EXISTS {name}")
    connection.execute(f"DROP TABLE IF EXISTS {LOG_TABLE}")
    return True


# --- the change log on a server backend ---------------------------------------------------------------
#
# A server backend (the hosted edition's Postgres) takes no DDL from the engine: its service role owns
# nothing and its schema is migrated before the code that needs it starts. The log is therefore part of
# that schema and lives in the rows themselves: `memories.gate_seq` is set from one database sequence by a
# trigger on every INSERT and UPDATE, and `memories.rowid` is an identity column (named after SQLite's
# rowid so the gates' SQL is the same on both backends). A reader keeps the highest gate_seq it has seen
# and re-reads only the rows above it, through the same row-level security as every other query, so it
# can only ever see the rows its own store can. This is exact as long as (1) the engine never deletes a
# memory row (forget is a soft delete; a hard delete is not seen, which is why account deletion must drop
# the caches, as forget does), and (2) one user's sessions are serialized, which the backend does with a
# per-user advisory lock: no change of that user's rows can then commit below a position already read.

_SERVER_CHANGES_SQL = ("SELECT rowid, id, normalized_content, category, importance, protected, "
                       "COALESCE(recall_count, 0), state, gate_seq FROM memories WHERE gate_seq > ? ORDER BY gate_seq")


def _server_position(connection: Any) -> int:
    return int(connection.execute("SELECT COALESCE(MAX(gate_seq), 0) FROM memories").fetchone()[0])


def log_position(connection: Any) -> int:
    if not _is_sqlite(connection):
        return _server_position(connection)
    return int(connection.execute(f"SELECT COALESCE(MAX(seq), 0) FROM {LOG_TABLE}").fetchone()[0])


def changes_since(connection: Any, seq: int) -> tuple[int, list[tuple] | None]:
    """(newest log position, [(rowid, active row tuple or None)] for every row changed after `seq`).
    None instead of the list when the log no longer reaches back to `seq` (rebuild)."""

    if not _is_sqlite(connection):
        rows = tuple_rows(connection, _SERVER_CHANGES_SQL, (int(seq),))
        if not rows:
            newest = _server_position(connection)
            # a position the database never reached (a restored backup): rebuild
            return (newest, None) if newest < seq else (seq, [])
        return (max(int(rows[-1][8]), int(seq)),
                [(int(r[0]), tuple(r[:7]) if r[7] == "active" else None) for r in rows])
    newest, oldest = connection.execute(f"SELECT COALESCE(MAX(seq), 0), COALESCE(MIN(seq), 0) FROM {LOG_TABLE}").fetchone()
    newest, oldest = int(newest), int(oldest)
    if newest < seq:   # round 14: the log was removed and re-created since (numbering restarted)
        return newest, None
    if newest <= seq:
        return newest, []
    if seq < oldest - 1:
        return newest, None
    rids = [int(r[0]) for r in connection.execute(f"SELECT DISTINCT row_id FROM {LOG_TABLE} WHERE seq > ?", (seq,))]
    found: dict[int, tuple] = {}
    for start in range(0, len(rids), 500):
        chunk = rids[start:start + 500]
        for row in tuple_rows(connection, _ROW_SQL.format(",".join("?" * len(chunk))), chunk):
            if row[7] == "active":
                found[int(row[0])] = tuple(row[:7])
    return newest, [(rid, found.get(rid)) for rid in rids]


def follow_changes(book: "Book", changes: list[tuple], touched: list[str] | None = None) -> tuple[list[tuple], list[str]]:
    """Bring `book` up to date with `changes` (from `changes_since`). Returns (rows added or changed,
    ids removed or changed) so an index can follow the same change.

    Round 14 (reviewer A finding 2): a row whose projection is unchanged is still a logged update -- e.g. a
    content-only edit that normalizes to the same text ("Goal: x" -> "Goal x"). The book (normalized
    text and numbers) is right to skip it, but anything derived from the ORIGINAL text is not; such
    ids are appended to `touched` (when given) so the caller drops those derived values."""

    added: list[tuple] = []
    removed: list[str] = []
    for rid, row in changes:
        old_id = book.by_rowid.get(rid)
        if old_id is not None:
            if row is not None and book.rows[old_id] == row:
                if touched is not None:
                    touched.append(old_id)
                continue
            book.remove(old_id)
            removed.append(old_id)
        if row is not None:
            book.add(row)
            added.append(row)
    return added, removed


def _current_book(connection: Any, index: ActiveIndex, keep_id: str | None = None) -> Book:
    """The index's snapshot of the active rows, brought up to date from the database change log
    (round 13; exact for every writer), or rebuilt from all the rows when there is no snapshot yet or
    the log does not reach back to it. `keep_id` is unused since round 13 (the log carries the new row).
    With no usable log (a foreign object holds its name) the snapshot is rebuilt on every call."""

    usable = log_usable(connection)
    book = index.book
    if book is not None and usable and book.seq >= 0:
        seq, changes = changes_since(connection, book.seq)
        if changes is not None:
            added, removed = follow_changes(book, changes)
            if added or removed:
                index.apply(added, [m for m in removed if m not in {r[1] for r in added}])
            book.seq = seq
            return book
    seq = log_position(connection) if usable else -1
    rows = _active_rows(connection)
    index.sync(rows)
    index.book = Book(rows)
    index.book.seq = seq
    return index.book


def enforce_capacity(store: Any, connection: Any, cap: int, keep_id: str | None = None) -> list[str]:
    """Capacity gate, called from remember(): move the lowest keep-priority memories
    out of recall until `cap` remain. Protected memories and the one just written stay."""

    from . import gates_r6, storage

    if gates_r6.useful_gate_enabled():
        return gates_r6.enforce_capacity_useful(store, connection, cap, keep_id)
    active = int(connection.execute("SELECT COUNT(*) FROM memories WHERE state = 'active'").fetchone()[0])
    overflow = active - cap
    if overflow <= 0:
        return []
    with _INDEX_LOCK:
        index = index_for(store)
        index.no_save = False
        book = _current_book(connection, index, keep_id)
        movable = book.select(overflow, index, keep_id, unresolved_ids(connection))
        stamp = storage._now()
        moved = []
        for item in movable:
            if _move_out(store, connection, item, _why_moved(item, cap, active, "Capacity gate"), "gate:capacity", stamp):
                moved.append(item["id"])
                book.remove(item["id"])
        if moved:
            index.apply([], moved)
    return moved


# --- round 14: lazily loaded part (cold first write) ----------------------------------------------------
# reviewer B measured the gated cold first write on a slower Mac at 1.65-2.06x 0.4.19; every line of a gate
# module is compiled (or unmarshalled) and executed when a gated process opens a store. The parts no write
# needs -- the surprise gate, its order/meaning checks, the reversible merge, prune and consolidation -- live in `gates_proto.py` and are executed INTO THIS MODULE'S NAMESPACE on first use (same
# globals, so behaviour, patching and the text-cache clearing are unchanged); the bytecode is cached like any
# module's.
_LAZY_NAMES = frozenset({'MERGE_COPY_STATE', 'MERGE_EVIDENCE_PREFIX', 'MERGE_UNDO_COLUMNS', '_CONTRACTION_STEMS', '_NEGATION_WORDS', '_OPPOSITE_PAIRS', '_merge_proposals', '_prune_window', '_public', 'assess_surprise', 'consolidate', 'is_pruned_merge_copy', 'keep_merge_copy', 'meaning_flipped', 'merge_veto', 'negation_count', 'order_differs', 'order_pairs', 'plan_undo', 'protect_budget_allows', 'prune_merge_copies', 'undo_merge'})
_LAZY_LOADED = False


def _load_lazy() -> None:
    global _LAZY_LOADED
    if not _LAZY_LOADED:
        import importlib.util

        spec = importlib.util.find_spec(f"{__package__}.gates_proto")
        exec(spec.loader.get_code(spec.name), globals())   # noqa: S102 -- our own module file
        _LAZY_LOADED = True


def __getattr__(name: str) -> Any:
    if name in _LAZY_NAMES and not _LAZY_LOADED:
        _load_lazy()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
