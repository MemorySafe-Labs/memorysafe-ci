"""Acceptance checks for `memorysafe consolidate` (the sleep-replay gate). INTERNAL.

Run: PYTHONPATH=src python tests/bench/gates_acceptance.py [--keep] [--json out.json]

Everything happens in a temporary directory with its own sqlite store; no live store is
read or written and no server is started, so no port is used (8765 in particular is never
touched). The CLI is driven as a subprocess, exactly as a person would run it.

Checks
  1  dry run: `consolidate` (no --apply, with --propose-merges) leaves the store identical.
     Compared three ways: sha256 of the sqlite file, sha256 of a dump of every table, and the
     directory listing (no new -wal/-shm/snapshot files left behind).
  2  `--apply` matches the dry-run plan exactly: the JSON report is identical apart from
     the "applied" flag; the memories that left active recall are exactly the planned moves;
     every other memory row is unchanged; the new history rows are exactly one EVICT per
     planned move (with the planned reason) and one PROPOSE_MERGE per proposal; nothing
     was merged (the proposed pairs are still two active rows, unchanged). A second plan
     after the apply has nothing left to move.
  3  `restore` reverses each change: for every moved memory, `memorysafe restore <id>
     --confirm` returns the row to its pre-apply value in every column except updated_at
     (state active, deleted_at empty), memory_relations is unchanged, and the active set
     equals the pre-apply set. History is append-only, so the EVICT/RESTORE rows stay.
  4  (round 4; was informational "not restorable") a surprise-gate MERGE is reversible: the write
     is kept as a hidden 'merge_copy' linked to the kept memory; `restore <copy>` without
     --confirm changes nothing (sha256 / table dumps / directory listing) and reports its plan;
     with --confirm the undo applied equals the plan, the kept memory returns to its pre-merge
     row in every column, the copy is an ordinary active memory, the history only grows; a kept
     memory changed after the merge is never edited by the undo.
  5  (round 5) prune-merge-copies: no window plans nothing; dry-run changes nothing; --apply
     matches the dry-run plan; restore of a pruned copy refuses (with and without --confirm)
     and changes nothing.

Exit code 0 only if every required check passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from memorysafe_chatgpt.storage import MemoryStore  # noqa: E402

CAPACITY = 24
NAMES = ["Ana", "Bruno", "Chen", "Dalia", "Emre", "Farah", "Gil", "Hana", "Ivo", "Jun"]
WINGS = ["east", "west", "north", "south", "annex", "lobby", "ward", "dock"]


def seed(store: MemoryStore) -> dict[str, str]:
    """A small mixed store: routine rounds (commons, some near-duplicates), distinct facts of
    several categories, recalled and protected memories. Gate flags are off while seeding."""

    ids: dict[str, str] = {}
    for i in range(30):
        text = f"Routine round: {NAMES[i % 10]} checked the {WINGS[(i // 10) % 8]} cart in the {WINGS[(i * 3) % 8]} wing"
        ids[f"routine{i}"] = store.remember(text, "task")["memory_id"]
    for i in range(4):  # worded repeats the shipped merge does not catch
        ids[f"dup{i}"] = store.remember(
            f"Restocking round {i}: {NAMES[i]} counted linen shelves {WINGS[i]} ward before noon", "task")["memory_id"]
        ids[f"dup{i}b"] = store.remember(
            f"Restocking round {i}: {NAMES[i]} counted linen shelves {WINGS[i]} ward before lunch today", "task")["memory_id"]
    facts = [
        ("Priya prefers short bullet summaries over long email threads", "preference"),
        ("Tomas is allergic to penicillin and the chart must say so", "safety"),
        ("We decided to keep the pharmacy audit on the first Monday", "decision"),
        ("Mei leads the discharge planning project for the annex", "project"),
        ("The vendor portal password rotates every quarter", "other"),
        ("Dana mentioned she enjoys cycling on weekends", "personal"),
        ("The cafeteria changed its soup of the day", "other"),
        ("Lindqvist asked about parking near the south entrance", "other"),
    ]
    for text, category in facts:
        ids[text] = store.remember(text, category)["memory_id"]
    store.protect(ids["Priya prefers short bullet summaries over long email threads"], True, "acceptance fixture")
    for _ in range(3):  # recalled facts
        store.find("discharge planning annex", 3)
        store.find("penicillin allergy chart", 3)
    return ids


def table_dump(path: Path) -> dict[str, str]:
    # immutable=1: the observer must not create -wal/-shm files or take locks itself
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        names = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {
            name: hashlib.sha256(
                json.dumps(connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall(), default=str).encode()
            ).hexdigest()
            for name in names
        }
    finally:
        connection.close()


def snapshot(path: Path) -> dict:
    listing = sorted((p.name, p.stat().st_size) for p in path.parent.iterdir())  # first: before any observer opens the db
    return {
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "tables": table_dump(path),
        "dir": listing,
    }


def rows(path: Path, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(sql, args).fetchall()
    finally:
        connection.close()


def memory_rows(path: Path) -> dict[str, dict]:
    return {r["id"]: dict(r) for r in rows(path, "SELECT * FROM memories")}


def cli(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "memorysafe_chatgpt.cli", *args], env=env, capture_output=True,
                          text=True, cwd=ROOT, timeout=300)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the temp directory")
    parser.add_argument("--json", help="write the results here")
    args = parser.parse_args()

    results: list[dict] = []

    def check(name: str, ok: bool, detail: str = "", required: bool = True) -> None:
        results.append({"check": name, "ok": bool(ok), "detail": detail, "required": required})
        print(f"  [{'PASS' if ok else ('FAIL' if required else 'INFO')}] {name}" + (f" -- {detail}" if detail else ""))

    with tempfile.TemporaryDirectory(prefix="ms-gates-acceptance-") as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "data" / "acceptance.sqlite3"
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORYSAFE_")}
        env.update(PYTHONPATH=str(ROOT / "src"), MEMORYSAFE_DB_PATH=str(db), MEMORYSAFE_INSTALL_ROOT=str(tmp_path),
                   MEMORYSAFE_GATE_CONSOLIDATE="1", HOME=str(tmp_path))
        assert str(db).startswith(tempfile.gettempdir()), "acceptance store must live in a temp dir"
        os.environ["MEMORYSAFE_DB_PATH"] = str(db)
        store = MemoryStore(db)
        ids = seed(store)
        del store
        with sqlite3.connect(db) as connection:  # fold the WAL into the file so the hash is stable
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        for sidecar in ("-wal", "-shm"):
            Path(str(db) + sidecar).unlink(missing_ok=True)
        before_rows = memory_rows(db)
        active_before = {i for i, r in before_rows.items() if r["state"] == "active"}
        print(f"Temp store {db} with {len(before_rows)} memories ({len(active_before)} active), capacity {CAPACITY}. No server, no port.")

        # 1 -- dry run changes nothing
        pre = snapshot(db)
        plan_run = cli(env, "consolidate", "--capacity", str(CAPACITY), "--propose-merges", "--json")
        post = snapshot(db)
        plan = json.loads(plan_run.stdout) if plan_run.returncode == 0 else {}
        check("1 dry run exits 0 and plans moves", plan_run.returncode == 0 and bool(plan.get("move")),
              f"{len(plan.get('move', []))} planned moves, {len(plan.get('merge_proposals', []))} merge proposals; stderr={plan_run.stderr.strip()[:120]}")
        check("1 dry run: sqlite file sha256 identical", pre["file_sha256"] == post["file_sha256"], pre["file_sha256"][:16])
        check("1 dry run: every table dump identical", pre["tables"] == post["tables"],
              ", ".join(f"{k}={v[:8]}" for k, v in post["tables"].items()))
        check("1 dry run: directory listing (names and sizes, incl. -wal/-shm/snapshots) identical", pre["dir"] == post["dir"], str(post["dir"]))
        again = cli(env, "consolidate", "--capacity", str(CAPACITY), "--propose-merges", "--json")
        check("1 dry run is repeatable (same plan twice)", json.loads(again.stdout) == plan)

        # 2 -- apply matches the plan
        events_before = rows(db, "SELECT * FROM decision_events ORDER BY rowid")
        relations_before = [tuple(r) for r in rows(db, "SELECT * FROM memory_relations ORDER BY rowid")]
        applied_run = cli(env, "consolidate", "--capacity", str(CAPACITY), "--propose-merges", "--apply", "--json")
        applied = json.loads(applied_run.stdout) if applied_run.returncode == 0 else {}
        check("2 apply exits 0", applied_run.returncode == 0, applied_run.stderr.strip()[:120])
        check("2 apply report == dry-run plan (all fields except 'applied')",
              {**applied, "applied": False} == plan and applied.get("applied") is True)
        planned = [m["id"] for m in plan["move"]]
        after_rows = memory_rows(db)
        left = {i for i in active_before if after_rows[i]["state"] != "active"}
        check("2 memories that left active recall == planned moves", left == set(planned) and len(planned) == len(set(planned)),
              f"{len(left)} moved")
        check("2 moved rows are state 'evicted', nothing deleted", all(after_rows[i]["state"] == "evicted" for i in planned)
              and set(after_rows) == set(before_rows))
        untouched = [i for i in before_rows if i not in set(planned)]
        check("2 every other memory row is byte-identical", all(after_rows[i] == before_rows[i] for i in untouched),
              f"{len(untouched)} rows compared")
        check("2 moved rows differ only in state and updated_at",
              all({k for k in after_rows[i] if after_rows[i][k] != before_rows[i][k]} <= {"state", "updated_at"} for i in planned))
        events_after = rows(db, "SELECT * FROM decision_events ORDER BY rowid")
        new_events = events_after[len(events_before):]
        check("2 earlier history untouched (append-only)", [dict(r) for r in events_after[:len(events_before)]] == [dict(r) for r in events_before])
        evict = {e["memory_id"]: e["reason"] for e in new_events if e["decision"] == "EVICT"}
        proposed = {e["memory_id"]: e["reason"] for e in new_events if e["decision"] == "PROPOSE_MERGE"}
        check("2 one EVICT event per planned move, with the planned reason",
              evict == {m["id"]: m["reason"] for m in plan["move"]} and len(new_events) == len(evict) + len(proposed))
        check("2 one PROPOSE_MERGE event per proposal, with the proposed reason",
              proposed == {p["merge_in"]: p["reason"] for p in plan["merge_proposals"]})
        pair_ids = {p["keep"] for p in plan["merge_proposals"]} | {p["merge_in"] for p in plan["merge_proposals"]}
        check("2 nothing was merged: both rows of every proposal are still active and unchanged",
              all(after_rows[i]["state"] == "active" and after_rows[i] == before_rows[i] for i in pair_ids), f"{len(pair_ids)} rows")
        check("2 relations table unchanged by apply", [tuple(r) for r in rows(db, "SELECT * FROM memory_relations ORDER BY rowid")] == relations_before)
        second = json.loads(cli(env, "consolidate", "--capacity", str(CAPACITY), "--json").stdout)
        check("2 a second plan has nothing left to move", second["move"] == [] and second["active_after"] <= CAPACITY,
              f"active {second['active_before']}")

        # 3 -- restore reverses each change
        restored_ok = True
        per_memory = []
        for memory_id in planned:
            result = cli(env, "restore", memory_id, "--confirm")
            now = memory_rows(db)[memory_id]
            diff = {k for k in now if now[k] != before_rows[memory_id][k]}
            ok = result.returncode == 0 and diff <= {"updated_at"} and now["state"] == "active"
            restored_ok &= ok
            per_memory.append({"id": memory_id, "ok": ok, "differs_in": sorted(diff)})
        check("3 each restored memory equals its pre-apply row (only updated_at differs)", restored_ok,
              f"{len(planned)} restored; sample {per_memory[0]['differs_in'] if per_memory else '-'}")
        final_rows = memory_rows(db)
        check("3 active set after restoring all == active set before", {i for i, r in final_rows.items() if r["state"] == "active"} == active_before)
        check("3 non-moved memories still identical", all(final_rows[i] == before_rows[i] for i in untouched))
        check("3 relations table identical to before", [tuple(r) for r in rows(db, "SELECT * FROM memory_relations ORDER BY rowid")] == relations_before)
        restores = [e for e in rows(db, "SELECT * FROM decision_events WHERE decision = 'RESTORE'")]
        check("3 history is append-only: one RESTORE event per restored memory", {e["memory_id"] for e in restores} == set(planned))

        # 4 -- a surprise-gate MERGE is reversible by `restore` (round 4: store-and-link)
        merge_db = tmp_path / "merge" / "m.sqlite3"
        merge_env = {**env, "MEMORYSAFE_DB_PATH": str(merge_db)}
        os.environ["MEMORYSAFE_DB_PATH"] = str(merge_db)
        os.environ["MEMORYSAFE_GATE_SURPRISE"] = "1"
        try:
            ms = MemoryStore(merge_db)
            for i in range(12):
                ms.remember(f"Routine round: {NAMES[i % 10]} checked the {WINGS[i % 8]} cart in the {WINGS[(i * 3) % 8]} wing", "task")
            kept = ms.remember("Bruno restocked linen shelf north ward today", "task")["memory_id"]
            del ms
            with sqlite3.connect(merge_db) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            for sidecar in ("-wal", "-shm"):
                Path(str(merge_db) + sidecar).unlink(missing_ok=True)
            time.sleep(1.1)  # timestamps have one-second resolution: let the merge change updated_at too
            ms = MemoryStore(merge_db)
            pre_rows = memory_rows(merge_db)
            pre_events = [dict(r) for r in rows(merge_db, "SELECT * FROM decision_events ORDER BY rowid")]
            incoming = "Bruno restocked the linen shelf in the north ward"
            merged = ms.remember(incoming, "task")
            del ms
            with sqlite3.connect(merge_db) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            for sidecar in ("-wal", "-shm"):
                Path(str(merge_db) + sidecar).unlink(missing_ok=True)
            merged_rows = memory_rows(merge_db)
            new_ids = set(merged_rows) - set(pre_rows)
            copy_id = next(iter(new_ids)) if len(new_ids) == 1 else ""
            copy_row = merged_rows.get(copy_id, {})
            links = rows(merge_db, "SELECT * FROM memory_relations WHERE decision = 'MERGE'")
            check("4 surprise-gate MERGE keeps the longer wording in the kept memory and the other wording in history",
                  merged["decision"] == "MERGE" and merged["memory_id"] == kept and merged_rows[kept]["content"] == incoming
                  and "Previous text: Bruno restocked linen shelf north ward today" in merged["reason"], f"decision={merged['decision']}")
            check("4 the merge adds exactly one row: a hidden copy of the write, state 'merge_copy' (out of recall, nothing lost)",
                  len(new_ids) == 1 and copy_row.get("state") == "merge_copy" and copy_row.get("content") == incoming
                  and {i for i, r in merged_rows.items() if r["state"] == "active"} == {i for i, r in pre_rows.items() if r["state"] == "active"},
                  f"copy {copy_id}")
            check("4 a MERGE link (old = kept memory, new = copy, status 'merged') and an event are written",
                  len(links) == 1 and (links[0]["old_memory_id"], links[0]["new_memory_id"], links[0]["status"]) == (kept, copy_id, "merged"))
            # dry run of restore (no --confirm) changes nothing
            before_dry = snapshot(merge_db)
            dry = cli(merge_env, "restore", copy_id, "--json")
            after_dry = snapshot(merge_db)
            dry_plan = json.loads(dry.stdout).get("would_undo_merge", {}) if dry.returncode == 0 else {}
            check("4 restore without --confirm: sqlite file sha256, every table dump and directory listing identical",
                  before_dry == after_dry and dry.returncode == 0, before_dry["file_sha256"][:16])
            check("4 restore without --confirm reports the planned undo (kept memory, columns)",
                  dry_plan.get("kept_memory_id") == kept and dry_plan.get("reverted") is True and "content" in dry_plan.get("columns", []),
                  f"columns {dry_plan.get('columns')}")
            applied_merge = cli(merge_env, "restore", copy_id, "--confirm", "--json")
            done = json.loads(applied_merge.stdout) if applied_merge.returncode == 0 else {}
            now_rows = memory_rows(merge_db)
            changed_cols = sorted(c for c in now_rows[kept] if now_rows[kept][c] != merged_rows[kept][c])
            check("4 restore --confirm: the undo applied == the undo planned, and exactly the planned columns of the kept memory changed",
                  done.get("merge_undone") == dry_plan and changed_cols == sorted(dry_plan.get("columns", [])), f"changed {changed_cols}")
            check("4 round trip: the kept memory equals its row from before the merge in every column",
                  now_rows[kept] == pre_rows[kept])
            check("4 round trip: every other earlier memory unchanged; the write is back as an ordinary active memory",
                  all(now_rows[i] == pre_rows[i] for i in pre_rows if i != kept) and now_rows[copy_id]["state"] == "active"
                  and now_rows[copy_id]["content"] == incoming and set(now_rows) == set(pre_rows) | {copy_id})
            link_after = rows(merge_db, "SELECT * FROM memory_relations WHERE decision = 'MERGE'")
            events_now = [dict(r) for r in rows(merge_db, "SELECT * FROM decision_events ORDER BY rowid")]
            check("4 link closed ('undone'); history append-only (earlier events identical, a RESTORE event added for the copy)",
                  len(link_after) == 1 and link_after[0]["status"] == "undone" and events_now[:len(pre_events)] == pre_events
                  and any(e["decision"] == "RESTORE" and e["memory_id"] == copy_id for e in events_now[len(pre_events):]))
            # a kept memory changed since the merge is never edited by the undo
            os.environ["MEMORYSAFE_GATE_SURPRISE"] = "1"
            ms = MemoryStore(merge_db)
            second_kept = ms.remember("Chen sealed the crash cart in the annex wing", "task")["memory_id"]
            time.sleep(1.1)
            second = ms.remember("Chen sealed crash cart annex wing today", "task")
            ms.protect(second_kept, True, "acceptance fixture: changed after the merge")
            del ms
            second_copy = next((i for i, r in memory_rows(merge_db).items() if r["state"] == "merge_copy"), "")
            kept_now = memory_rows(merge_db)[second_kept]
            late = json.loads(cli(merge_env, "restore", second_copy, "--confirm", "--json").stdout)
            check("4 a kept memory changed after the merge is left as it is; the copy is still restored",
                  second["decision"] == "MERGE" and late.get("restored") is True and late["merge_undone"]["reverted"] is False
                  and memory_rows(merge_db)[second_kept] == kept_now and memory_rows(merge_db)[second_copy]["state"] == "active",
                  late.get("reason", "")[:80])

            # 5 -- prune-merge-copies (round 5): dry-run / apply / restore-after-prune refusal
            prune_env = {**merge_env, "MEMORYSAFE_GATE_CONSOLIDATE": "1"}
            # empty window → nothing planned
            empty = cli(prune_env, "prune-merge-copies", "--json")
            empty_report = json.loads(empty.stdout) if empty.returncode == 0 else {}
            check("5 prune with no window plans nothing and exits 0",
                  empty.returncode == 0 and empty_report.get("plan") == [] and empty_report.get("days") is None
                  and empty_report.get("keep") is None, empty.stderr.strip()[:80])
            # make a fresh merge copy to prune
            ms = MemoryStore(merge_db)
            prune_kept = ms.remember("Emre audited the supply cart in the lobby wing", "task")["memory_id"]
            time.sleep(1.1)
            prune_merge = ms.remember("Emre audited supply cart lobby wing today", "task")
            del ms
            prune_copy = next((i for i, r in memory_rows(merge_db).items()
                               if r["state"] == "merge_copy" and r["content"] == "Emre audited supply cart lobby wing today"), "")
            check("5 a fresh merge_copy exists to prune",
                  prune_merge.get("decision") == "MERGE" and bool(prune_copy), f"decision={prune_merge.get('decision')}")
            before_prune = snapshot(merge_db)
            dry_prune = cli(prune_env, "prune-merge-copies", "--keep", "0", "--json")
            after_dry_prune = snapshot(merge_db)
            dry_report = json.loads(dry_prune.stdout) if dry_prune.returncode == 0 else {}
            check("5 prune dry-run: sqlite file sha256, every table dump and directory listing identical",
                  before_prune == after_dry_prune and dry_prune.returncode == 0, before_prune["file_sha256"][:16])
            check("5 prune dry-run plans the merge_copy (keep=0)",
                  prune_copy in [p["id"] for p in dry_report.get("plan", [])] and dry_report.get("applied") is False)
            apply_prune = cli(prune_env, "prune-merge-copies", "--keep", "0", "--apply", "--json")
            apply_report = json.loads(apply_prune.stdout) if apply_prune.returncode == 0 else {}
            check("5 prune --apply: pruned ids == dry-run plan ids",
                  apply_report.get("pruned") == [p["id"] for p in dry_report.get("plan", [])]
                  and apply_report.get("applied") is True, f"pruned={apply_report.get('pruned')}")
            check("5 pruned copy is state 'deleted' and the MERGE link is status 'pruned'",
                  memory_rows(merge_db).get(prune_copy, {}).get("state") == "deleted"
                  and any(r["new_memory_id"] == prune_copy and r["status"] == "pruned"
                          for r in rows(merge_db, "SELECT * FROM memory_relations WHERE decision = 'MERGE'")))
            before_refuse = snapshot(merge_db)
            refuse_dry = cli(prune_env, "restore", prune_copy, "--json")
            refuse_dry_body = json.loads(refuse_dry.stdout) if refuse_dry.returncode == 0 else {}
            check("5 restore without --confirm of a pruned copy refuses and changes nothing",
                  before_refuse == snapshot(merge_db) and refuse_dry_body.get("restored") is False
                  and refuse_dry_body.get("pruned_merge_copy") is True
                  and "would_undo_merge" not in refuse_dry_body)
            refuse_apply = cli(prune_env, "restore", prune_copy, "--confirm", "--json")
            refuse_apply_body = json.loads(refuse_apply.stdout) if refuse_apply.returncode == 0 else {}
            check("5 restore --confirm of a pruned copy refuses and changes nothing",
                  before_refuse == snapshot(merge_db) and refuse_apply_body.get("restored") is False
                  and refuse_apply_body.get("pruned_merge_copy") is True)
        finally:
            os.environ.pop("MEMORYSAFE_GATE_SURPRISE", None)

        if args.keep:
            print("kept", tmp)
            keep_copy = ROOT.parent / "acceptance_store_kept"
            subprocess.run(["cp", "-r", tmp, str(keep_copy)])

    required_failed = [r for r in results if r["required"] and not r["ok"]]
    print(f"\n{sum(r['ok'] for r in results if r['required'])}/{sum(1 for r in results if r['required'])} required checks passed"
          + ("" if not required_failed else f"; FAILED: {[r['check'] for r in required_failed]}"))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
    return 1 if required_failed else 0


if __name__ == "__main__":
    sys.exit(main())
