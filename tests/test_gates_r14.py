"""Round 14 regression tests: one or more per finding of reviewer A's r13 review (REVIEWER_A_GATES_R13_REVIEW_2026-10-11)
and reviewer B's r13 check (REVIEWER_B_GATES_R13_CHECK_2026-10-11). Each first reproduces the reviewer's exact
probe. Synthetic temporary stores only. All gates stay default-off outside these tests."""

import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import warnings
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import gate_flags, gates, gates_r6, gates_stale, storage
from memorysafe_chatgpt.storage import MemoryStore

from test_gates_r13 import CANDIDATE, SRC, WORDS, Case

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / "fixtures" / "lumi_r13"


# The reviewer's probes are byte copies, so they are not edited. They open `with sqlite3.connect(...)`,
# which commits but does not close, and one keeps that connection in a module global until the end.
# Windows cannot delete a temp dir holding an open database, so the runner records every connection
# the probe opens and closes them all before any temp dir is cleaned up.
_PROBE_RUNNER = """
import runpy, sqlite3, sys, tempfile
_opened, _connect = [], sqlite3.connect
def connect(*args, **kwargs):
    connection = _connect(*args, **kwargs)
    _opened.append(connection)
    return connection
def close_all():
    while _opened:
        _opened.pop().close()
sqlite3.connect = connect
_cleanup, _exit = tempfile.TemporaryDirectory.cleanup, tempfile.TemporaryDirectory.__exit__
def cleanup(self):
    close_all()
    return _cleanup(self)
def __exit__(self, *exc):
    close_all()
    return _exit(self, *exc)
tempfile.TemporaryDirectory.cleanup, tempfile.TemporaryDirectory.__exit__ = cleanup, __exit__
runpy.run_path(sys.argv[1], run_name="__main__")
"""


def _run(script: Path, extra_env: dict | None = None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
    env.update({"PYTHONPATH": SRC, **(extra_env or {})})
    p = subprocess.run([sys.executable, "-c", _PROBE_RUNNER, str(script)], env=env, capture_output=True,
                       text=True, encoding="utf-8", timeout=300)
    if p.returncode:
        raise AssertionError(p.stderr[-3000:])
    return p.stdout


# --- reviewer A's edge probes, UNCHANGED (tests/fixtures/lumi_r13/edge_probes.py is a byte copy) ---------------

class ReviewerAEdgeProbeTests(unittest.TestCase):
    def test_edge_probes_pass_unchanged(self):
        out = dict(line.split(" ", 1) for line in _run(EVIDENCE / "edge_probes.py").strip().splitlines()
                   if not line.startswith("STATUS"))
        status = [line for line in _run(EVIDENCE / "edge_probes.py").splitlines() if line.startswith("STATUS")]
        self.assertEqual(out["CONTENT_ONLY_CACHE_STALE"], "False")
        self.assertTrue(out["ROLLBACK_CACHE_STALE"].startswith("False"))
        self.assertEqual(out["INNER_ENTITY_CACHE_RETAINS_FORGOTTEN_TEXT"], "False")
        # kept on purpose and documented (GATES_CHANGELOG_LIFECYCLE.md): a database with the log keeps
        # logging when one client turns the flags off, so other gate-enabled clients stay correct
        self.assertEqual(out["LOG_WRITES_AFTER_FLAGS_DISABLED"], "True")
        kinds = [line.rsplit(" ", 1)[1] for line in status]
        self.assertEqual(kinds, ["mixed", "uncertain", "historical"])

    def test_original_r10_probes_still_pass(self):
        for line in _run(EVIDENCE / "original_r10_probes.py").strip().splitlines():
            self.assertTrue(line.endswith(" False"), line)


# --- Finding 1: rollback ------------------------------------------------------------------------------

class RollbackTests(Case):
    """A failed write must leave no cache reflecting it: after an injected failure on the REAL remember and
    eviction paths, the next writes decide exactly as a fresh store opened on the committed database."""

    env = {**CANDIDATE, "MEMORYSAFE_GATE_MAX_ACTIVE": "12"}

    def fill(self, n=14, seed=3):
        rng = random.Random(seed)
        for i in range(n):
            self.put(f"{rng.choice(WORDS)} {rng.choice(WORDS)} note {i} about the {rng.choice(WORDS)} shelf",
                     rng.choice(["other", "task", "project"]), importance=rng.choice([.2, .3, .5]))

    def oracle(self) -> MemoryStore:
        copy = self.dir / "oracle.sqlite3"
        src, dst = sqlite3.connect(self.db), sqlite3.connect(copy)
        src.backup(dst)
        src.close()
        dst.close()
        return MemoryStore(copy)

    @staticmethod
    def active(store):
        with store._session() as c:
            return sorted(r[0] for r in c.execute("SELECT content FROM memories WHERE state = 'active'"))

    def compare_next(self, oracle, k=6):
        rng = random.Random(99)
        for i in range(k):
            text = f"follow-up {i}: the {rng.choice(WORDS)} {rng.choice(WORDS)} list"
            self.later()
            a = self.store.remember(text, "other", importance=.25)
            b = oracle.remember(text, "other", importance=.25)
            self.assertEqual(a["decision"], b["decision"])
            self.assertEqual(self.active(self.store), self.active(oracle))

    def _fail_after(self, module, name):
        real = getattr(module, name)

        def boom(*a, **k):
            real(*a, **k)
            raise RuntimeError("injected failure")
        return patch.object(module, name, boom)

    def test_failure_inside_eviction_is_rolled_back_everywhere(self):
        self.fill()
        before = self.active(self.store)
        with self._fail_after(gates, "_move_out"), self.assertRaises(RuntimeError):
            self.put("the injected write that fails while evicting", "other", importance=.2)
        self.assertEqual(self.active(self.store), before)          # the database rolled back ...
        self.compare_next(self.oracle())                            # ... and so did every cache

    def test_failure_after_extinction_step_is_rolled_back(self):
        self.fill()
        with self._fail_after(gates_r6, "after_write"), self.assertRaises(RuntimeError):
            self.put("the injected write that fails after the extinction step", "other", importance=.2)
        self.compare_next(self.oracle())

    def test_failure_on_snapshot_path_is_rolled_back(self):
        with patch.dict(os.environ, {"MEMORYSAFE_GATE_R14_STREAM": "0"}):
            self.fill()
            with self._fail_after(gates, "_move_out"), self.assertRaises(RuntimeError):
                self.put("the injected write that fails on the snapshot path", "other", importance=.2)
            self.compare_next(self.oracle())

    def test_failed_commit_drops_caches_and_restores_counters(self):
        self.fill()
        fc = gates_r6._feature_cache(self.store)
        self.store._r6_writes = 41
        with self.assertRaises(RuntimeError), self.store._session() as c:
            c.execute("UPDATE memories SET category = 'project' WHERE rowid = 1")
            self.store._r6_writes = 99
            raise RuntimeError("abort")
        self.assertIsNone(fc.book)
        self.assertEqual(fc.by_id, {})
        self.assertEqual(self.store._r6_writes, 41)


# --- Finding 2: content-only change -------------------------------------------------------------------

class ContentOnlyTests(Case):
    def test_content_only_update_drops_text_features(self):
        a = self.put("Goal: launch the amber notebook", importance=.2)
        fc = gates_r6._feature_cache(self.store)
        with self.store._session() as c:
            book = gates_r6._snapshot(c, fc, None)
            gates_r6._feats(fc, c, [a], book.rows)
            self.assertTrue(fc.by_id[a][1][2])
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE memories SET content = ? WHERE id = ?", ("Goal launch the amber notebook", a))
        with self.store._session() as c:
            book = gates_r6._snapshot(c, fc, None)
            gates_r6._feats(fc, c, [a], book.rows)
            self.assertEqual(fc.by_id[a][1], gates_r6.features("Goal launch the amber notebook", book.rows[a][2]))

    def test_streamed_path_also_follows_content_only_updates(self):
        a = self.put("Goal: launch the amber notebook", importance=.2)
        self.put("another note so the log is installed", importance=.2)
        fc = gates_r6._feature_cache(self.store)
        with self.store._session() as c:
            gates.ensure_change_log(c)
        with self.store._session() as c:
            gates_r6._stream_sync(fc, c, None)
            row = c.execute("SELECT rowid, id, normalized_content FROM memories WHERE id = ?", (a,)).fetchone()
            gates_r6._feats(fc, c, [a], {a: tuple(row) + ("other", .2, 0, 0)})
            fc.rid2mid[row[0]] = a
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE memories SET content = ? WHERE id = ?", ("Goal launch the amber notebook", a))
        with self.store._session() as c:
            gates_r6._stream_sync(fc, c, None)
        self.assertNotIn(a, fc.by_id)


# --- Finding 3: every cache layer is cleared ----------------------------------------------------------

class CacheLayerTests(Case):
    def test_no_layer_keeps_forgotten_text(self):
        a = self.put("Goal launch the amber notebook with Petra")
        gates_r6.entities("Goal launch the amber notebook with Petra")
        self.store.forget(a, confirm=True)
        for function in gates_r6._LRU_TEXT_CACHES():
            self.assertEqual(function.cache_info().currsize, 0, function)
        layer, depth = gates_r6.entities, 0
        while layer is not None and depth < 8:          # walk the decorator chain explicitly
            if hasattr(layer, "cache_info"):
                self.assertEqual(layer.cache_info().currsize, 0)
            layer, depth = getattr(layer, "__wrapped__", None), depth + 1

    def test_entities_has_exactly_one_cache_layer(self):
        layers = []
        layer = gates_r6.entities
        while layer is not None:
            if hasattr(layer, "cache_info") and getattr(layer, "cache_info") not in [x.cache_info for x in layers]:
                layers.append(layer)
            layer = getattr(layer, "__wrapped__", None)
        self.assertEqual(len({id(x.cache_info.__self__) if hasattr(x.cache_info, "__self__") else id(x)
                              for x in layers}), 1)


# --- Finding 4: status classification ------------------------------------------------------------------

class StatusClassifierTests(Case):
    def test_reviewer_a_exact_cases(self):
        self.assertEqual(gates_r6.status_kind("The furnace was installed in 1980. The permit is pending."), "mixed")
        self.assertEqual(gates_r6.status_kind('The archive quotes "The permit is pending".'), "uncertain")
        self.assertEqual(gates_r6.status_kind("The permit was previously pending."), "historical")

    def test_more_shapes(self):
        cases = {
            "The furnace was installed in 1980 but the permit is pending.": "mixed",
            "Bob said the permit is pending.": "uncertain",
            "According to the clerk, the permit is pending.": "uncertain",
            "The permit is no longer pending.": "historical",
            "The permit used to be pending.": "historical",
            "The permit had previously been pending.": "historical",
            "The permit was formerly on hold.": "historical",
            "The permit is pending. The fee is unpaid and still outstanding.": "present",
            "Contract with Acme is pending signature.": "present",
            "Lena loves hiking in the Alps.": None,
        }
        for text, kind in cases.items():
            self.assertEqual(gates_r6.status_kind(text), kind, text)

    def _age_sweep(self, text):
        mid = self.put(text)
        self.later(days=60)
        self.store._r10_last_sweep = None
        self.sweeps = getattr(self, "sweeps", 0) + 1
        self.put(f"A violet umbrella number {WORDS[self.sweeps]} hangs beside the entrance")
        with self.store._session() as c:
            return mid in gates_stale.stale_ids(c)

    def test_nouns_are_not_reporting_verbs(self):
        # dev-found bug: "report"/"claims" as nouns made plain present statuses look reported
        for text in ("The audit report is under review as of 12 March 2026", "The insurance claims are pending."):
            self.assertEqual(gates_r6.status_kind(text), "present", text)
        for text in ("Bob said the permit is pending.", "According to the clerk, the permit is pending."):
            self.assertEqual(gates_r6.status_kind(text), "uncertain", text)

    def test_mixed_quoted_past_never_age_demoted(self):
        for text in ["The furnace was installed in 1980. The permit is pending.",
                     'The archive quotes "The permit is pending".', "The permit was previously pending.",
                     "The furnace was installed in 1980 but the permit is pending."]:
            self.assertFalse(self._age_sweep(text), text)
        self.assertTrue(self._age_sweep("The Quarry lease renewal is still pending"))

    def test_mixed_note_gets_review_not_demotion_on_newer_activity(self):
        mixed = self.put("The Halvorsen permit is pending. Our architect is Ines.")
        present = self.put("The Brandt permit is still pending")
        self.later(days=30)
        self.put("Called Halvorsen and Brandt about the drawings")
        with self.store._session() as c:
            stale, reviews = gates_stale.stale_ids(c), gates_stale.status_reviews(c)
        self.assertNotIn(mixed, stale)
        self.assertIn(mixed, reviews)
        self.assertIn("halvorsen", reviews[mixed])
        self.assertIn(present, stale)
        found = {m["memory_id"]: m for m in self.store.find("Halvorsen permit", limit=5)}
        self.assertIsNotNone(found[mixed]["status_review"])
        self.assertFalse(found[mixed]["possibly_stale"])

    def test_review_is_cleared_by_restating(self):
        mixed = self.put("The Halvorsen permit is pending. Our architect is Ines.")
        self.later(days=30)
        self.put("Called Halvorsen about the drawings")
        with self.store._session() as c:
            n = gates_stale.clear_stale(self.store, c, mixed, self.clock())
            self.assertEqual(n, 1)
            self.assertNotIn(mixed, gates_stale.status_reviews(c))


# --- change-log lifecycle ------------------------------------------------------------------------------

class ChangeLogLifecycleTests(Case):
    env = {**CANDIDATE, "MEMORYSAFE_GATE_MAX_ACTIVE": "4"}

    def status(self):
        with self.store._session() as c:
            return gates.change_log_status(c)

    def test_install_on_second_gated_write_of_a_process(self):
        self.put("first")
        self.assertFalse(self.status()["table"])          # a one-write process never installs it
        self.put("second")
        st = self.status()
        self.assertTrue(st["installed"])
        self.assertEqual(st["version"], gates.LOG_VERSION)
        self.assertEqual(set(st["triggers"].values()), {gates.LOG_VERSION})

    def test_validation_repairs_missing_old_or_altered_objects(self):
        self.put("a")
        self.put("b")
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(f"DROP TRIGGER {gates.LOG_TABLE}_upd")
            c.execute(f"DROP TRIGGER {gates.LOG_TABLE}_ins")
            c.execute(f"CREATE TRIGGER {gates.LOG_TABLE}_ins AFTER INSERT ON memories BEGIN SELECT 1; END")  # v1-shaped
        st = self.status()
        self.assertFalse(st["installed"])
        self.assertEqual(st["triggers"][f"{gates.LOG_TABLE}_ins"], 1)
        self.assertIsNone(st["triggers"][f"{gates.LOG_TABLE}_upd"])
        with self.store._session() as c:
            gates.ensure_change_log(c)
        self.assertTrue(self.status()["installed"])

    def test_wrong_table_shape_is_recreated(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(f"CREATE TABLE {gates.LOG_TABLE} (seq INTEGER PRIMARY KEY, other TEXT)")
        with self.store._session() as c:
            gates.ensure_change_log(c)
        self.assertTrue(self.status()["installed"])

    def test_repair_forces_cache_rebuild(self):
        for i in range(3):
            self.put(f"note {i}")
        fc = gates_r6._feature_cache(self.store)
        with self.store._session() as c:
            book = gates_r6._snapshot(c, fc, None)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute(f"DROP TRIGGER {gates.LOG_TABLE}_upd")             # an unlogged edit happens
            c.execute("UPDATE memories SET category = 'project' WHERE rowid = 1")
        with self.store._session() as c:
            gates.ensure_change_log(c)
            seq, changes = gates.changes_since(c, book.seq)
        self.assertIsNone(changes)                                      # -> rebuild, not a stale snapshot

    def test_coordinated_removal_drops_triggers_then_table(self):
        self.put("a")
        self.put("b")
        statements = []
        with self.store._session() as c:
            c.set_trace_callback(statements.append)
            self.assertTrue(gates.remove_change_log(c))
        drops = [s for s in statements if s.startswith("DROP")]
        self.assertTrue(all("TRIGGER" in s for s in drops[:4]) and "TABLE" in drops[-1], drops)
        st = self.status()
        self.assertFalse(st["table"] or any(st["triggers"].values()))
        self.put("c")                                                   # writes still work without it
        self.put("d")
        self.assertTrue(self.status()["installed"])                     # a gated process re-creates it

    def test_flags_off_client_never_removes_it_and_keeps_logging(self):
        self.put("a")
        self.put("b")
        n = self.status()["rows"]
        with patch.dict(os.environ, {k: "0" for k in CANDIDATE}):
            off = MemoryStore(self.db)
            off.remember("written by a client with every flag off", "other")
        st = self.status()
        self.assertTrue(st["installed"])
        self.assertGreater(st["rows"], n)

    def test_retention_is_bounded(self):
        self.put("a")
        self.put("b")
        with patch.object(gates, "LOG_KEEP", 4096), closing(sqlite3.connect(self.db)) as c, c:
            mid = c.execute("SELECT id FROM memories LIMIT 1").fetchone()[0]
            for i in range(gates.LOG_KEEP + 300):
                c.execute("UPDATE memories SET importance = ? WHERE id = ?", (0.2 + (i % 2) / 10, mid))
        self.assertLessEqual(self.status()["rows"], gates.LOG_KEEP)

    def test_backup_holds_ids_only(self):
        self.put("Secret phrase velvet harbor")
        self.put("b")
        with closing(sqlite3.connect(self.db)) as c, c:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({gates.LOG_TABLE})")]
            self.assertEqual(cols, ["seq", "row_id"])
            dump = "\n".join(c.iterdump())
        log_lines = [line for line in dump.splitlines() if gates.LOG_TABLE in line and line.startswith("INSERT")]
        self.assertTrue(log_lines)
        self.assertFalse(any("velvet" in line for line in log_lines))

    def test_downgrade_to_0419_code_keeps_working(self):
        self.put("a")
        self.put("b")
        base = os.environ.get("BASE_SRC", "/tmp/d9tree/src")
        if not Path(base, "memorysafe_chatgpt").exists():
            self.skipTest("pristine 0.4.19 worktree (BASE_SRC) not available")
        code = ("import sys; from pathlib import Path; from memorysafe_chatgpt.storage import MemoryStore; "
                f"s = MemoryStore(Path({str(self.db)!r})); r = s.remember('written by 0.4.19', 'other'); "
                "print(r['decision'])")
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
        p = subprocess.run([sys.executable, "-c", code], env={**env, "PYTHONPATH": base}, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(self.status()["installed"])
        self.put("gated again after the downgrade")


class SidecarResidueTests(Case):
    def test_undeletable_sidecar_is_reported_not_hidden(self):
        side = Path(f"{self.db}.gates-index")
        side.mkdir()                                       # unlink() of a directory fails (EISDIR / EPERM)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            store = MemoryStore(self.db)
        self.assertEqual(store.gate_sidecar_left, [str(side)])
        self.assertTrue(any("could not delete" in str(w.message) for w in caught))
        a = self.put("x")
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = self.store.forget(a, confirm=True)
        self.assertEqual(result["gate_sidecar_left"], [str(side)])
        side.rmdir()

    def test_deletable_sidecar_is_removed_silently(self):
        side = Path(f"{self.db}.gates-index")
        side.write_text("old")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            store = MemoryStore(self.db)
        self.assertFalse(side.exists())
        self.assertEqual(store.gate_sidecar_left, [])
        self.assertFalse([w for w in caught if "sidecar" in str(w.message)])


# --- reviewer B: MCP output schemas -----------------------------------------------------------------------

SCHEMA_DUMP = HERE / "bench" / "mcp_schema_dump.py"


class McpSchemaTests(unittest.TestCase):
    def dump(self, env):
        return json.loads(_run(SCHEMA_DUMP, env))

    def test_flags_on_declares_the_added_fields(self):
        tools = self.dump(CANDIDATE)["tools"]
        match = tools["memorysafe_find"]["outputSchema"]["$defs"]["MemoryMatch"]["properties"]
        for field in ("possibly_stale", "stale_reason", "status_review", "weakened"):
            self.assertIn(field, match)
        self.assertIn("extinguished_memory_ids", tools["memorysafe_remember"]["outputSchema"]["properties"])
        restore = tools["memorysafe_restore"]["outputSchema"]["properties"]
        for field in ("weakening_undone", "stale_cleared", "would_undo_weakening"):
            self.assertIn(field, restore)
        self.assertIn("gate_sidecar_left", tools["memorysafe_forget"]["outputSchema"]["properties"])

    def test_flags_off_schemas_have_none_of_them(self):
        text = json.dumps(self.dump({})["tools"])
        for field in ("possibly_stale", "stale_reason", "status_review", "weakened", "extinguished_memory_ids",
                      "weakening_undone", "stale_cleared", "would_undo_weakening", "gate_sidecar_left"):
            self.assertNotIn(f'"{field}"', text)


# --- Cold first write: the streamed ranking is exact ------------------------------------------------------

class StreamedRankingTests(unittest.TestCase):
    def test_streamed_equals_snapshot_and_reference(self):
        names = "Alice Bob Carmen Dmitri Elena Farid".split()
        cats = ["other", "task", "project", "preference", "safety", "decision"]
        for seed in range(6):
            r = random.Random(seed)
            with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
                    **CANDIDATE, "MEMORYSAFE_GATE_MAX_ACTIVE": "100000", "MEMORYSAFE_STATE_DIR": d}):
                gates._INDEXES.clear()
                gates_r6._CACHES.clear()
                s = MemoryStore(Path(d) / "m.sqlite3")
                ids = []
                for i in range(r.randint(30, 80)):
                    n = r.choice(names)
                    text = r.choice([f"{n} lives in {r.choice(['Oslo', 'Lima', 'Kyoto'])}", f"Goal: ship it with {n}",
                                     f"{n} is allergic to peanuts", f"{n} mentioned {r.choice(WORDS)} {i}"]) + f" #{i}"
                    res = s.remember(text, r.choice(cats), importance=r.choice([.2, .2, .5, .35]))
                    ids.append(res["memory_id"])
                with closing(sqlite3.connect(Path(d) / "m.sqlite3")) as c, c:
                    for mid in r.sample(ids, 5):
                        c.execute("UPDATE memories SET recall_count = ? WHERE id = ?", (r.randint(1, 4), mid))
                    for mid in r.sample(ids, 3):
                        c.execute("UPDATE memories SET protected = 1 WHERE id = ?", (mid,))
                for count in (1, 3, 6):
                    keep = r.choice(ids)
                    with s._session() as c:
                        a = [x["id"] for x in gates_r6.rank_useful(s, c, count, 20, keep)]
                        stats = dict(gates_r6._feature_cache(s).stats)
                    gates_r6._CACHES.clear()
                    with s._session() as c:
                        b = [x["id"] for x in gates_r6._rank_useful_book(s, c, count, 20, keep)]
                        f = gates_r6.rank_useful_full(s, c, count, 20, keep)
                    self.assertEqual(a, b)
                    self.assertEqual(a, f)
                    self.assertGreaterEqual(stats["streamed"], 1)


if __name__ == "__main__":
    unittest.main()
