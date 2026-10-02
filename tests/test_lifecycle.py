"""Evidence-based supersession: age never demotes; only a clear later fact does."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memorysafe_chatgpt.lifecycle import classify_pair, extract_slots
from memorysafe_chatgpt.storage import MemoryStore


class ClassificationTests(unittest.TestCase):
    def test_clear_location_change_is_a_contradiction(self) -> None:
        verdict = classify_pair("I live in Montreal", "I live in Toronto")
        self.assertEqual(verdict["action"], "contradict")

    def test_bare_deadlines_are_ambiguous(self) -> None:
        verdict = classify_pair(
            "Deadline is 4 November 2026",
            "Deadline is 28 September 2026",
        )
        self.assertEqual(verdict["action"], "review")

    def test_different_project_deadlines_are_unrelated(self) -> None:
        verdict = classify_pair(
            "Primo-adoptants submission closes 4 November 2026",
            "Gama Fund deadline is 28 September 2026",
        )
        self.assertEqual(verdict["action"], "unrelated")

    def test_same_person_new_title_is_a_contradiction(self) -> None:
        verdict = classify_pair(
            "Morgan Reyes is Head of Sales",
            "Morgan Reyes is Head of Operations",
        )
        self.assertEqual(verdict["action"], "contradict")

    def test_unrelated_people_are_not_conflicts(self) -> None:
        verdict = classify_pair(
            "Jordan Ellis is a strategic advisor",
            "Morgan Reyes is Head of Operations",
        )
        self.assertEqual(verdict["action"], "unrelated")


class VersionSupersessionTests(unittest.TestCase):
    """A release that says it replaces another one is evidence, not ambiguity.

    This pair sat open as an unresolved review for six days on a live store, with
    both memories active, because "0.3.4" normalises to "0 3 4" and every piece is
    then dropped for being one character long -- so two memories about different
    builds of the same product shared no distinctive term at all.
    """

    OLD = "MemorySafe 0.3.4 was installed and both services registered on 22 August 2026"
    NEW = "MemorySafe 0.3.5 was installed on Dana's laptop on 2026-08-26, replacing 0.3.4."

    def test_version_numbers_survive_slot_extraction(self) -> None:
        self.assertIn(("version", "memorysafe", "0.3.4"), extract_slots(self.OLD))

    def test_explicit_replacement_of_a_version_supersedes(self) -> None:
        verdict = classify_pair(self.NEW, self.OLD)
        self.assertEqual(verdict["action"], "contradict")
        self.assertIn("0.3.4", verdict["evidence"])
        self.assertIn("0.3.5", verdict["evidence"])

    def test_bare_version_bump_is_reviewed_not_superseded(self) -> None:
        """The same product at two versions can legitimately be on two machines."""
        verdict = classify_pair(
            "MemorySafe 0.3.5 was installed on the Windows box on 2026-08-30",
            "MemorySafe 0.3.4 was installed on the iMac on 22 August 2026",
        )
        self.assertEqual(verdict["action"], "review")

    def test_versions_of_different_products_are_unrelated(self) -> None:
        verdict = classify_pair(
            "Widget 0.3.5 was installed on 2026-08-30",
            "MemorySafe 0.3.4 was installed on 22 August 2026",
        )
        self.assertEqual(verdict["action"], "unrelated")

    def test_plain_decimals_are_not_version_numbers(self) -> None:
        """Benchmark figures share this shape and must not trigger supersession."""
        self.assertEqual(extract_slots("PathMNIST similarity was 0.87 in the run"), ())
        verdict = classify_pair(
            "PathMNIST similarity was 0.87 in the August run",
            "PathMNIST similarity was 0.45 in the July run",
        )
        self.assertNotEqual(verdict["action"], "contradict")


class LifecycleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "life.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_protection_never_expires_because_of_age(self) -> None:
        saved = self.store.remember("The permit is filed in Dana's name", "decision")
        self.assertTrue(saved["protected"])
        with self.store._session() as connection:
            connection.execute(
                "UPDATE memories SET created_at = ?, updated_at = ? WHERE id = ?",
                ("2018-01-01T00:00:00+00:00", "2018-01-01T00:00:00+00:00", saved["memory_id"]),
            )
        explained = self.store.explain(saved["memory_id"])
        self.assertTrue(explained["protected"])
        self.assertEqual(explained["state"], "active")
        hits = self.store.find("permit filed", 5, record=False)
        self.assertEqual(hits[0]["memory_id"], saved["memory_id"])

    def test_clear_contradiction_supersedes_the_old_memory(self) -> None:
        old = self.store.remember("I live in Montreal", "personal")
        new = self.store.remember("I live in Toronto", "personal")
        self.assertEqual(new["lifecycle"], "superseded_previous")
        self.assertEqual(new["replaced_memory_id"], old["memory_id"])
        self.assertEqual(self.store.explain(old["memory_id"])["state"], "superseded")
        self.assertEqual(self.store.explain(new["memory_id"])["state"], "active")

    def test_ambiguous_evidence_does_not_supersede(self) -> None:
        first = self.store.remember("Deadline is 4 November 2026", "project")
        second = self.store.remember("Deadline is 28 September 2026", "project")
        self.assertEqual(second["lifecycle"], "needs_review")
        self.assertIsNone(second["replaced_memory_id"])
        self.assertEqual(self.store.explain(first["memory_id"])["state"], "active")
        self.assertEqual(self.store.explain(second["memory_id"])["state"], "active")
        reviews = self.store.review_conflicts()
        self.assertEqual(reviews["count"], 1)

    def test_superseded_memories_do_not_appear_in_normal_recall(self) -> None:
        self.store.remember("I live in Montreal", "personal")
        self.store.remember("I live in Toronto", "personal")
        hits = [item["content"] for item in self.store.find("live in", 5, record=False)]
        self.assertTrue(any("Toronto" in text for text in hits))
        self.assertFalse(any("Montreal" in text for text in hits))

    def test_superseded_memories_remain_inspectable(self) -> None:
        old = self.store.remember("I live in Montreal", "personal")
        new = self.store.remember("I live in Toronto", "personal")
        detail = self.store.inspect_memory(old["memory_id"])
        self.assertTrue(detail["found"])
        self.assertEqual(detail["state"], "superseded")
        self.assertIn("Montreal", detail["content"])
        relations = detail["relations"]
        self.assertTrue(relations)
        self.assertEqual(relations[-1]["new_memory_id"], new["memory_id"])
        self.assertEqual(relations[-1]["decision"], "SUPERSEDE")

    def test_restoration_returns_a_memory_to_active_recall(self) -> None:
        old = self.store.remember("I live in Montreal", "personal")
        self.store.remember("I live in Toronto", "personal")
        blocked = self.store.restore(old["memory_id"], confirm=False)
        self.assertFalse(blocked["restored"])
        self.assertTrue(blocked["needs_confirmation"])
        self.assertEqual(self.store.explain(old["memory_id"])["state"], "superseded")
        restored = self.store.restore(old["memory_id"], confirm=True)
        self.assertTrue(restored["restored"])
        self.assertEqual(self.store.explain(old["memory_id"])["state"], "active")
        hits = [item["content"] for item in self.store.find("Montreal", 5, record=False)]
        self.assertTrue(any("Montreal" in text for text in hits))

    def test_decision_history_identifies_the_replacement_and_reason(self) -> None:
        old = self.store.remember("I live in Montreal", "personal")
        new = self.store.remember("I live in Toronto", "personal")
        history = self.store.explain(old["memory_id"])["history"]
        supersedes = [event for event in history if event["decision"] == "SUPERSEDE"]
        self.assertEqual(len(supersedes), 1)
        self.assertIn(new["memory_id"], supersedes[0]["reason"])
        self.assertIn("lives_in", supersedes[0]["reason"])

    def test_unrelated_protected_memories_are_not_harmed(self) -> None:
        kept = self.store.remember("Morgan Reyes is Head of Operations", "decision")
        self.store.remember("I live in Montreal", "personal")
        self.store.remember("I live in Toronto", "personal")
        explained = self.store.explain(kept["memory_id"])
        self.assertEqual(explained["state"], "active")
        self.assertTrue(explained["protected"])
        hits = self.store.find("Morgan", 5, record=False)
        self.assertEqual(hits[0]["memory_id"], kept["memory_id"])

    def test_exact_and_near_duplicate_merging_still_works(self) -> None:
        first = self.store.remember("I prefer concise answers", "preference")
        exact = self.store.remember("I prefer concise answers", "preference")
        self.assertEqual(exact["decision"], "MERGE")
        self.assertEqual(exact["memory_id"], first["memory_id"])
        near = self.store.remember("I prefer concise answers.", "preference")
        self.assertEqual(near["decision"], "MERGE")
        self.assertEqual(self.store.health()["active_memories"], 1)

    def test_forget_is_soft_deletion_not_erasure(self) -> None:
        saved = self.store.remember("A working note about the Fasken workshop", "project")
        result = self.store.forget(saved["memory_id"])
        self.assertIn("not permanently erased", result["reason"])
        detail = self.store.inspect_memory(saved["memory_id"])
        self.assertEqual(detail["state"], "deleted")
        self.assertIn("Fasken", detail["content"])
        self.assertFalse(self.store.find("Fasken", 5, record=False))

    def test_resolve_conflict_requires_confirmation(self) -> None:
        self.store.remember("Deadline is 4 November 2026", "project")
        self.store.remember("Deadline is 28 September 2026", "project")
        conflict_id = self.store.review_conflicts()["conflicts"][0]["conflict_id"]
        refused = self.store.resolve_conflict(conflict_id, "supersede", confirm=False)
        self.assertFalse(refused["resolved"])
        self.assertTrue(refused["needs_confirmation"])
        done = self.store.resolve_conflict(conflict_id, "supersede", confirm=True)
        self.assertTrue(done["resolved"])
        remaining = [item["content"] for item in self.store.find("Deadline", 5, record=False)]
        self.assertEqual(len(remaining), 1)
        self.assertIn("28 September", remaining[0])

    def test_forgetting_one_side_closes_the_conflict(self) -> None:
        """An open conflict about a forgotten memory can never be acted on."""
        self.store.remember("MemorySafe 0.3.4 was installed on the iMac", "project")
        second = self.store.remember(
            "MemorySafe 0.3.5 was installed on the Windows box on 2026-08-30", "project"
        )
        open_before = self.store.review_conflicts()["conflicts"]
        self.assertTrue(open_before, "expected a version review to be raised")

        result = self.store.forget(second["memory_id"], confirm=True)
        self.assertTrue(result["forgotten"])
        self.assertGreaterEqual(result["conflicts_closed"], 1)
        self.assertEqual(self.store.review_conflicts()["conflicts"], [])

    def test_closed_conflicts_remain_inspectable(self) -> None:
        self.store.remember("MemorySafe 0.3.4 was installed on the iMac", "project")
        second = self.store.remember(
            "MemorySafe 0.3.5 was installed on the Windows box on 2026-08-30", "project"
        )
        self.store.forget(second["memory_id"], confirm=True)
        everything = self.store.review_conflicts(include_resolved=True)["conflicts"]
        self.assertTrue(any(c["status"] == "closed" for c in everything))

    def test_health_reports_open_conflicts(self) -> None:
        """The score cannot fall when a conflict opens, so it has to be said in words."""
        quiet = self.store.health()
        self.assertEqual(quiet["open_reviews"], 0)
        self.assertIn("No conflicts", quiet["governance_status"])

        self.store.remember("MemorySafe 0.3.4 was installed on the iMac", "project")
        self.store.remember(
            "MemorySafe 0.3.5 was installed on the Windows box on 2026-08-30", "project"
        )
        busy = self.store.health()
        self.assertGreaterEqual(busy["open_reviews"], 1)
        self.assertIn("waiting for a decision", busy["governance_status"])

    def test_keep_both_never_revives_a_forgotten_memory(self) -> None:
        """Forgetting is an instruction. Resolving a conflict must not undo it.

        Found on a live store: two probes were forgotten, then keep_both put both
        back into active recall -- with deleted_at still set -- and nobody was asked.
        """
        self.store.remember("MemorySafe 0.3.4 was installed on the iMac", "project")
        second = self.store.remember(
            "MemorySafe 0.3.5 was installed on the Windows box on 2026-08-30", "project"
        )
        conflict_id = self.store.review_conflicts()["conflicts"][0]["conflict_id"]
        self.store.forget(second["memory_id"], confirm=True)

        self.store.resolve_conflict(conflict_id, "keep_both", confirm=True)

        self.assertEqual(
            self.store.explain(second["memory_id"])["state"],
            "deleted",
            "a forgotten memory was put back into recall by conflict resolution",
        )
        self.assertEqual(self.store.find("Windows box", 5), [])

    def test_restore_is_still_the_way_back_from_forgotten(self) -> None:
        saved = self.store.remember("Dana prefers straight talk", "preference")
        self.store.forget(saved["memory_id"])
        self.store.restore(saved["memory_id"], confirm=True)
        self.assertEqual(self.store.explain(saved["memory_id"])["state"], "active")

    def test_concurrent_claude_and_codex_writes_remain_safe(self) -> None:
        def write(index: int) -> str:
            store = MemoryStore(self.store.database_path)
            result = store.remember(f"Codex note {index} about unique topic {index}", "project")
            store.find(f"unique topic {index}", 3, record=False)
            return result["memory_id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(write, range(24)))
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(self.store.health()["active_memories"], 24)


if __name__ == "__main__":
    unittest.main()


class TitleSlotTests(unittest.TestCase):
    """"X is Y" is not a job title unless Y looks like one.

    Found live: "Dana is satisfied that MemorySafe is installed once..." and
    "Dana is recording the demo video..." were read as two competing titles for
    the same person, and the first was silently superseded at confidence 0.86 --
    a true memory removed from recall with nobody asked.
    """

    SATISFIED = (
        "Dana is satisfied that MemorySafe is installed once on the laptop and shared "
        "across Claude, Codex, and Cursor chats without reinstalling each conversation."
    )
    RECORDING = (
        "Dana is recording the demo video herself on 2026-09-02 "
        "rather than generating it."
    )

    def test_a_clause_is_not_a_title(self) -> None:
        self.assertEqual(extract_slots(self.SATISFIED), ())

    def test_an_activity_is_not_a_title(self) -> None:
        self.assertEqual(extract_slots(self.RECORDING), ())

    def test_two_unrelated_facts_about_one_person_never_supersede(self) -> None:
        verdict = classify_pair(self.RECORDING, self.SATISFIED)
        self.assertNotEqual(
            verdict["action"],
            "contradict",
            "an unrelated fact about the same person was auto-superseded",
        )

    def test_real_titles_still_supersede(self) -> None:
        self.assertEqual(
            classify_pair(
                "Morgan Reyes is Head of Sales",
                "Morgan Reyes is Head of Operations",
            )["action"],
            "contradict",
        )

    def test_short_noun_phrase_titles_are_still_extracted(self) -> None:
        self.assertIn(
            ("titled", "jordan ellis", "strategic advisor"),
            extract_slots("Jordan Ellis is a strategic advisor"),
        )
