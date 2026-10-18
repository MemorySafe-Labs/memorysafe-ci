"""Round 6 opt-in gates: a usefulness-weighted capacity gate and evidence-based active
forgetting ("extinction"). PROTOTYPE -- both are OFF unless their environment flag is set.

The design is inspired by (not a model of) two ideas from neuroscience: memory under a
budget is kept for what has proven useful (reward / goal relevance, salience), and a
trace that evidence says is wrong is deliberately weakened (extinction), rather than
left to decay with age or to fall out of a buffer by chance.

Flags (truthy = 1/true/yes/on):

  MEMORYSAFE_GATE_USEFUL      with MEMORYSAFE_GATE_CAPACITY: rank by usefulness first;
                              rarity only adds to memories that already show a sign of use.
  MEMORYSAFE_GATE_EXTINCTION  weaken a memory when a later write gives evidence that it is
                              wrong; after repeated evidence move it out of recall to the
                              restorable state `extinguished`.

Usefulness (all from signals the connector already stores; no schema change):
  u = U_FLOOR + W_IMP * importance + W_CRIT * [safety/decision] + W_REC * min(recalls, 3) / 3
      + W_HEAD * [current head of a slot] + W_ANCHOR * [durable anchor: deadline, must, never,
      decided, a date, an amount -- storage._DURABLE_ANCHOR / _DATE_HINT / _AMOUNT_HINT]
      + W_GOAL * [shares >= 2 content words with a goal memory]
  evidence of use = any of: safety/decision, recalled at least once, slot head, anchored, goal.
  keep-priority = u * (1 + RARITY_BONUS * rarity) with evidence, u without (rarity = 1 / cluster,
  the round-2..5 near-neighbour count). Weakened memories (extinction) get u * (1 - WEAK_CUT)
  and lose the rarity bonus. RARE QUOTA: the best-scoring rare (cluster <= RARE_CLUSTER)
  memories WITH evidence, up to ceil(RARE_QUOTA * cap), are not moved at all.
  Protected memories and the memory just written are never moved (as in rounds 1-5).

Goal memories: no new category or column. A memory is a goal memory when its text starts
with "Goal:", "Mission:" or "Objective:" (any case), or contains the tag "#goal" or
"#mission". Goal relevance = sharing >= GOAL_MIN_SHARED content words with any active goal
memory (goal memories count as goal-relevant themselves).

Extinction (evidence only, NEVER age). One unit of evidence against an older active memory O
from a new write N with confidence >= O's:
  (a1) the shipped lifecycle check marked N for review against O (needs_review);
  (a2) replacement evidence: N uses change language (now, new, moved, switched, migrated,
       slipped, revised, replacing, instead ...), shares at least half of O's content words
       (>= 2), O has 1-3 words N lacks (the old value) and N has new words (the new value); if
       both carry typed values (date, time, amount, version) they must be of a shared kind and
       differ. This only WEAKENS; supersession stays the lifecycle classifier's job.
  (b)  a conflict resolved against a memory (lifecycle auto-supersede or a confirmed
       resolve_conflict supersede/revert): shipped already moves the loser out of recall, so
       extinction does not repeat that; it weakens ACTIVE near-duplicates of the loser
       (lifecycle.classify_pair says "duplicate"). keep_both / revert undo weakening that the
       rejected side caused.
  (c)  retrieved then corrected: O was among the results of a find() in this process within the
       last RECALL_WINDOW writes, and N gives (a1) or (a2) evidence against it: one more unit.
Effect: each unit is a memory_relations row (decision WEAKEN, status 'weakened', source
gate:extinction) plus a WEAKEN decision event. A weakened memory ranks lower in find()
(score - WEAK_PENALTY per unit, at most 2 units) and lower in the usefulness keep-priority.
With >= MOVE_AT units from at least two distinct pieces of evidence it moves to state
`extinguished` (out of recall, row kept, EXTINGUISH event naming every piece of evidence) --
except while it is a side of an OPEN conflict (shipped promises both sides stay available
until someone decides) or protected.
Protected memories are never weakened automatically: a PROPOSE_WEAKEN event is logged and
`weaken(store, id, confirm=True)` applies it. `restore <id> --confirm` undoes all weakening
(rows -> status 'undone', UNWEAKEN event) and brings an extinguished memory back. Nothing is
ever hard-deleted. Time and age are never inputs.
"""

from __future__ import annotations

import sys

import heapq
import math
from bisect import bisect_right
import os
import re
from functools import lru_cache
from typing import Any

from . import gates


class _LazyRe:
    """Round 13 (cold first write): a module pattern compiled on first use, then replaced by the
    compiled pattern in this module's globals, so a process pays only for the patterns it uses.
    Same pattern, same flags, same results."""

    __slots__ = ("_name", "_args")

    def __init__(self, name: str, *args: Any) -> None:
        self._name, self._args = name, args

    def __getattr__(self, attr: str) -> Any:
        compiled = re.compile(*self._args)
        globals()[self._name] = compiled
        return getattr(compiled, attr)


def _lazy_re(name: str, *args: Any) -> Any:
    return _LazyRe(name, *args)

# --- constants. FROZEN after tuning on seeds 0-7 (the grid in GATES_R6_PREREG.md, an internal write-up):
# stage 1 chose RARITY_BONUS 0.5, RARE_QUOTA 0.05, W_ANCHOR 0.0 (anchored still counts as evidence of use);
# stage 2 chose WEAK_PENALTY 0.30, FRAME_MIN 0.6. Everything else is the pre-registered default.

U_FLOOR = 0.10
W_IMP, W_CRIT, W_REC, W_HEAD, W_ANCHOR, W_GOAL = 0.25, 0.20, 0.20, 0.15, 0.0, 0.20
# round 7. FROZEN after tuning on seeds 0-7 (GATES_R7_PREREG.md grid, gates_r7_tune.py objective):
# W_HAZ 0.20 (all three values tied), GOAL_DF_MAX 0.06, GOAL_INCIDENTAL 1. NOTE: every grid point
# failed the goal-distractor guardrail on the tuning seeds (r7 kept ~58% of distractors vs ~31% shipped).
W_HAZ = 0.20          # usefulness added by hazard words (and hazard words count as a sign of use)
GOAL_DF_MAX = 0.06    # a goal word in more than max(5, this share of active memories) is too common to count
GOAL_INCIDENTAL = 1.0  # 1 = a passing remark (read/saw/heard ...) is never goal-related
ORDER_RULE = 1.0      # 1 = in find(), a memory with extinction evidence (weakened OR proposed) ranks just
                      #     below the newer memory that gave the evidence when both are returned
# round 10 (MEMORYSAFE_GATE_STALE): narrowed supersession evidence + "possibly stale" status flags
STALE_DAYS = 30.0        # a status claim not reconfirmed for this many days is flagged "possibly stale"
STALE_NEWER_DAYS = 14.0  # ... or when its (uncommon) entity has activity at least this many days newer
STALE_PENALTY = 0.15     # find() score demotion of a flagged memory (never extinguished, never evicted for it)
ENTITY_DF_MAX = 0.05     # an entity in more than max(3, this share of the scanned active memories) is too common
# round 11 (MEMORYSAFE_GATE_GOAL2): explicit goal / "that mattered" signals (gates_goal.py). FROZEN after
# tuning on seeds 0-7 (r11_pipeline/TUNING_R11_GRID.md, TUNING_R11_RESULT.json): no grid point met the
# distractor constraint; by the declared fallback the lowest-distractor point meeting the others was
# frozen: GOAL_ENTITY 0 (entity membership off), TAG_EVIDENCE 0, GOAL_KEYWORD 1, RECONF_MIN 2.
# (tuning rerun #2 after the classify tag-check fix, 88bc12a; same grid, same pick; TAG_EVIDENCE 0 and 1
# gave identical tuning metrics at GOAL_ENTITY 0 - tie broken by grid order.)
W_GOAL2 = 0.20          # usefulness added by goal relation under the round-11 rule
W_MATTER = 0.20         # usefulness added by a "mattered" tag or repeated restatement
RECONF_MIN = 2.0        # restatements (merges) that count as "mattered"
GOAL_KEYWORD = 1.0      # 1 = entity membership also needs one non-name key word of the goal
GOAL_ENTITY = 0.0       # 1 = entity membership counts at all (0 = caller tags and restatements only)
TAG_EVIDENCE = 0.0      # 1 = a caller goal tag alone is a sign of use (rarity bonus, rare quota); 0 = weight only
RARITY_BONUS = 0.5
RARE_CLUSTER = 2
RARE_QUOTA = 0.05
WEAK_CUT = 0.5
GOAL_MIN_SHARED = 2

WEAK_PENALTY = 0.30
MOVE_AT = 2
FRAME_MIN = 0.6
RECALL_WINDOW = 3


def _num(name: str, default: float, low: float = 0.0, high: float = 1e6) -> float:
    """An override, range-validated (round 13, reviewer A: the lazy ranking's bound argument needs
    nonnegative weights and a weakening cut below 1). Unparseable, NaN or infinite values fall back to
    the default; out-of-range values are clamped into [low, high]."""
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return min(high, max(low, value))


# Round 13: allowed ranges of the overrides (everything else: 0 .. 1e6).
_RANGES = {
    "WEAK_CUT": (0.0, 0.95), "RARE_QUOTA": (0.0, 1.0), "FRAME_MIN": (0.0, 1.0), "GOAL_DF_MAX": (0.0, 1.0),
    "ENTITY_DF_MAX": (0.0, 1.0), "RARE_CLUSTER": (1.0, 1e6), "GOAL_MIN_SHARED": (1.0, 1e6), "MOVE_AT": (1.0, 1e6),
    "GOAL_INCIDENTAL": (0.0, 1.0), "ORDER_RULE": (0.0, 1.0), "GOAL_KEYWORD": (0.0, 1.0), "GOAL_ENTITY": (0.0, 1.0),
    "TAG_EVIDENCE": (0.0, 1.0), "STALE_PENALTY": (0.0, 10.0), "WEAK_PENALTY": (0.0, 10.0),
}


def useful_gate_enabled() -> bool:
    return gates._flag("MEMORYSAFE_GATE_USEFUL")


def extinction_enabled() -> bool:
    return gates._flag("MEMORYSAFE_GATE_EXTINCTION")


def stale_enabled() -> bool:
    """Round 10: narrowed supersession evidence, keep-both respected, "possibly stale" flags."""
    return gates._flag("MEMORYSAFE_GATE_STALE")


def params() -> dict[str, float]:
    """Constants, each overridable by MEMORYSAFE_GATE_R6_<NAME> (used only by the tuning run)."""

    names = ("U_FLOOR", "W_IMP", "W_CRIT", "W_REC", "W_HEAD", "W_ANCHOR", "W_GOAL", "RARITY_BONUS",
             "RARE_CLUSTER", "RARE_QUOTA", "WEAK_CUT", "GOAL_MIN_SHARED", "WEAK_PENALTY", "MOVE_AT",
             "FRAME_MIN", "RECALL_WINDOW", "W_HAZ", "GOAL_DF_MAX", "GOAL_INCIDENTAL", "ORDER_RULE",
             "STALE_DAYS", "STALE_NEWER_DAYS", "STALE_PENALTY", "ENTITY_DF_MAX",
             "W_GOAL2", "W_MATTER", "RECONF_MIN", "GOAL_KEYWORD", "GOAL_ENTITY", "TAG_EVIDENCE")
    return {n: _num(f"MEMORYSAFE_GATE_R6_{n}", globals()[n], *_RANGES.get(n, (0.0, 1e6))) for n in names}


# --- per-memory features (cached by text) ----------------------------------------------

_GOAL_RE = _lazy_re("_GOAL_RE", r"^\s*(?:goal|mission|objective)s?\s*:|#(?:goal|mission)\b", re.IGNORECASE)
# Round 7: words that name a hazard to a person (allergy, medication, poisoning ...). A memory that
# says one is treated as showing a sign of use, like a durable anchor: the round-7 diagnosis on
# tuning seeds 0-7 found every rare-critical miss was an unprotected, mislabelled allergy note
# with no anchor word, no date and no recall before the question.
_HAZARD = _lazy_re("_HAZARD", 
    r"\b(?:allerg\w*|anaphyla\w*|epinephrine|epi-?pens?|intoleran\w*|overdos\w*|toxic\w*|poison\w*|"
    r"seizures?|contraindicat\w*|hazard\w*|dangerous)\b", re.IGNORECASE)
# Words of the goal sentence itself that say THAT it is a goal, not WHAT it is about.
_GOAL_FRAME_TEXT = ("goal goals mission missions objective objectives aim aims want wants wanted need needs plan plans "
                    "try make sure get gets achieve finish finishes complete done before by end this year quarter")
# A passing remark ("read an article about ...", "saw a video of ...") does not serve a goal even
# when it shares the goal's words.
_INCIDENTAL = frozenset("""read reads saw seen watched watching heard hearing article articles news story stories podcast
podcasts video videos documentary blog post magazine joked joke laughed laughing rumour rumor gossip photo photos
meme tweet""".split())
# Normalized text that may hold a slot of each kind (a superset test before the exact extractor).
_KIND_HINT = {
    "lives_in": ("liv", "moved", "relocated"), "works_at": ("work",), "works_as": ("work",), "prefers": ("prefer",),
    "deadline": ("deadline", "due", "close", "submission"), "version": tuple("0123456789"), "titled": (" is ", " are "),
}


@lru_cache(maxsize=65536)
def features(content: str, normalized: str | None = None) -> tuple:
    """(slot keys, anchored, is_goal, content terms, hazard, words) of one memory's text."""

    from . import storage
    from .lifecycle import _norm_slot, extract_slots

    if normalized is None:
        normalized = storage._normalize(content)
    slots = frozenset((kind, _norm_slot(subject)) for kind, subject, _value in extract_slots(content))
    anchored = bool(storage._DURABLE_ANCHOR.search(content) or storage._DATE_HINT.search(content)
                    or storage._AMOUNT_HINT.search(content))
    is_goal = bool(_GOAL_RE.search(content))
    terms = gates.terms_of(normalized)
    hazard = bool(_HAZARD.search(content))
    return slots, anchored, is_goal, terms, hazard, frozenset(normalized.split())


class _FeatureCache:
    """Per database: features by id (worked out once per text), the snapshot of active rows the
    usefulness gate ranks (checked against the database with one aggregate query per write, as
    the round-4 capacity gate does), and the goal profiles. Nothing here is written to disk."""

    def __init__(self) -> None:
        self.by_id: dict[str, tuple] = {}
        self.book: Any = None
        self.rid2mid: dict[int, str] = {}
        self.goal_ids: set[str] | None = None
        self.goal_key: Any = None
        self.goal_profiles: list[frozenset] = []
        self.head_memo: dict[str, tuple] = {}
        self.stats = {"full_passes": 0, "evaluated": 0, "index_syncs": 0, "streamed": 0, "snapshot_path": 0}
        self.sig: Any = None          # round 11: gates_goal._Signals (ids and counts only)
        self.goal2: Any = None        # round 11: (profiles, token df, df limit, all goal names)
        self.goal2_key: Any = None
        self.names: dict[str, tuple] = {}   # round 11: id -> (normalized text, names, opener-only names)
        self.feat_seq: int | None = None    # round 14: change-log position the features (by_id) reflect


_CACHES: dict[str, _FeatureCache] = {}


def _feature_cache(store: Any) -> _FeatureCache:
    """This store's cache, keyed by `gate_flags.cache_key` (the database file locally; backend, user and
    layers on a server backend, where every store has the same `database_path`, None)."""
    from .gate_flags import cache_key

    key = cache_key(store)
    cache = _CACHES.get(key)
    if cache is None:
        if len(_CACHES) > 8:
            _CACHES.clear()
        cache = _CACHES[key] = _FeatureCache()
    return cache


def _feats(fc: _FeatureCache, connection: Any, ids: list[str], rows: dict[str, tuple]) -> None:
    """Make sure fc.by_id holds features for `ids` (read the text of the missing ones)."""

    cache = fc.by_id
    missing = [m for m in ids if (c := cache.get(m)) is None or c[0] != rows[m][2]]
    for start in range(0, len(missing), 500):
        chunk = missing[start:start + 500]
        for mid, content in connection.execute(
            f"SELECT id, content FROM memories WHERE id IN ({','.join('?' * len(chunk))})", chunk
        ).fetchall():
            cache[mid] = (rows[mid][2], features(str(content), rows[mid][2]))
    if len(cache) > 2 * max(1, len(rows)) + 1000:
        for mid in [m for m in cache if m not in rows]:
            del cache[mid]


def _snapshot(connection: Any, fc: _FeatureCache, keep_id: str | None) -> Any:
    """The active rows (gates.Book: rows by id, rowids in order, running sums), kept up to date from
    the database change log (round 13, see gates.changes_since: every insert/update/delete of a memory
    row by any process is seen, so a category edit or an equal-length rewording is no longer missed)
    and rebuilt when there is no snapshot yet or the log does not reach back to it."""

    usable = gates.log_usable(connection)
    book = fc.book
    if book is not None and usable and book.seq >= 0:
        seq, changes = gates.changes_since(connection, book.seq)
        if changes is not None:
            if changes:
                top = book.rowids[-1] if book.rowids else 0
                appended = all(rid > top and row is not None and rid not in book.by_rowid for rid, row in changes)
                touched: list[str] = []
                added, removed = gates.follow_changes(book, changes, touched)
                for mid in touched:   # round 14: logged update, same projection -- text-derived values go
                    fc.by_id.pop(mid, None)
                    fc.names.pop(mid, None)
                if touched:
                    _reset_derived(fc)
                for rid, row in changes:
                    if row is None:
                        fc.rid2mid.pop(rid, None)
                for row in added:
                    fc.rid2mid[row[0]] = row[1]
                if appended:
                    for row in added:
                        _note_goal_row(fc, connection, row)
                else:   # an existing row changed or left: drop everything derived from the old rows
                    for mid in removed:
                        fc.by_id.pop(mid, None)
                        fc.names.pop(mid, None)
                    _reset_derived(fc)
            book.seq = seq
            return book
    seq = gates.log_position(connection) if usable else -1   # -1: no log to follow, rebuild next time
    rows = gates._active_rows(connection)
    # A gap loses the list of touched rows, including content-only edits.
    fc.by_id.clear()
    fc.names.clear()
    fc.goal_key = None
    fc.goal_profiles = []
    fc.goal2_key = None
    fc.goal2 = None
    fc.book = gates.Book(rows)
    fc.book.seq = seq
    fc.rid2mid = {row[0]: row[1] for row in rows}
    _reset_derived(fc)
    return fc.book


def discard_cache(key: str) -> None:
    """Round 14 (reviewer A finding 1): after a rolled-back session, empty this database's cache IN PLACE
    (a caller may hold the object): it is rebuilt from committed rows at the next gated call."""

    fc = _CACHES.get(key)
    if fc is None:
        return
    fc.book = None
    fc.rid2mid = {}
    fc.by_id.clear()
    fc.names.clear()
    fc.feat_seq = None
    fc.goal_key = None
    fc.goal_profiles = []
    fc.goal2 = None
    fc.goal2_key = None
    fc.sig = None
    _reset_derived(fc)


def _reset_derived(fc: _FeatureCache) -> None:
    fc.goal_ids = None
    fc.head_memo.clear()
    if fc.sig is not None:
        fc.sig.loaded = False         # round 11: another process may have tagged or merged
    fc.__dict__.pop("goal_live", None)


def forget_in_process(store: Any, memory_ids: list[str]) -> None:
    """Round 13: drop this process's usefulness-gate data about forgotten memories (features hold
    words of the text) and the text-keyed lru caches."""

    from .gate_flags import cache_key

    fc = _CACHES.get(cache_key(store))
    if fc is not None:
        for mid in memory_ids:
            fc.by_id.pop(mid, None)
            fc.names.pop(mid, None)
        fc.book = None
        fc.rid2mid = {}
        fc.feat_seq = None
        _reset_derived(fc)
    for function in _LRU_TEXT_CACHES():
        try:
            function.cache_clear()
        except AttributeError:
            pass


def _LRU_TEXT_CACHES() -> list:
    found = []
    # round 13: a module that was never imported holds no cached text; do not import it to find out
    for module in (gates, sys.modules[__name__], sys.modules.get(f"{__package__}.gates_goal"),
                   sys.modules.get(f"{__package__}.gates_stale")):
        if module is None:
            continue
        for value in vars(module).values():
            # round 14: walk the whole decorator chain; every layer with an lru cache is listed
            seen = 0
            while callable(value) and seen < 16:
                if hasattr(value, "cache_clear") and hasattr(value, "cache_info") and value not in found:
                    found.append(value)
                value = getattr(value, "__wrapped__", None)
                seen += 1
    return found


def _maybe_goal(normalized: str) -> bool:
    return "goal" in normalized or "mission" in normalized or "objective" in normalized


def _note_goal_row(fc: _FeatureCache, connection: Any, row: tuple) -> None:
    if fc.goal_ids is not None and _maybe_goal(row[2]):
        content = connection.execute("SELECT content FROM memories WHERE id = ?", (row[1],)).fetchone()
        if content is not None and _GOAL_RE.search(str(content[0])):
            fc.goal_ids.add(row[1])










def _goal_signal(fc: _FeatureCache, connection: Any, rows: dict[str, tuple], mid: str, feat: tuple,
                 profiles: list[frozenset], p: dict) -> tuple[bool, bool]:
    """(goal-related, mattered). Round 7 rule unless MEMORYSAFE_GATE_GOAL2 is on (then gates_goal)."""
    if not gates._flag("MEMORYSAFE_GATE_GOAL2"):   # round 13: same as gates_goal.enabled(), no import
        return _goal_related(feat, profiles, p), False, True
    from . import gates_goal

    if fc.sig is None:
        fc.sig = gates_goal._Signals()
    sig = fc.sig
    if not sig.loaded:
        sig.load(connection)
    profs, df, limit, allnames = fc.goal2
    mattered = mid in sig.mattered or sig.merges.get(mid, 0) >= p["RECONF_MIN"]
    if feat[2]:
        return True, mattered, True
    tags = sig.goal_tags.get(mid)
    _load_lazy()
    tagged = bool(tags and any(_goal_tag_live(fc, connection, rows, g) for g in tags))
    by_rule = False
    if profs and (feat[5] & allnames):     # a goal name among its words: read the text and check
        content = connection.execute("SELECT content FROM memories WHERE id = ?", (mid,)).fetchone()
        if content is not None:
            common = frozenset(t for t in feat[5] if df.get(t, 0) > limit)
            by_rule, _m = gates_goal.classify(mid, str(content[0]), feat, profs, common, sig, p, _INCIDENTAL)
    return (tagged or by_rule), mattered, (by_rule or (tagged and bool(p["TAG_EVIDENCE"])))


def _goal_related(feat: tuple, profiles: list[frozenset], p: dict) -> bool:
    if feat[2]:
        return True
    if not profiles:
        return False
    if p["GOAL_INCIDENTAL"] and feat[5] & _INCIDENTAL:
        return False
    need = int(p["GOAL_MIN_SHARED"])
    terms = feat[3]
    return any(len(terms & key) >= need for key in profiles)


def weak_strengths(connection: Any) -> dict[str, int]:
    """memory id -> units of standing extinction evidence (status 'weakened')."""

    return {str(r[0]): int(r[1]) for r in connection.execute(
        "SELECT old_memory_id, COUNT(*) FROM memory_relations WHERE status = 'weakened' GROUP BY old_memory_id"
    ).fetchall()}


# --- usefulness-weighted capacity gate ---------------------------------------------------


def _base_u(p: dict, category: str, importance: float, recalls: int) -> float:
    return (p["U_FLOOR"] + p["W_IMP"] * importance + (p["W_CRIT"] if category in gates.CRITICAL_CATEGORIES else 0.0)
            + p["W_REC"] * (recalls if recalls < 3 else 3) / 3)


def _usefulness(p: dict, row: tuple, head: bool, feat: tuple, goal: bool, weak: int,
                mattered: bool = False, goal_evidence: bool = True) -> tuple[float, bool]:
    _slots, anchored, _is_goal, _terms, hazard, _words = feat
    critical = row[3] in gates.CRITICAL_CATEGORIES
    w_goal = p["W_GOAL2"] if p.get("_GOAL2") else p["W_GOAL"]
    u = (_base_u(p, row[3], row[4], row[6]) + (p["W_HEAD"] if head else 0.0) + (p["W_ANCHOR"] if anchored else 0.0)
         + (w_goal if goal else 0.0) + (p["W_HAZ"] if hazard else 0.0) + (p["W_MATTER"] if mattered else 0.0))
    evidence = critical or row[6] > 0 or head or anchored or (goal and goal_evidence) or hazard or mattered
    if weak:
        u *= (1.0 - p["WEAK_CUT"])
        evidence = False
    return u, evidence


def _cluster(index: gates.ActiveIndex, memory_id: str) -> int:
    siblings = index.templates[index.template[memory_id]] - 1
    if siblings + 1 >= gates.CLUSTER_CAP:
        return gates.CLUSTER_CAP
    return min(gates.CLUSTER_CAP, 1 + max(index.neighbours(memory_id), siblings))




def _full_items(fc: _FeatureCache, connection: Any, rows: dict[str, tuple], keep_id: str | None, p: dict,
                weak: dict[str, int], profiles: list[frozenset], excluded: frozenset = frozenset()) -> dict[str, tuple]:
    """Every movable row's (u, evidence, head, goal): the reference computation (used for the rare
    quota and by the equivalence test)."""

    fc.stats["full_passes"] += 1
    _feats(fc, connection, list(rows), rows)
    feats = fc.by_id
    newest: dict[tuple, tuple[int, str]] = {}
    for mid, row in rows.items():
        for key in feats[mid][1][0]:
            have = newest.get(key)
            if have is None or row[0] > have[0]:
                newest[key] = (row[0], mid)
    heads = {mid for _rid, mid in newest.values()}
    out = {}
    for mid, row in rows.items():
        if row[5] or mid == keep_id or mid in excluded:  # round 13: open-conflict sides never move
            continue
        f = feats[mid][1]
        goal, mattered, gev = _goal_signal(fc, connection, rows, mid, f, profiles, p)
        u, ev = _usefulness(p, row, mid in heads, f, goal, weak.get(mid, 0), mattered, gev)
        out[mid] = (u, ev, mid in heads, goal)
    return out


# --- round 14: the first write without a snapshot of every active row ------------------------------
#
# reviewer B (r13, Intel Mac): a cold first write at the cap still cost 1.65-2.06x 0.4.19. On Linux the gated
# part of that write was dominated not by ranking (it evaluates ~1 row) but by reading every active row
# and building the Book snapshot the lazy ranking walks (8 of 15 ms at N=5000). `rank_useful` now streams
# the candidates from SQLite in lower-bound order instead: the database computes the same lower bound
# (category, importance, recalls, extinction weakening) and returns the lowest few rows first (LIMIT,
# widened 4x when more are needed); the Python bound is recomputed for every fetched row and a batch is
# fetched whenever the next unfetched (bound, rowid) could still come before the top of the heap. The bound
# SQLite returns must equal Python's to the bit (same IEEE operations in the same order; checked for every
# fetched row -- on any difference the round-13 path below runs), so the visiting order and the stop rule
# are exactly those of the full heap. The
# slot-head test reads only newer rows that could hold the slot (an indexed rowid range + instr), and the
# goal test needs no store-wide word counts unless a goal could share GOAL_MIN_SHARED words with a
# memory (never, at the shipped GOAL_MIN_SHARED=99; otherwise -- and with MEMORYSAFE_GATE_GOAL2 -- the
# round-13 snapshot path below runs, unchanged). The rare-quota and the neighbour index, needed only
# for rows with evidence of use that reach the second stage, are built exactly as before when first
# needed. Text features cached between writes are dropped for every row the change log reports.

def _stream_sync(fc: _FeatureCache, connection: Any, keep_id: str | None) -> None:
    """Bring fc.by_id / fc.names in line with the database without reading every row."""

    if fc.book is None and not gates.change_log_installed(connection):
        # round 14: no log yet (a process's first gated write does not install it): keep nothing between
        # writes -- every text feature is worked out from the row as it is now
        fc.by_id.clear()
        fc.names.clear()
        fc.head_memo.clear()
        fc.rid2mid = {}
        fc.goal_ids = None
        fc.feat_seq = None
        return
    if fc.book is not None:          # a snapshot exists: it already follows the log, per row
        _snapshot(connection, fc, keep_id)
        fc.feat_seq = fc.book.seq if fc.book.seq >= 0 else None
        if fc.feat_seq is None:      # no usable log: nothing derived is kept between writes
            fc.by_id.clear()
            fc.names.clear()
            fc.head_memo.clear()
            fc.goal_ids = None
        return
    if fc.feat_seq is not None and fc.feat_seq >= 0:
        seq, changes = gates.changes_since(connection, fc.feat_seq)
        if changes is not None:
            for rid, row in changes:
                mid = fc.rid2mid.pop(rid, None) or (row[1] if row is not None else None)
                if mid is not None:
                    fc.by_id.pop(mid, None)
                    fc.names.pop(mid, None)
                    fc.head_memo.pop(mid, None)
            if changes:
                fc.goal_ids = None
            fc.feat_seq = seq
            return
    fc.by_id.clear()
    fc.names.clear()
    fc.head_memo.clear()
    fc.rid2mid = {}
    fc.goal_ids = None
    fc.feat_seq = gates.log_position(connection)


def _stream_goal_profiles(fc: _FeatureCache, connection: Any, p: dict) -> list[frozenset] | None:
    """[] when no memory can be goal-related by shared words (no goal, or every goal has fewer than
    GOAL_MIN_SHARED key words even before common words are taken out): then the profiles cannot change
    any answer and need no store-wide counts. None: they can -- use the snapshot path."""

    # A goal's key words are some of its content terms; a term comes from one whitespace-separated word of
    # two or more characters, so a text with `need` terms is at least 3*need - 1 characters long. Shorter
    # goals cannot reach `need` shared words and are not even read.
    need = int(p["GOAL_MIN_SHARED"])
    frame = gates.terms_of(_GOAL_FRAME_TEXT)
    for _mid, content, normalized in connection.execute(
            f"SELECT id, content, normalized_content FROM memories{gates.not_indexed(connection)} WHERE state = 'active' AND "
            "length(normalized_content) >= ? AND (instr(normalized_content, 'goal') > 0 OR "
            "instr(normalized_content, 'mission') > 0 OR instr(normalized_content, 'objective') > 0)",
            (max(0, 3 * need - 1),)):
        if _GOAL_RE.search(str(content)) and len(features(str(content), str(normalized))[3] - frame) >= need:
            return None
    return []


def _stream_is_head(fc: _FeatureCache, connection: Any, rows: dict[str, tuple], mid: str) -> bool:
    """`_is_head` without the snapshot: the newer active rows that could hold each slot, from SQL."""

    slots = fc.by_id[mid][1][0]
    if not slots:
        return False
    start = rows[mid][0]
    blocked = set()
    for key in slots:
        kind, subject = key
        words = [w for w in subject.split() if len(w) > 1]
        word = max(words, key=len) if words else ""
        hints = _KIND_HINT.get(kind, ("",))
        sql = ("SELECT rowid, id, normalized_content, category, importance, protected, COALESCE(recall_count, 0) "
               "FROM memories WHERE state = 'active' AND rowid > ?")
        args: list[Any] = [start]
        if word:
            sql += " AND instr(normalized_content, ?) > 0"
            args.append(word)
        cands = []
        for r in connection.execute(sql + " ORDER BY rowid", args):
            row = (int(r[0]), str(r[1]), str(r[2]), r[3], r[4], r[5], r[6])
            if any(h in f" {row[2]} " for h in hints):
                rows.setdefault(row[1], row)
                cands.append(row[1])
        if cands:
            _feats(fc, connection, cands, rows)
            for m in cands:
                fc.rid2mid[rows[m][0]] = m
            for m in reversed(cands):
                if key in fc.by_id[m][1][0]:
                    blocked.add(key)
                    break
    return any(k not in blocked for k in slots)


def rank_useful(store: Any, connection: Any, count: int, cap: int, keep_id: str | None = None,
                index_rows: Any = None) -> list[dict[str, Any]]:
    """The `count` lowest keep-priority movable memories under the usefulness rule (round 14: streamed
    from SQLite in lower-bound order; same answer as `_rank_useful_book` and `rank_useful_full`)."""

    if gates._flag("MEMORYSAFE_GATE_GOAL2") or os.environ.get("MEMORYSAFE_GATE_R14_STREAM", "1").strip() == "0":
        _load_lazy()
        return _rank_useful_book(store, connection, count, cap, keep_id, index_rows)
    p = params()
    fc = _feature_cache(store)
    _stream_sync(fc, connection, keep_id)
    profiles = _stream_goal_profiles(fc, connection, p)
    if profiles is None:
        fc.stats["snapshot_path"] += 1
        _load_lazy()
        return _rank_useful_book(store, connection, count, cap, keep_id, index_rows)
    fc.stats["streamed"] += 1
    weak = weak_strengths(connection) if extinction_enabled() else {}
    cut = 1.0 - p["WEAK_CUT"]
    excluded = gates.unresolved_ids(connection)
    crit = sorted(gates.CRITICAL_CATEGORIES)
    bound = (f"(? + ? * importance + (CASE WHEN category IN ({','.join('?' * len(crit))}) THEN ? ELSE 0.0 END) "
             "+ ? * (CASE WHEN COALESCE(recall_count, 0) < 3 THEN COALESCE(recall_count, 0) ELSE 3 END) / 3.0)")
    bound_args: list[Any] = [float(p["U_FLOOR"]), float(p["W_IMP"]), *crit, float(p["W_CRIT"]), float(p["W_REC"])]
    if weak:
        bound = (f"({bound} * (CASE WHEN id IN (SELECT old_memory_id FROM memory_relations WHERE status = 'weakened') "
                 "THEN ? ELSE 1.0 END))")
        bound_args.append(float(cut))
    sql = (f"SELECT rowid, id, normalized_content, category, importance, protected, COALESCE(recall_count, 0), "
           f"{bound} AS lb FROM memories{gates.not_indexed(connection)} WHERE state = 'active' AND COALESCE(protected, 0) = 0")
    if keep_id is not None:
        sql += " AND id != ?"
        bound_args.append(keep_id)
    sql += " ORDER BY lb, rowid LIMIT ? OFFSET ?"
    rows: dict[str, tuple] = {}
    heap: list[tuple] = []
    feed: dict[str, Any] = {"taken": 0, "limit": max(64, 2 * count), "frontier": (-math.inf, -1), "done": False,
                            "inexact": False}

    def fetch() -> None:
        got = connection.execute(sql, [*bound_args, feed["limit"], feed["taken"]]).fetchall()
        for r in got:
            row = (int(r[0]), str(r[1]), str(r[2]), r[3], r[4], r[5], r[6])
            mid = row[1]
            rows[mid] = row
            lb = _base_u(p, row[3], row[4], row[6])
            if mid in weak:
                lb *= cut
            if lb != r[7]:          # SQLite's double arithmetic differs from Python's: do not trust the order
                feed["inexact"] = True
            feed["frontier"] = (r[7], row[0])
            if mid in excluded or row[5]:
                continue
            heapq.heappush(heap, (lb, row[0], 0, mid))
        feed["taken"] += len(got)
        feed["done"] = len(got) < feed["limit"]
        feed["limit"] *= 4

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
            full = _snapshot(connection, fc, keep_id).rows    # the rare quota needs every row (rare event)
            fc.feat_seq = fc.book.seq
            items = _full_items(fc, connection, full, keep_id, p, weak, profiles, excluded)
            ix = index()
            rare = []
            for m2, (u2, ev2, _h, _g) in items.items():
                if ev2:
                    c2 = _cluster(ix, m2)
                    if c2 <= p["RARE_CLUSTER"]:
                        rare.append((u2 * (1 + p["RARITY_BONUS"] / c2), full[m2][0], m2))
            rare.sort(reverse=True)
            state["quota"] = {m for *_x, m in rare[: math.ceil(p["RARE_QUOTA"] * cap)]}
        return mid in state["quota"]

    info: dict[str, tuple] = {}
    while True:
        # every row not fetched yet comes after `frontier` in (bound, rowid) order -- the heap's order -- so
        # fetching until the frontier reaches the heap top gives the same visiting order as the full heap
        while not feed["done"] and (not heap or feed["frontier"] < (heap[0][0], heap[0][1])):
            fetch()
        if feed["inexact"]:
            fc.stats["snapshot_path"] += 1
            _load_lazy()
            return _rank_useful_book(store, connection, count, cap, keep_id, index_rows)
        if not heap:
            break
        key, rowid, stage, mid = heap[0]
        if len(best) >= count and (key, rowid) > (best[-1][0], best[-1][1]):
            break
        heapq.heappop(heap)
        row = rows[mid]
        if stage == 0:
            fc.stats["evaluated"] += 1
            _feats(fc, connection, [mid], rows)
            fc.rid2mid[row[0]] = mid
            f = fc.by_id[mid][1]
            head = _stream_is_head(fc, connection, rows, mid)
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






def _why_useful(item: dict[str, Any], cap: int, active: int) -> str:
    row = item["row"]
    signs = [name for name, on in (("safety/decision", row[3] in gates.CRITICAL_CATEGORIES),
                                   ("recalled", row[6] > 0), ("current slot value", item["head"]),
                                   ("durable anchor", item["anchored"]), ("goal-related", item["goal"]),
                                   ("names a hazard", item.get("hazard"))) if on]
    rarity = f"rarity {1 / item['cluster']:.2f} counted" if item["cluster"] else (
        "rarity not counted (no sign of use)" if not item["evidence"] else "rarity not needed")
    weak = f", weakened by {item['weak']} piece(s) of evidence" if item["weak"] else ""
    return (f"Usefulness gate: {active} active memories exceed the capacity of {cap}. Moved out of recall "
            f"(restorable) as one of the least useful unprotected memories: {row[3]}, importance {row[4]:.2f}, "
            f"recalled {row[6]} time(s), signs of use: {', '.join(signs) or 'none'}{weak}; usefulness "
            f"{item['u']:.2f}, {rarity}, keep priority {item['priority']:.2f}. Restore brings it back.")


def enforce_capacity_useful(store: Any, connection: Any, cap: int, keep_id: str | None = None) -> list[str]:
    from . import storage

    active = int(connection.execute("SELECT COUNT(*) FROM memories WHERE state = 'active'").fetchone()[0])
    overflow = active - cap
    if overflow <= 0:
        return []
    with gates._INDEX_LOCK:
        fc = _feature_cache(store)
        used: dict[str, Any] = {}
        chosen = rank_useful(store, connection, overflow, cap, keep_id, index_rows=used)
        stamp = storage._now()
        moved = []
        for item in chosen:
            if gates._move_out(store, connection, item, _why_useful(item, cap, active), "gate:useful", stamp):
                moved.append(item["id"])
                row = fc.book.rows.get(item["id"]) if fc.book is not None else None
                if row is not None:
                    fc.book.remove(item["id"])
                    fc.rid2mid.pop(row[0], None)
        if moved:
            ix = used.get("index") or gates._INDEXES.get(gates._cache_key(store))
            if ix is not None and ix.norm:
                ix.apply([], moved)
                if ix.book is not None:
                    for mid in moved:
                        if mid in ix.book.rows:
                            ix.book.remove(mid)
    return moved


# --- extinction --------------------------------------------------------------------------

EXTINGUISHED_STATE = "extinguished"
_CHANGE = frozenset("""now new newly moved move moves switched switch changed change updated update replaced
replacing replace replaces instead migrated migrate slipped revised promoted relocated rescheduled postponed
pushed longer anymore left off became reorg""".split())
_FROM_MARK = _lazy_re("_FROM_MARK", r"\b(?:from|off|left|replacing|replaces|instead of)\s+([^\W_]+)", re.IGNORECASE)


@lru_cache(maxsize=65536)
def _shape(content: str) -> tuple[frozenset, frozenset, dict]:
    from . import storage
    from .lifecycle import extract_values

    normalized = storage._normalize(content)
    words = frozenset(normalized.split())
    return gates.terms_of(normalized), words, extract_values(content)[0]


def replacement_evidence(new_content: str, old_content: str, frame_min: float = FRAME_MIN,
                         common: frozenset = frozenset()) -> str | None:
    """Plain-language evidence that `new_content` replaces the value `old_content` holds, or None."""

    new_terms, new_words, new_values = _shape(new_content)
    old_terms, _old_words, old_values = _shape(old_content)
    if stale_enabled():
        # Round 10: dates are not a subject. Month/weekday words never count as shared subject
        # words, and a replacement needs a slot or entity match (same typed slot, a shared
        # uncommon proper name, or an explicit "from X").
        dt = _date_terms()
        new_terms, old_terms = new_terms - dt, old_terms - dt
        if not subject_match(new_content, old_content, common) or is_tentative(new_content):
            return None
    if not old_terms:
        return None
    shared = new_terms & old_terms
    if len(shared) < 2 or len(shared) < frame_min * len(old_terms):
        return None
    old_only = old_terms - new_terms
    change = new_words & _CHANGE
    new_only = new_terms - old_terms - {gates._TERM_MAP[w] for w in change}
    explicit = [m.group(1) for m in _FROM_MARK.finditer(new_content)
                if gates._TERM_MAP[m.group(1).casefold()] in old_terms]
    if not change and not explicit:
        return None
    typed_change = False
    if new_values or old_values:
        kinds = set(new_values) & set(old_values)
        if (new_values and old_values and not kinds) or any(new_values[k] == old_values[k] for k in kinds):
            return None
        typed_change = bool(kinds)
    if len(old_only) > 3 and not explicit:
        return None
    if not old_only and not (explicit or typed_change):
        return None
    if not new_only and not typed_change:
        return None
    return (f"later write says it changed ({', '.join(sorted(change)) or 'from ' + explicit[0]}); "
            f"same subject words ({', '.join(sorted(shared))}); old value words ({', '.join(sorted(old_only)) or '-'}) "
            f"vs new ({', '.join(sorted(new_only)) or 'a different ' + '/'.join(sorted(set(new_values)))})")


def _recent_recall(store: Any) -> set[str]:
    from .gate_flags import store_state

    state = store_state(store)
    last = state.get("_r6_last_recall")
    if not last:
        return set()
    seq, ids = last
    if state.get("_r6_writes", 0) - seq > int(params()["RECALL_WINDOW"]):
        return set()
    return set(ids)


def note_recall(store: Any, ids: list[str]) -> None:
    """find() calls this (flag on) so a correction right after a recall counts as evidence (c)."""

    from .gate_flags import store_state

    state = store_state(store)
    state["_r6_last_recall"] = (state.get("_r6_writes", 0), list(ids))


def _open_conflict(connection: Any, memory_id: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM memory_relations WHERE status = 'open' AND (old_memory_id = ? OR new_memory_id = ?) LIMIT 1",
        (memory_id, memory_id)).fetchone() is not None


def _add_unit(store: Any, connection: Any, old_id: str, new_id: str, kind: str, evidence: str, stamp: str) -> None:
    from .lifecycle import record_relation

    record_relation(connection, old_memory_id=old_id, new_memory_id=new_id, decision="WEAKEN", status="weakened",
                    reason=f"Extinction ({kind}): evidence that this memory is no longer right.",
                    evidence=evidence, confidence=0.5, source="gate:extinction", created_at=stamp)
    store._record_event(connection, "WEAKEN", old_id,
                        f"Extinction gate ({kind}): weakened, not deleted. Evidence from {new_id}: {evidence}. "
                        "It ranks lower in recall and in the keep order; restore undoes this.",
                        source="gate:extinction")


def _apply(store: Any, connection: Any, old_id: str, new_id: str, units: list[tuple[str, str]], stamp: str,
           confirm: bool = False) -> dict[str, Any]:
    row = connection.execute("SELECT state, protected FROM memories WHERE id = ?", (old_id,)).fetchone()
    if row is None or row["state"] != "active" or old_id == new_id:
        return {"applied": False}
    if stale_enabled() and keep_both_pair(connection, old_id, new_id):
        store._record_event(connection, "SKIP_SUPERSEDE", old_id,
                            f"Not treated as outdated by {new_id}: the two were resolved as keep-both.",
                            source="gate:stale")
        return {"applied": False, "keep_both": True}
    if row["protected"] and not confirm:
        from .lifecycle import record_relation

        # Round 7: the proposal is also kept as a relation row (status 'proposed'). It changes
        # nothing about the protected memory -- not its state, protection, importance or keep
        # order -- and is only used by find() to list the newer memory first (see order_pairs).
        record_relation(connection, old_memory_id=old_id, new_memory_id=new_id, decision="PROPOSE_WEAKEN",
                        status="proposed", reason="Extinction proposal for a protected memory (nothing applied).",
                        evidence="; ".join(e for _k, e in units), confidence=0.5, source="gate:extinction",
                        created_at=stamp)
        store._record_event(connection, "PROPOSE_WEAKEN", old_id,
                            f"Extinction gate: evidence from {new_id} says this protected memory may be outdated "
                            f"({'; '.join(e for _k, e in units)}). Protected, so nothing was changed; "
                            "weaken with confirm to apply.", source="gate:extinction")
        return {"applied": False, "proposed": True}
    for kind, evidence in units:
        _add_unit(store, connection, old_id, new_id, kind, evidence, stamp)
    p = params()
    rows = connection.execute(
        "SELECT new_memory_id, evidence FROM memory_relations WHERE status = 'weakened' AND old_memory_id = ?",
        (old_id,)).fetchall()
    strength = len(rows)
    distinct = len({(str(r[0]), str(r[1])) for r in rows})
    moved = False
    from .gate_flags import rank_only

    # A rank-only store (the hosted edition never moves a memory out of recall by itself) keeps the
    # weakening, which lowers the memory in find(), but an automatic extinction stops there.
    if strength >= p["MOVE_AT"] and distinct >= 2 and (confirm or not row["protected"]) \
            and (confirm or not rank_only(store)) and not _open_conflict(connection, old_id):
        moved = bool(connection.execute(
            f"UPDATE memories SET state = '{EXTINGUISHED_STATE}', updated_at = ? WHERE id = ? AND state = 'active'"
            + ("" if confirm else " AND protected = 0"), (stamp, old_id)).rowcount)
        if moved:
            store._record_event(connection, "EXTINGUISH", old_id,
                                f"Extinction gate: moved out of recall (restorable, state '{EXTINGUISHED_STATE}') after "
                                f"{strength} pieces of evidence that it is outdated: "
                                + " | ".join(str(r[1]) for r in rows)
                                + ". Never by age. Restore brings it back and clears the weakening.",
                                source="gate:extinction")
    return {"applied": True, "strength": strength, "extinguished": moved}


def after_write(store: Any, connection: Any, new_id: str, new_content: str, new_confidence: float,
                lifecycle: dict[str, Any], others: list[Any], stamp: str) -> list[str]:
    """Collect evidence (a1, a2, b, c) from one write and apply it. Returns ids extinguished."""

    from .lifecycle import classify_pair

    from .gate_flags import store_state

    state = store_state(store)
    state["_r6_writes"] = state.get("_r6_writes", 0) + 1
    p = params()
    units: dict[str, list[tuple[str, str]]] = {}
    by_id = {str(r["id"]): r for r in others}
    stale = stale_enabled()
    common_box: list = []   # round 13 (cold first write): the common names only when a check needs them

    def common() -> frozenset:
        if not common_box:
            common_box.append(common_entities(others) if stale else frozenset())
        return common_box[0]
    linked = None
    if lifecycle.get("lifecycle") == "needs_review" and lifecycle.get("conflict_id"):
        rel = connection.execute("SELECT old_memory_id, reason FROM memory_relations WHERE id = ?",
                                 (lifecycle["conflict_id"],)).fetchone()
        if rel is not None:
            linked = str(rel[0])
            old = by_id.get(linked)
            if old is not None and new_confidence + 1e-9 >= float(old["confidence"]):
                if stale and (not (slot_reason(str(rel[1])) or subject_match(new_content, str(old["content"]), common()))
                              or dates_only_differ(new_content, str(old["content"])) or is_tentative(new_content)):
                    store._record_event(connection, "SKIP_SUPERSEDE", linked,
                                        f"Round 10: lifecycle review not used as extinction evidence: the review "
                                        f"('{rel[1]}') is not a same-slot value change and the notes share no slot or "
                                        "name, or only dates differ (separate occurrences), or the new note is only "
                                        "a plan or consideration.", source="gate:stale")
                else:
                    units.setdefault(linked, []).append(("a1 lifecycle review", f"lifecycle check: {rel[1]}"))
    replaced = lifecycle.get("replaced_memory_id")
    if replaced:
        linked = str(replaced)
        units.update(_duplicates_of(connection, linked, others, classify_pair))
    new_terms, new_words, _v = _shape(new_content)
    might_replace = bool(new_words & _CHANGE) or bool(_FROM_MARK.search(new_content))
    for row in (others if might_replace else ()):
        mid = str(row["id"])
        if mid == linked or mid == new_id or mid in units:
            continue
        if len(new_terms & _shape(str(row["content"]))[0]) < 2:
            continue
        if new_confidence + 1e-9 < float(row["confidence"]):
            continue
        why = replacement_evidence(new_content, str(row["content"]), p["FRAME_MIN"], common())
        if why:
            units.setdefault(mid, []).append(("a2 replacement", why))
    recent = _recent_recall(store)
    for mid, found in units.items():
        if mid in recent and any(k.startswith("a") for k, _e in found):
            found.append(("c retrieved then corrected",
                          f"it was returned by a recall within the last {int(p['RECALL_WINDOW'])} writes and then corrected"))
    out = []
    for mid, found in units.items():
        if _apply(store, connection, mid, new_id, found, stamp).get("extinguished"):
            out.append(mid)
    return out


def _duplicates_of(connection: Any, loser_id: str, others: list[Any], classify_pair: Any) -> dict[str, list]:
    loser = connection.execute("SELECT content FROM memories WHERE id = ?", (loser_id,)).fetchone()
    if loser is None:
        return {}
    text = str(loser[0])
    terms = _shape(text)[0]
    found: dict[str, list] = {}
    for row in others:
        mid = str(row["id"])
        if mid == loser_id or len(_shape(str(row["content"]))[0] & terms) < 2:
            continue
        if classify_pair(str(row["content"]), text).get("action") == "duplicate":
            found[mid] = [("b loser's duplicate", f"near-duplicate of {loser_id}, which lost a resolved conflict")]
    return found














# --- round 10: "possibly stale" status claims ----------------------------------------------
#
# A memory can only be flagged if it is a STATUS claim (pending, unsigned, unconfirmed, installed,
# current version, ...). A durable fact (an allergy, a birthday, a preference, a decision) is never
# flagged, whatever its age. A flag is evidence-based and logged with its reason: either the
# claim's own uncommon entity has much newer activity (never the claim's keep-both partner), or
# the claim has gone STALE_DAYS without being reconfirmed (a duplicate write refreshes it). A flag
# only demotes the memory in find(); it never extinguishes, evicts or edits it, and a later
# reconfirmation (updated_at after the flag) voids it.

_MONTHS = ("january february march april may june july august september october november december "
           "jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday "
           "saturday sunday mon tue tues wed thu thur thurs fri sat sun today yesterday tomorrow tonight "
           "morning afternoon evening week weeks weekend month months year years day days").split()
@lru_cache(maxsize=1)
def _date_terms() -> frozenset:
    # lazy: gates._TERM_MAP needs storage fully imported (circular import at module load)
    return frozenset(t for t in (gates._TERM_MAP[w] for w in _MONTHS) if t)
_STATUS = _lazy_re("_STATUS", 
    r"\b(?:pending|unsigned|not\s+(?:yet\s+)?signed|unconfirmed|not\s+(?:yet\s+)?confirmed|awaiting|"
    r"waiting\s+(?:for|on)|still\s+(?:open|pending|waiting|unsigned|unconfirmed|not|in\s+progress|under)|"
    r"in\s+progress|on\s+hold|to\s+be\s+confirmed|tbc|tbd|tentative(?:ly)?|not\s+yet|yet\s+to|"
    r"installed|is\s+running|currently|current\s+version|is\s+on\s+version|as\s+of|outstanding|"
    r"under\s+review|in\s+review|open\s+(?:ticket|issue|question))\b", re.IGNORECASE)
_ENTITY = _lazy_re("_ENTITY", r"(?<![.!?]\s)(?<!^)\b([A-Z][\w&'-]{2,})")
_NOT_ENTITY = frozenset(w.capitalize() for w in _MONTHS) | frozenset(
    "The This That These Those There Then When What Which Who Why How And But For Not Our Your Their His Her Its "
    "Also Next Last New Old Please Note Use Uses Used Always Never Every Each Some Any All One Two Three".split())
STALE_STATE_FLAG = "flagged"
_INITIAL = _lazy_re("_INITIAL", r"(?:^|[.!?]\s+)([A-Z][\w&'-]{2,})")


@lru_cache(maxsize=16384)
def initials(content: str) -> frozenset[str]:
    """Capitalised words that open a sentence. Ambiguous on their own ("Lunch", "Quillfeather"); they
    count as a name only when another memory has the same word capitalised mid-sentence."""
    out = set()
    for m in _INITIAL.finditer(content):
        word = re.sub(r"'s$", "", m.group(1)).rstrip("'")
        if word not in _NOT_ENTITY:
            out.add(word.casefold())
    return frozenset(out)


def shared_entities(a: str, b: str) -> frozenset[str]:
    """Names in both texts, with at least one of the two using it mid-sentence."""
    ea, eb = entities(a), entities(b)
    return frozenset((ea & (eb | initials(b))) | (initials(a) & eb))


@lru_cache(maxsize=65536)
def is_status_claim(content: str) -> bool:
    return bool(_STATUS.search(content))


# Round 13 (reviewer A P2, "durable historical facts are flagged by age"). `is_status_claim` only says a status
# WORD occurs. Before a stale flag is raised the note must also read as a PRESENT status, lexically and
# conservatively (when unsure: not flagged).
#
# Round 14 (reviewer A finding 4, "status classification is too broad"). The unit is now a CLAUSE: a sentence
# (split at . ! ? ; and line breaks) split again at "but / whereas / although / though / however / while".
# Each status word in a clause is one of
#   * historical -- right after a past auxiliary or past modifier ("was installed", "had been pending",
#     "was previously pending", "used to be", "is no longer pending", "formerly pending"), or "installed"
#     in a clause that dates it ("in 1980");
#   * reported   -- inside double quotes, or after a reporting phrase in the same clause ("the archive
#     quotes", "said", "according to", ...): someone else's words, not a statement of the present;
#   * present    -- otherwise.
# A clause is present when at least one of its status words is present. status_kind is then
#   None         no status word at all;
#   "present"    EVERY clause is a present status clause (round 13 counted clauses that had any status
#                word, so "X was installed in 1980. The permit is pending." passed as present -- the bug);
#   "mixed"      at least one present status clause plus other clauses (other facts, history, quotes);
#   "uncertain"  no present clause, but a status word in quoted or reported speech;
#   "historical" no present clause, otherwise.
# Only "present" notes are ever demoted (age sweep or newer activity). A "mixed" or "uncertain" note
# whose present/reported clause names the subject of newer activity gets a REVIEW indication instead
# (gates_stale: shown with the note, nothing demoted). This is word-pattern matching, not understanding.
_SENTENCE = _lazy_re("_SENTENCE", r"(?<=[.!?;])\s+|\n+")
_CLAUSE = _lazy_re("_CLAUSE", r"\s*,?\s+\b(?:but|whereas|although|though|however|while)\b[\s,]+", re.IGNORECASE)
_PAST_AUX = _lazy_re("_PAST_AUX",
    r"(?:\b(?:was|were|wasn't|weren't|had\s+been|had|got|been|became|used\s+to\s+be)\s+"
    r"(?:(?:already|then|first|finally|originally|fully|all|still|previously|formerly|once|earlier|initially|"
    r"long|briefly|temporarily|only)\s+)?"
    r"|\b(?:no\s+longer|not\s+any\s+longer|previously|formerly|once|originally|initially)\s+(?:(?:been|be)\s+)?)$",
    re.IGNORECASE)
_DATED_PAST = _lazy_re("_DATED_PAST", r"\b(?:back\s+)?in\s+(?:(?:early|late|mid)\s+)?(?:1[89]\d\d|20\d\d)\b", re.IGNORECASE)
# Reporting VERBS only (round 14 dev fix: "report", "quotes", "claims", "reads" are also nouns -- "the audit
# report is under review" is a plain present status). Quoted words are caught by the quote marks themselves.
_REPORTED = _lazy_re("_REPORTED",
    r"\b(?:said|says|saying|wrote|writes|reported|stated|claimed|mentioned|quoted|quoting|according\s+to|"
    r"told\s+(?:me|us|him|her|them))\b", re.IGNORECASE)


def _in_quotes(clause: str, pos: int) -> bool:
    before = clause[:pos]
    return before.count('"') % 2 == 1 or before.count("\u201c") > before.count("\u201d")


def _word_kind(clause: str, m: Any) -> str:
    """'historical' / 'reported' / 'present' for one status-word match in a clause."""
    before = clause[: m.start()]
    if _PAST_AUX.search(before):
        return "historical"
    if m.group(0).casefold() == "installed" and _DATED_PAST.search(clause):
        return "historical"
    if _in_quotes(clause, m.start()) or _REPORTED.search(before):
        return "reported"
    return "present"


def _clause_kind(clause: str) -> str:
    """'none' / 'present' / 'reported' / 'historical' for one clause."""
    kinds = {_word_kind(clause, m) for m in _STATUS.finditer(clause)}
    if not kinds:
        return "none"
    # Shared tense across coordinated predicates is ambiguous to this lexical
    # classifier. Keep the whole note visible and eligible for review, not demotion.
    if "historical" in kinds and "present" in kinds:
        return "reported"
    for kind in ("present", "reported"):
        if kind in kinds:
            return kind
    return "historical"


def _clauses(content: str) -> list[str]:
    out = []
    for sentence in _SENTENCE.split(content.strip()):
        if sentence.strip():
            out.extend(c for c in _CLAUSE.split(sentence) if c.strip())
    return out


def _present_sentence(sentence: str) -> bool:   # kept for callers: any present status clause in it
    return any(_clause_kind(c) == "present" for c in _clauses(sentence))


@lru_cache(maxsize=65536)
def _status_parts(content: str) -> tuple[tuple[str, ...], tuple[str, ...], int, int]:
    """(present status clauses, reported status clauses, clauses with a status word, all clauses)."""
    present: list[str] = []
    reported: list[str] = []
    with_status = total = 0
    for clause in _clauses(content):
        total += 1
        kind = _clause_kind(clause)
        if kind != "none":
            with_status += 1
        if kind == "present":
            present.append(clause)
        elif kind == "reported":
            reported.append(clause)
    return tuple(present), tuple(reported), with_status, total


def status_kind(content: str) -> str | None:
    present, reported, with_status, total = _status_parts(content)
    if not with_status:
        return None
    if present:
        return "present" if len(present) == total else "mixed"
    return "uncertain" if reported else "historical"


def present_status_names(content: str) -> frozenset[str]:
    """Names (entities or clause-opening capitalised words) in the note's PRESENT status clauses: the
    subject(s) the present part is about. Empty for historical / uncertain notes."""
    out: set[str] = set()
    for clause in _status_parts(content)[0]:
        out |= entities(clause) | initials(clause)
    return frozenset(out)


def review_status_names(content: str) -> frozenset[str]:
    """Round 14: names in the present AND the reported status clauses (what a review indication is about)."""
    present, reported, _w, _t = _status_parts(content)
    out: set[str] = set()
    for clause in present + reported:
        out |= entities(clause) | initials(clause)
    return frozenset(out)


def _entities_uncached(content: str) -> frozenset[str]:
    out = set()
    for m in _ENTITY.finditer(content):
        word = re.sub(r"'s$", "", m.group(1)).rstrip("'")
        if len(word) >= 3 and word not in _NOT_ENTITY:
            out.add(word.casefold())
    return frozenset(out)


# Round 14 (reviewer A finding 3): round 13 stacked a second lru_cache on top of the original one, and
# clearing the outer layer left the inner one holding forgotten text. There is now exactly ONE cache
# layer (`_entities_cached`); `entities` is a plain pass-through whose `__wrapped__` is that layer, and
# `_LRU_TEXT_CACHES` follows every `__wrapped__` chain so no layer can be missed again.
_entities_cached = lru_cache(maxsize=65536)(_entities_uncached)   # by text, in memory only


def entities(content: str) -> frozenset[str]:
    """Proper names in the text (capitalised words not opening a sentence), months/days excluded."""
    return _entities_cached(content)


entities.__wrapped__ = _entities_cached          # type: ignore[attr-defined]
entities.cache_info = _entities_cached.cache_info    # type: ignore[attr-defined]
entities.cache_clear = _entities_cached.cache_clear  # type: ignore[attr-defined]


def keep_both_pair(connection: Any, a: str, b: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM memory_relations WHERE decision = 'KEEP_BOTH' AND "
        "((old_memory_id = ? AND new_memory_id = ?) OR (old_memory_id = ? AND new_memory_id = ?)) LIMIT 1",
        (a, b, b, a)).fetchone() is not None


def common_entities(others: list[Any]) -> frozenset[str]:
    """Names held by more than max(3, ENTITY_DF_MAX * N) memories (the user's own name, the product
    name...). Too common to say two memories are about the same thing."""
    limit = max(3.0, params()["ENTITY_DF_MAX"] * len(others))
    df: dict[str, int] = {}
    for r in others:
        for e in entities(str(r["content"])):
            df[e] = df.get(e, 0) + 1
    return frozenset(e for e, n in df.items() if n > limit)


# Lifecycle review reasons that DO say "same slot, new value" (lifecycle.classify_pair). Every other
# review reason (value not explicit, different subjects, separate dates/installations, two versions
# both active) is not supersession evidence under round 10.
_SLOT_REASONS = frozenset({
    "A later fact says the value changed from the one this memory holds.",
    "A later fact states a different value for the same slot.",
    "A later fact names a different version of the same thing and says it replaces the old one.",
    "Negation changed; confirm which statement is valid.",
})
_SLOT_REASON_PREFIX = "A later fact gives a different value for the same subject"


def slot_reason(reason: str) -> bool:
    return reason in _SLOT_REASONS or reason.startswith(_SLOT_REASON_PREFIX)


def subject_match(new_content: str, old_content: str, common: frozenset = frozenset()) -> bool:
    """Round 10 supersession precondition (a slot or entity match). Shared dates or numbers alone never
    qualify. Matches when the two notes share:
      - an explicit "from X" naming a non-date word of the old note, or
      - an uncommon proper name, or
      - a typed slot other than a date (time, version, amount...), or
      - a date slot or a common name (the user's own, the product's) AND at least two other subject
        words that are neither dates nor names ("the X deadline is March 3" / "slipped to May 2")."""
    dt = _date_terms()
    old_terms = _shape(old_content)[0] - dt
    if any(gates._TERM_MAP[m.group(1).casefold()] in old_terms for m in _FROM_MARK.finditer(new_content)):
        return True
    names = shared_entities(new_content, old_content)
    if names - common:
        return True
    from .lifecycle import extract_values

    kinds = set(extract_values(new_content)[0]) & set(extract_values(old_content)[0])
    if kinds - {"date"}:
        return True
    if not (kinds or names):
        return False
    every_name = {gates._TERM_MAP[w] for w in entities(new_content) | entities(old_content)
                  | initials(new_content) | initials(old_content)}
    words = (_shape(new_content)[0] & old_terms) - every_name
    return len(words) >= 2


def dates_only_differ(new_content: str, old_content: str) -> bool:
    """True when the two notes say the same words and differ only in dates/numbers: separate
    occurrences ("coffee with X on 3 May" / "on 9 June"), not a replacement."""
    dt = _date_terms()
    return (_shape(new_content)[0] - dt) == (_shape(old_content)[0] - dt)


_TENTATIVE = _lazy_re("_TENTATIVE", r"\b(evaluat\w*|consider(?:s|ing)?|might|maybe|perhaps|plan(?:s|ning)? to|thinking (?:about|of)|"
                        r"propos(?:e|es|ed|al)|should we|whether)\b", re.I)


def is_tentative(content: str) -> bool:
    """A plan, proposal or consideration ("evaluate 2.0", "might move to..."). It does not say the
    current state changed, so it is not supersession evidence."""
    return bool(_TENTATIVE.search(content))


# --- round 14: lazily loaded part (cold first write) ----------------------------------------------------
# reviewer B measured the gated cold first write on a slower Mac at 1.65-2.06x 0.4.19; every line of a gate
# module is compiled (or unmarshalled) and executed when a gated process opens a store. The parts no write
# needs -- the round-13 snapshot ranking (the streamed ranking falls back to it), the reference ranking, the GOAL2 helpers, and the restore / resolve / weaken / find-order helpers -- live in `gates_r6_lazy.py` and are executed INTO THIS MODULE'S NAMESPACE on first use (same
# globals, so behaviour, patching and the text-cache clearing are unchanged); the bytecode is cached like any
# module's.
_LAZY_NAMES = frozenset({'_goal2_prepare', '_goal2_state', '_goal_profiles', '_goal_tag_live', '_is_head', '_rank_useful_book', 'after_resolution', 'apply_order', 'order_pairs', 'pending_weakening', 'rank_useful_full', 'undo_weakening', 'weaken'})
_LAZY_LOADED = False


def _load_lazy() -> None:
    global _LAZY_LOADED
    if not _LAZY_LOADED:
        import importlib.util

        spec = importlib.util.find_spec(f"{__package__}.gates_r6_lazy")
        exec(spec.loader.get_code(spec.name), globals())   # noqa: S102 -- our own module file
        _LAZY_LOADED = True


def __getattr__(name: str) -> Any:
    if name in _LAZY_NAMES and not _LAZY_LOADED:
        _load_lazy()
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
