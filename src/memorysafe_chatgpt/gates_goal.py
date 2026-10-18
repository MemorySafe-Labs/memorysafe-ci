"""Round 11 (opt-in, MEMORYSAFE_GATE_GOAL2): goal and "that mattered" signals from explicit
evidence only. PROTOTYPE, default off. Inspired by (not a model of) the idea that what is kept
under a budget is what a goal or an outcome marked as mattering.

With the flag on, the round-7 word-overlap goal rule (_goal_related: >= 2 shared content words
with a goal memory) is NOT used. A memory counts as goal-related only when:

  (1) it is itself a goal memory ("Goal:", "Mission:", "Objective:", "#goal", "#mission"), or
  (2) the caller tagged it for an active goal (remember(..., tag="goal:<goal memory id>")), or
  (3) entity membership: it names an UNCOMMON proper name of an active goal memory (a name in more
      than max(5, GOAL_DF_MAX x active) memories -- the user's own name -- never counts), AND it
      names no other uncommon proper name and no other-party word ("cousin", "rival", "another
      company" ...): a fact about someone or something else is not this goal's, AND it is not a
      passing remark (read / saw / heard ...). With GOAL_KEYWORD=1 it must also share one
      non-name key word of the goal.

And a memory "mattered" (its own weight W_MATTER, also a sign of use) when:
  (4) the caller tagged it: remember(..., tag="mattered") or mark_mattered(store, id), or
  (5) it was restated at least RECONF_MIN times (exact or near-duplicate merges: explicit
      reconfirmation, never time).

Hazard words keep the round-7 W_HAZ weight. Age is never an input.

Storage: a tag is one decision_events row (decision GOAL_TAG, the memory id, and a reason naming
only "mattered" or the goal memory's id). No memory text is written anywhere new; the in-process
cache holds ids and counts only and is never persisted.
"""

from __future__ import annotations

import re
from typing import Any

from . import gates

TAG_EVENT = "GOAL_TAG"
_GOAL_TAG = re.compile(r"^goal:([\w-]{1,80})$")
_OTHER_PARTY = re.compile(
    r"\b(?:cousin|friend|friends|neighbou?r|neighbou?rs|sister|brother|uncle|aunt|colleague|teammate|"
    r"rival|competitor|another|other|former|different|someone|somebody)\b", re.IGNORECASE)


def enabled() -> bool:
    return gates._flag("MEMORYSAFE_GATE_GOAL2")


def parse_tag(tag: str | None) -> str | None:
    """"mattered" | "goal:<id>" -> the reason stored; anything else is refused (ValueError)."""
    if tag is None:
        return None
    tag = tag.strip()
    if tag == "mattered":
        return "caller tag: mattered"
    m = _GOAL_TAG.match(tag)
    if m:
        return f"caller tag: goal {m.group(1)}"
    raise ValueError('tag must be "mattered" or "goal:<goal memory id>"')


def record_tag(store: Any, connection: Any, memory_id: str, tag: str | None) -> None:
    reason = parse_tag(tag)
    if reason is None:
        return
    store._record_event(connection, TAG_EVENT, memory_id, reason, source="gate:goal2")
    sig = _signals_cache(store)
    if sig.loaded:                 # otherwise the next load reads it from the database
        sig.note_tag(memory_id, reason)


def note_merge(store: Any, memory_id: str) -> None:
    sig = _signals_cache(store)
    if sig.loaded:
        sig.merges[memory_id] = sig.merges.get(memory_id, 0) + 1


def mark_mattered(store: Any, memory_id: str) -> dict[str, Any]:
    """Explicit "that mattered" signal after the fact (for example, the fact answered a question the
    user acted on). Only an id is recorded."""
    with store._session() as connection:
        row = connection.execute("SELECT state FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return {"memory_id": memory_id, "tagged": False, "reason": "No memory with that id."}
        record_tag(store, connection, memory_id, "mattered")
    return {"memory_id": memory_id, "tagged": True, "reason": "Marked as mattered (raises its keep priority)."}


class _Signals:
    """Per database, in process memory only: tag reasons and restatement counts by memory id."""

    def __init__(self) -> None:
        self.loaded = False
        self.mattered: set[str] = set()
        self.goal_tags: dict[str, set[str]] = {}
        self.merges: dict[str, int] = {}

    def load(self, connection: Any) -> None:
        self.mattered.clear()
        self.goal_tags.clear()
        self.merges.clear()
        for mid, reason in connection.execute(
                "SELECT memory_id, reason FROM decision_events WHERE decision = ?", (TAG_EVENT,)).fetchall():
            self.note_tag(str(mid), str(reason))
        for mid, n in connection.execute(
                "SELECT memory_id, COUNT(*) FROM decision_events WHERE decision = 'MERGE' AND memory_id IS NOT NULL "
                "GROUP BY memory_id").fetchall():
            self.merges[str(mid)] = int(n)
        self.loaded = True

    def note_tag(self, mid: str, reason: str) -> None:
        if reason == "caller tag: mattered":
            self.mattered.add(mid)
        elif reason.startswith("caller tag: goal "):
            self.goal_tags.setdefault(mid, set()).add(reason[len("caller tag: goal "):])



def _signals_cache(store: Any) -> _Signals:
    """Kept on the usefulness gate's per-database cache, so a full re-read of the active rows
    (another process wrote) also re-reads the signals."""
    from .gates_r6 import _feature_cache

    fc = _feature_cache(store)
    if getattr(fc, "sig", None) is None:
        fc.sig = _Signals()
    return fc.sig


def signals(store: Any, connection: Any, reload: bool = False) -> _Signals:
    s = _signals_cache(store)
    if reload or not s.loaded:
        s.load(connection)
    return s


def profiles(goal_texts: dict[str, str], df_tokens: dict[str, int], limit: float, frame: frozenset,
             key_terms: dict[str, frozenset]) -> list[tuple[str, frozenset, frozenset]]:
    """For each active goal memory: (goal id, its uncommon names, its non-name key words)."""
    from .gates_r6 import entities, initials

    out = []
    for gid, text in goal_texts.items():
        names = frozenset(n for n in (entities(text) | initials(text)) if df_tokens.get(n, 0) <= limit)
        name_terms = frozenset().union(*(gates.terms_of(n) for n in names)) if names else frozenset()
        keys = frozenset(key_terms[gid] - frame - name_terms)
        out.append((gid, names, keys))
    return out


def classify(mid: str, content: str, feat: tuple, goal_profiles: list, common_names: frozenset,
             sig: _Signals, p: dict, incidental: frozenset) -> tuple[bool, bool]:
    """(goal memory or entity member, mattered) for one memory: rules (1) and (3); tags are not read here."""
    from .gates_r6 import entities, initials

    mattered = mid in sig.mattered or sig.merges.get(mid, 0) >= p["RECONF_MIN"]
    if feat[2]:
        return True, mattered
    # (caller tags are handled by the caller of this function, gates_r6._goal_signal; this is the
    # entity-membership rule only)
    if not p["GOAL_ENTITY"] or not goal_profiles or (p["GOAL_INCIDENTAL"] and feat[5] & incidental):
        return False, mattered
    names = (entities(content) | initials(content)) - common_names
    if not names:
        return False, mattered
    for _gid, gnames, gkeys in goal_profiles:
        hit = names & gnames
        if not hit:
            continue
        if names - gnames - _initials_only(content, names):
            continue                       # names someone or something else as well
        if _OTHER_PARTY.search(content):
            continue                       # about another person or organisation
        if p["GOAL_KEYWORD"] and not (feat[3] & gkeys):
            continue
        return True, mattered
    return False, mattered


def _initials_only(content: str, names: frozenset) -> frozenset:
    """Sentence-opening capitalised words that are never used as a name mid-sentence in this text:
    'Registration', 'Movers', 'Visa' ... are not names, so they do not make a fact 'about someone else'."""
    from .gates_r6 import entities, initials

    return frozenset(n for n in initials(content) if n not in entities(content)) & names
