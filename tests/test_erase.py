"""Permanent erase on the local store: the text is gone from every file, not just from recall."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memorysafe_chatgpt import erase
from memorysafe_chatgpt.storage import MemoryStore

NEEDLE = "Quillfeather"


def _bytes_under(folder: Path) -> bytes:
    return b"".join(path.read_bytes() for path in sorted(folder.rglob("*")) if path.is_file())


class EraseOneMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "memorysafe.sqlite3"
        self.exports = self.root / "state" / "exports"
        self.exports.mkdir(parents=True)
        self.store = MemoryStore(self.database)

    def remember(self, text: str, category: str = "project") -> str:
        return str(self.store.remember(text, category)["memory_id"])

    def test_forget_alone_leaves_the_text_in_the_file(self) -> None:
        # The reason this module exists: forget is a soft delete, and says so.
        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        self.store.forget(memory_id, confirm=True)
        self.assertIn(NEEDLE.encode(), _bytes_under(self.root))

    def test_without_confirm_nothing_changes_and_the_counts_are_listed(self) -> None:
        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        result = erase.erase_memory(self.store, memory_id)
        self.assertFalse(result["erased"])
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual(result["would_delete"]["memories"], 1)
        self.assertEqual(len(self.store.find(NEEDLE, 5, record=False)), 1)

    def test_an_unknown_id_changes_nothing(self) -> None:
        result = erase.erase_memory(self.store, "MS-00000000-NOPE", confirm=True)
        self.assertFalse(result["erased"])
        self.assertEqual(erase.receipts(self.store), [])

    def test_the_text_is_in_no_file_afterwards(self) -> None:
        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        other = self.remember("The harbour survey is scheduled for spring")
        # Every place the text reaches: a merge note, a search, a snapshot, an export.
        self.store.remember(f"The {NEEDLE} contract renews on 3 March 2027.", "project")
        self.store.find(f"{NEEDLE} renewal", 5)
        self.store.snapshot()
        (self.exports / "memorysafe-export-20261110-090000.json").write_text(
            json.dumps({"memories": [{"content": f"The {NEEDLE} contract"}]}), encoding="utf-8")
        self.assertIn(NEEDLE.encode(), _bytes_under(self.root))

        result = erase.erase_memory(self.store, memory_id, confirm=True, export_dirs=(self.exports,))

        self.assertTrue(result["erased"])
        self.assertTrue(result["file_rewritten"])
        self.assertGreaterEqual(result["snapshots_deleted"], 1)
        self.assertEqual(result["exports_deleted"], 1)
        self.assertNotIn(NEEDLE.encode(), _bytes_under(self.root))
        self.assertNotIn(NEEDLE.lower().encode(), _bytes_under(self.root))
        # The rest of the store is untouched, and a snapshot exists again.
        self.assertEqual(self.store.explain(other)["state"], "active")
        self.assertTrue(result["fresh_snapshot"])
        self.assertFalse(self.store.explain(memory_id)["found"])

    def test_no_snapshot_is_taken_of_a_file_that_could_not_be_rewritten(self) -> None:
        """SQLite's backup copies free pages too. When another program held the file and the
        rewrite was refused, the fresh snapshot carried the erased text back into the folder
        the erase had just emptied."""
        from unittest.mock import patch

        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        self.store.snapshot()
        with patch.object(erase, "_compact", return_value=(False, "OperationalError")):
            result = erase.erase_memory(self.store, memory_id, confirm=True)
        self.assertTrue(result["erased"])
        self.assertFalse(result["file_rewritten"])
        self.assertFalse(result["fresh_snapshot"])
        self.assertEqual(list((self.database.parent / "snapshots").glob("*")), [])
        self.assertIn("memorysafe erase --finish", result["reason"])
        finished = erase.finish(self.store)
        self.assertTrue(finished["file_rewritten"])
        self.assertEqual(len(list((self.database.parent / "snapshots").glob("*.sqlite3"))), 1)
        self.assertNotIn(NEEDLE.encode(), _bytes_under(self.root))

    def test_a_routine_snapshot_waits_for_an_unfinished_erase(self) -> None:
        """After a rewrite that was refused, the dashboard's snapshot at every start copied
        the erased text back into snapshots/ on any SQLite that leaves deleted bytes in
        place, which is the default in many builds. No snapshot is taken until the rewrite
        has happened."""
        from contextlib import contextmanager
        from unittest.mock import patch

        backend = self.store._backend
        connect = backend._connect

        def without_secure_delete():
            connection = connect()
            connection.execute("PRAGMA secure_delete = OFF")
            return connection

        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        snapshots = self.database.parent / "snapshots"
        with patch.object(backend, "_connect", without_secure_delete):
            with patch.object(erase, "_compact", return_value=(False, "OperationalError")):
                result = erase.erase_memory(self.store, memory_id, confirm=True)
            self.assertFalse(result["file_rewritten"])
            # What the dashboard does each time it starts, from a new process.
            self.assertIsNone(MemoryStore(self.database).snapshot())
            self.assertIsNone(self.store.snapshot())
            self.assertEqual(list(snapshots.glob("*")), [])
            self.assertTrue(erase.unfinished(self.store))
            # The mark is in the store, not in this process: it holds through a reopened
            # store, later saves and a second refused rewrite, for as long as it takes.
            reopened = MemoryStore(self.database)
            reopened.remember("The harbour survey is scheduled for spring", "project")
            self.assertIsNone(reopened.snapshot())
            with patch.object(erase, "_compact", return_value=(False, "OperationalError")):
                self.assertFalse(erase.finish(reopened)["file_rewritten"])
            self.assertTrue(erase.unfinished(MemoryStore(self.database)))
            self.assertIsNone(MemoryStore(self.database).snapshot())
            self.assertEqual(list(snapshots.glob("*")), [])

            finished = erase.finish(self.store)
            self.assertTrue(finished["file_rewritten"])
            self.assertFalse(erase.unfinished(self.store))
            self.assertIsNotNone(self.store.snapshot())
        self.assertGreaterEqual(len(list(snapshots.glob("*.sqlite3"))), 1)
        self.assertNotIn(NEEDLE.encode(), _bytes_under(self.root))

    def test_deleted_text_is_overwritten_even_before_the_rewrite(self) -> None:
        """Every store connection asks SQLite to zero what it deletes, so a copy of the file
        made between a refused rewrite and `--finish` does not carry the text either."""
        import sqlite3
        from contextlib import closing
        from unittest.mock import patch

        with self.store._session() as connection:
            self.assertEqual(connection.execute("PRAGMA secure_delete").fetchone()[0], 1)
        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        with patch.object(erase, "_compact", return_value=(False, "OperationalError")):
            erase.erase_memory(self.store, memory_id, confirm=True)
        copy = self.root / "copy.sqlite3"
        with self.store._session() as source, closing(sqlite3.connect(copy)) as destination:
            source.backup(destination)
        self.assertNotIn(NEEDLE.encode(), copy.read_bytes())
        self.assertNotIn(NEEDLE.lower().encode(), copy.read_bytes())

    def _refused_erase(self) -> None:
        """An erase whose file rewrite was refused, beside one memory that stays."""
        from unittest.mock import patch

        self.remember("The harbour survey is scheduled for spring")
        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        with patch.object(erase, "_compact", return_value=(False, "OperationalError")):
            self.assertFalse(erase.erase_memory(self.store, memory_id, confirm=True)["file_rewritten"])

    def _doctor_paths(self):
        from memorysafe_chatgpt.doctor import DoctorPaths

        return DoctorPaths(install_root=self.root, database_path=self.database, state_dir=self.root / "state",
                           secret_file=self.root / "s", tunnel_id_file=self.root / "t", health_url_file=self.root / "h")

    def test_an_unfinished_erase_is_said_until_it_is_finished(self) -> None:
        """The erase says once that the file was not rewritten. Health and the doctor keep
        saying it, with what to run, for as long as it is true."""
        from memorysafe_chatgpt import doctor

        self.assertFalse(self.store.health()["erase_unfinished"])
        self.assertIsNone(doctor._erase_check(self._doctor_paths()))
        self._refused_erase()
        health = self.store.health()
        self.assertTrue(health["erase_unfinished"])
        self.assertIn("erase --finish", health["erase_status"])
        self.assertNotIn(NEEDLE, json.dumps(health))
        check = doctor._erase_check(self._doctor_paths())
        self.assertEqual((check["id"], check["status"]), ("erase", "warning"))
        actions = doctor.next_actions({"checks": [check]})
        self.assertEqual([action["check"] for action in actions], ["erase"])
        self.assertIn("erase --finish", actions[0]["action"])

        self.assertTrue(erase.finish(self.store)["file_rewritten"])
        health = self.store.health()
        self.assertEqual((health["erase_unfinished"], health["erase_status"]), (False, ""))
        self.assertIsNone(doctor._erase_check(self._doctor_paths()))

    def test_the_health_tool_passes_the_unfinished_erase_on(self) -> None:
        from memorysafe_chatgpt.server import HealthResult

        self.assertLessEqual({"erase_unfinished", "erase_status"}, set(HealthResult.model_fields))

    def test_the_dashboard_finishes_the_erase_before_its_snapshot(self) -> None:
        """Each start tries the rewrite again, briefly, before any copy of the store is made."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from memorysafe_chatgpt import setup_app

        self._refused_erase()
        paths = SimpleNamespace(database_path=self.database)
        snapshots = self.database.parent / "snapshots"
        with patch.object(erase, "_compact", return_value=(False, "OperationalError")) as compact:
            setup_app._snapshot_on_start(paths)
        self.assertEqual(compact.call_args.kwargs, {"attempts": 1, "wait_seconds": 2})
        self.assertTrue(erase.unfinished(self.store))
        self.assertEqual(list(snapshots.glob("*")), [])

        setup_app._snapshot_on_start(paths)
        self.assertFalse(erase.unfinished(self.store))
        self.assertEqual(len(list(snapshots.glob("*.sqlite3"))), 1)
        self.assertNotIn(NEEDLE.encode(), _bytes_under(self.root))
        setup_app._snapshot_on_start(paths)
        self.assertEqual(len(list(snapshots.glob("*.sqlite3"))), 2)

    def test_an_uninstall_backup_leaves_the_erased_text_behind(self) -> None:
        """A purge copies the store out first. While an erase is unfinished, a copy of the
        file would take the freed pages with it, so the copy is rewritten as it is made."""
        import sqlite3
        from contextlib import closing
        from unittest.mock import patch

        from memorysafe_chatgpt import uninstall

        backend = self.store._backend
        connect = backend._connect

        def without_secure_delete():
            connection = connect()
            connection.execute("PRAGMA secure_delete = OFF")
            return connection

        home = self.root / "home"
        home.mkdir()
        with patch.object(backend, "_connect", without_secure_delete):
            self._refused_erase()
        self.assertIn(NEEDLE.encode(), _bytes_under(self.database.parent))

        real = sqlite3.connect

        def refuse_the_copy(target, *args, **kwargs):
            if not kwargs.get("uri"):
                raise sqlite3.OperationalError("database is locked")
            return real(target, *args, **kwargs)

        with patch.object(uninstall.sqlite3, "connect", refuse_the_copy):
            with self.assertRaises(OSError) as refused:
                uninstall.back_up_memories(home, self.root)
        self.assertIn("erase --finish", str(refused.exception))
        self.assertEqual(list(home.iterdir()), [])

        backup = uninstall.back_up_memories(home, self.root)
        self.assertNotIn(NEEDLE.encode(), _bytes_under(home))
        self.assertNotIn(NEEDLE.lower().encode(), _bytes_under(home))
        with closing(sqlite3.connect(backup)) as copy:
            kept = [row[0] for row in copy.execute("SELECT content FROM memories WHERE state = 'active'")]
            mark = copy.execute("SELECT 1 FROM settings WHERE key = 'erase_unfinished'").fetchone()
        self.assertEqual(kept, ["The harbour survey is scheduled for spring"])
        self.assertIsNone(mark)
        # The store itself is untouched: still waiting for its own rewrite.
        self.assertTrue(erase.unfinished(self.store))

    def test_the_receipt_holds_counts_and_no_text(self) -> None:
        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        result = erase.erase_memory(self.store, memory_id, confirm=True)
        receipt = result["receipt"]
        self.assertNotIn(NEEDLE, json.dumps(receipt))
        self.assertEqual(receipt["memory_id"], memory_id)
        self.assertEqual(receipt["deleted"]["memories"], 1)
        self.assertEqual(len(receipt["digest"]), 64)
        self.assertEqual(erase.receipts(self.store), [receipt])

    def test_the_older_version_it_replaced_goes_too(self) -> None:
        old = self.remember(f"I live in {NEEDLE}", "personal")
        new = self.store.remember("I live in Ottawa", "personal")
        self.assertEqual(new["replaced_memory_id"], old, new)
        result = erase.erase_memory(self.store, str(new["memory_id"]), confirm=True)
        self.assertEqual(result["receipt"]["deleted"]["older_versions_it_replaced"], 1)
        self.assertFalse(self.store.explain(old)["found"])
        self.assertNotIn(NEEDLE.encode(), _bytes_under(self.root))

    def test_a_memory_it_only_conflicted_with_stays(self) -> None:
        first = self.remember("The launch review is on 4 May 2027", "project")
        second = self.store.remember("The launch review is on 9 June 2027", "project")
        self.assertEqual(second["lifecycle"], "needs_review", second)
        erase.erase_memory(self.store, str(second["memory_id"]), confirm=True)
        self.assertEqual(self.store.explain(first)["state"], "active")
        self.assertEqual(self.store.review_conflicts()["count"], 0)

    def test_a_forgotten_memory_can_be_erased(self) -> None:
        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        self.store.forget(memory_id, confirm=True)
        self.assertTrue(erase.erase_memory(self.store, memory_id, confirm=True)["erased"])
        self.assertNotIn(NEEDLE.encode(), _bytes_under(self.root))

    def test_old_search_words_are_removed_and_the_search_count_is_kept(self) -> None:
        memory_id = self.remember(f"The {NEEDLE} contract renews on 3 March 2027")
        with self.store._session() as connection:
            connection.execute(
                "INSERT INTO decision_events(decision, memory_id, reason, content_bytes, source, created_at) "
                "VALUES ('RECALL', NULL, ?, 40, 'recall', '2026-10-01T00:00:00+00:00')",
                (f"Recalled 1 memories for: when does {NEEDLE} renew",),
            )
        before = self.store.health()["recall"]["times_memory_was_consulted"]
        erase.erase_memory(self.store, memory_id, confirm=True)
        self.assertEqual(self.store.health()["recall"]["times_memory_was_consulted"], before)
        self.assertNotIn(NEEDLE.encode(), _bytes_under(self.root))

    def test_a_new_search_records_no_words(self) -> None:
        self.remember("The harbour survey is scheduled for spring")
        self.store.find("harbour survey zanzibar", 5)
        with self.store._session() as connection:
            reasons = [str(row["reason"]) for row in connection.execute(
                "SELECT reason FROM decision_events WHERE decision = 'RECALL'")]
        self.assertEqual(reasons, ["Recalled 1 memories."])


class EraseEverythingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "data" / "memorysafe.sqlite3"
        store = MemoryStore(self.database)
        store.remember(f"The {NEEDLE} contract renews on 3 March 2027", "project")
        store.snapshot()

    def test_the_phrase_is_required(self) -> None:
        result = erase.erase_everything(self.database, "yes")
        self.assertFalse(result["erased"])
        self.assertTrue(self.database.is_file())

    def test_every_file_goes_and_a_textless_receipt_stays(self) -> None:
        result = erase.erase_everything(self.database, "Erase everything")
        self.assertTrue(result["erased"])
        self.assertFalse(self.database.exists())
        self.assertEqual(list((self.root / "data" / "snapshots").glob("*")), [])
        self.assertNotIn(NEEDLE.encode(), _bytes_under(self.root))
        saved = json.loads(Path(result["receipt_file"]).read_text(encoding="utf-8"))
        self.assertEqual(saved[-1]["deleted"]["memories"], 1)
        self.assertEqual(saved[-1]["kind"], "everything_on_this_computer")


if __name__ == "__main__":
    unittest.main()
