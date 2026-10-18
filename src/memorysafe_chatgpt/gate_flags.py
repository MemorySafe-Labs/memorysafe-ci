"""Round 13: the light part of the opt-in gates -- on/off checks and the sidecar cleanup -- so that
storage (and the MCP server) need not import the gate modules when no gate is on."""

from __future__ import annotations

import glob
import os
import sys
from typing import Any

_TRUE = frozenset({"1", "true", "yes", "on"})


def flag(name: str) -> bool:
    """Same rule as gates._flag."""
    return os.environ.get(name, "").strip().lower() in _TRUE


def loaded(module: str) -> bool:
    return f"{__package__}.{module}" in sys.modules


def remove_sidecar(database_path: Any) -> int:
    """Upgrade/cleanup step: delete `<database>.gates-index` and any `<database>.gates-index.<pid>.tmp`
    that a round-4..12 prototype left next to the database (they could hold normalised memory text).
    Called whenever a store is opened and after forget, flags on or off. Returns how many files were
    removed; never raises. Round 14 (reviewer A): a file that could NOT be removed is no longer passed over
    in silence -- see `remove_sidecar_report`, which storage uses and which warns."""

    return remove_sidecar_report(database_path)[0]


def remove_sidecar_report(database_path: Any) -> tuple[int, list[str]]:
    """(files removed, files still there after the attempt). Never raises."""

    if not database_path:
        return 0, []
    base = f"{database_path}.gates-index"
    removed = 0
    left: list[str] = []
    for path in [base, *glob.glob(glob.escape(base) + ".*.tmp")]:
        try:
            os.unlink(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            if os.path.lexists(path):
                left.append(path)
    return removed, left


def warn_sidecar_left(left: list[str]) -> None:
    import warnings

    warnings.warn("MemorySafe could not delete an old gate sidecar file (it may hold normalised memory "
                  "text); delete it by hand: " + ", ".join(left), RuntimeWarning, stacklevel=3)


def gates_loaded() -> bool:
    return f"{__package__}.gates" in sys.modules or f"{__package__}.gates_r6" in sys.modules


_KEPT_STORE_STATE = ("_r6_writes", "_r10_last_sweep", "_r6_last_recall")
_MISSING = object()
_SHARED_STATE: "dict[str, dict]" = {}
_SHARED_KEEP = 1024


def store_state(store: Any) -> dict:
    """Where the gates keep their small per-store counters (`_KEPT_STORE_STATE`: writes seen, the last
    recall, the last stale sweep). Locally that is the store object itself, as before. A server backend
    builds a new store for every request, so there they would start empty on every call: the "recalled,
    then corrected" evidence could never be seen and the once-a-day stale sweep ran on every write. For
    such a store they live in this process, keyed like the caches (`cache_key`: database, user, layers);
    numbers and memory ids only, never text."""

    if getattr(store, "database_path", None) is not None or not callable(
            getattr(getattr(store, "_backend", None), "gate_cache_identity", None)):
        return store.__dict__
    key = cache_key(store)
    state = _SHARED_STATE.pop(key, None)
    if state is None:
        state = {}
        while len(_SHARED_STATE) >= _SHARED_KEEP:
            _SHARED_STATE.pop(next(iter(_SHARED_STATE)))
    _SHARED_STATE[key] = state   # most recently used last
    return state


def guarded_session(store: Any) -> Any:
    """Round 14 (reviewer A finding 1): the store's session, plus -- when this session ends in an exception,
    i.e. the transaction was rolled back or its commit failed -- every in-process gate cache of this
    database is dropped and the store's gate counters are put back, so nothing cached can reflect a
    write that never happened. (Caches are otherwise brought up to date from the database change log,
    which only ever holds committed changes.) Only used once a gate module is loaded."""

    from contextlib import contextmanager

    @contextmanager
    def session():
        state = store_state(store)
        saved = {name: state.get(name, _MISSING) for name in _KEPT_STORE_STATE}
        try:
            with store._backend.session() as connection:
                yield connection
        except BaseException:
            discard_gate_caches(store)
            for name, value in saved.items():
                if value is _MISSING:
                    state.pop(name, None)
                else:
                    state[name] = value
            raise

    return session()


def cache_key(store: Any) -> str:
    """Which of this process's gate caches belong to `store`.

    Locally that is the database file, as it always was. A server backend serves many users from one
    process, and every one of its stores has the same `database_path` (None), so a key built from the path
    alone would hand one user's cached rows to the next. Such a backend says who and what a store can see
    through `gate_cache_identity()` (for the hosted edition: the database, the user, and the layers the
    store reads and writes), and the key is built from that. A store with neither shares nothing."""

    backend = getattr(store, "_backend", None)
    identity = getattr(backend, "gate_cache_identity", None)
    if callable(identity):
        return "backend:" + "|".join(str(part) for part in identity())
    path = getattr(store, "database_path", None)
    if path is not None:
        return str(path)
    return f"store:{id(store)}"


def rank_only(store: Any) -> bool:
    """True for a store whose backend never lets a gate move a memory out of recall on its own (the
    hosted edition never evicts). The gates then only score and rank: no capacity eviction, and
    extinction weakens a memory (it ranks lower) without extinguishing it."""

    return bool(getattr(getattr(store, "_backend", None), "gates_rank_only", False))


# Settings that make a gate move memories out of recall by capacity. A process that must never evict
# (the hosted edition) refuses to start with any of them, as it does with MEMORYSAFE_MAX_ACTIVE.
_EVICTING_SETTINGS = ("MEMORYSAFE_GATE_MAX_ACTIVE",)


def evicting_gate_settings() -> list[str]:
    """The gate capacity settings in the environment that would evict: a positive integer. (Unset, 0
    or anything else enforces no gate capacity, the same rule gates.gate_capacity applies.)"""

    found = []
    for name in _EVICTING_SETTINGS:
        raw = os.environ.get(name, "").strip()
        try:
            if raw and int(raw) > 0:
                found.append(name)
        except ValueError:
            pass
    return found


def discard_gate_caches(store: Any) -> None:
    """Drop this process's gate caches of `store`'s rows (they are rebuilt from committed rows)."""

    key = cache_key(store)
    gates = sys.modules.get(f"{__package__}.gates")
    if gates is not None:
        with gates._INDEX_LOCK:
            gates._INDEXES.pop(key, None)
    gates_r6 = sys.modules.get(f"{__package__}.gates_r6")
    if gates_r6 is not None:
        gates_r6.discard_cache(key)


_GATE_FLAGS = ("MEMORYSAFE_GATE_CAPACITY", "MEMORYSAFE_GATE_SURPRISE", "MEMORYSAFE_GATE_CONSOLIDATE",
               "MEMORYSAFE_GATE_USEFUL", "MEMORYSAFE_GATE_EXTINCTION", "MEMORYSAFE_GATE_STALE",
               "MEMORYSAFE_GATE_GOAL2")


def any_gate_on() -> bool:
    return any(flag(name) for name in _GATE_FLAGS)


def preload() -> None:
    import importlib

    # round 13: only what the write path of the flags that are on needs; find's ordering module and the
    # goal module (MEMORYSAFE_GATE_GOAL2) load on first use through storage's lazy proxies
    modules = ["gates", "gates_r6"]
    if flag("MEMORYSAFE_GATE_STALE"):
        modules.append("gates_stale")
    if flag("MEMORYSAFE_GATE_GOAL2"):
        modules.append("gates_goal")
    for module in modules:
        importlib.import_module(f"{__package__}.{module}")
