"""Which memory a search returns first, what it leaves out, and what search words add."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memorysafe_chatgpt import server, storage
from memorysafe_chatgpt.storage import MemoryStore

FILLER = [
    "The quarterly report is tracked in a shared folder",
    "The onboarding checklist was approved by the finance lead",
    "The vendor contract is scheduled for next sprint",
    "The analytics dashboard was handed to the regional team",
    "The support queue depends on the on-call rota",
    "The pricing page was reviewed with an external agency",
    "The design review is owned by the platform group",
    "The hiring plan is budgeted under the autumn roadmap",
    "The data pipeline was moved to the Lisbon office",
    "The billing export is blocked on a new ticket",
    "The release notes were reviewed with the second vendor",
    "The travel booking is tracked in the partner portal",
    "The office lease was approved by the compliance track",
    "The team offsite is scheduled for the Thursday sync",
    "The audit trail depends on the backup rotation",
    "The style guide was handed to the customer survey group",
    "The mobile build is owned by the regional team",
    "The staging cluster was moved to the autumn roadmap",
    "The customer survey is tracked in a shared folder",
    "The backup rotation was reviewed with the platform group",
]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MemoryStore(Path(self.temporary.name) / "memorysafe.sqlite3")

    def fill(self) -> None:
        for text in FILLER:
            self.store.remember(text, "other", source="automatic")

    def texts(self, query: str) -> list[str]:
        return [hit["content"] for hit in self.store.find(query, 5, record=False)]


class RarityTests(Base):
    def test_the_rarer_word_decides_which_memory_comes_first(self) -> None:
        """"Which platforms ship first" ranked a note that said "ships" above the one about
        platforms: six notes mention shipping, one lists the platforms."""
        self.fill()
        for n in range(6):
            self.store.remember(f"Build {n + 1} ships on the Monday after review", "project", source="automatic")
        self.store.remember("Target platforms are Windows and Linux at launch", "project", source="automatic")
        self.assertIn("Target platforms", self.texts("which platforms ship first")[0])

    def test_one_shared_common_word_is_not_an_answer(self) -> None:
        self.fill()
        for n in range(4):
            self.store.remember(f"Module {n} review happens every second Tuesday", "project", source="automatic")
        self.assertEqual(self.texts("who is the lawyer doing the trademark review in Norway"), [])


class NamesAndNumbersTests(Base):
    def test_a_question_about_a_name_the_store_has_never_heard_returns_nothing(self) -> None:
        self.fill()
        self.store.remember("Publisher talk: Fernhollow Games offered 40,000 for a 30% revenue share", "project")
        self.assertEqual(self.texts("Who published Hollow Knight?"), [])
        self.assertIn("Fernhollow", self.texts("what did the publisher Fernhollow offer")[0])

    def test_a_different_number_is_a_different_thing(self) -> None:
        self.fill()
        self.store.remember("Known bug 212: the player clips through the lighthouse door", "project")
        self.assertEqual(self.texts("bug 305"), [])
        self.assertIn("lighthouse", self.texts("bug 212")[0])

    def test_two_shared_words_survive_a_changed_number(self) -> None:
        # The 10am note answers "daily standup 9am": it is the same standup, moved.
        self.store.remember("The daily standup moved to 10am.", "project")
        self.assertEqual(len(self.texts("daily standup 9am")), 1)


class WordFormTests(Base):
    def test_a_plural_in_ies_meets_its_singular(self) -> None:
        self.fill()
        self.store.remember("Enemy AI uses a hand-rolled state machine, not behaviour trees", "project")
        self.assertIn("state machine", self.texts("how do enemies decide what to do")[0])

    def test_a_longer_ending_meets_the_shorter_word(self) -> None:
        self.assertEqual(storage._fuzzy("publish", "publisher"), storage.FUZZY_MATCH)
        self.assertEqual(storage._fuzzy("deploy", "deployment"), storage.FUZZY_MATCH)
        self.assertEqual(storage._fuzzy("cat", "category"), 0.0)
        self.assertEqual(storage._fuzzy("organ", "organization"), 0.0)
        self.assertEqual(storage._fuzzy("station", "stationery"), storage.FUZZY_MATCH)


class KeywordTests(Base):
    def test_a_search_in_other_words_finds_the_memory_by_its_keywords(self) -> None:
        self.fill()
        self.store.remember(
            "Save system uses JSON files in user://saves with a version field", "project",
            keywords=["savegame", "persistence", "serialization"],
        )
        self.assertEqual(self.texts("how is persistence handled"), ["Save system uses JSON files in user://saves with a version field"])

    def test_without_keywords_the_same_search_finds_nothing(self) -> None:
        self.fill()
        self.store.remember("Save system uses JSON files in user://saves with a version field", "project")
        self.assertEqual(self.texts("how is persistence handled"), [])

    def test_keywords_are_folded_bounded_and_never_repeat_the_text(self) -> None:
        cleaned = storage.clean_keywords(["Persistence", "save system", "SÉRIALISATION", "the", "x"] + ["w%d" % n for n in range(40)],
                                         "Save system uses JSON files")
        words = cleaned.split()
        self.assertEqual(words[:2], ["persistence", "serialisation"])
        self.assertNotIn("save", words)
        self.assertLessEqual(len(words), storage.MAX_SEARCH_TERMS)
        self.assertEqual(storage.clean_keywords("alpha, beta; gamma", ""), "alpha beta gamma")
        self.assertEqual(storage.clean_keywords(None, "anything"), "")

    def test_they_are_search_words_only(self) -> None:
        saved = self.store.remember("The tide tables are in the blue binder", "other", keywords=["almanac", "schedule"])
        hit = self.store.find("almanac", 5, record=False)[0]
        self.assertEqual(hit["content"], "The tide tables are in the blue binder")
        self.assertNotIn("almanac", str(hit))
        self.assertEqual(self.store.explain(saved["memory_id"])["keywords"], ["almanac", "schedule"])
        # Never compared when deciding duplicates: the same keywords on another fact do not merge them.
        other = self.store.remember("The pump manual is on the top shelf", "other", keywords=["almanac", "schedule"])
        self.assertEqual(other["decision"], "STORE")

    def test_saying_it_again_adds_its_keywords(self) -> None:
        first = self.store.remember("The tide tables are in the blue binder", "other", keywords=["almanac"])
        self.store.remember("The tide tables are in the blue binder", "other", keywords=["timetable", "almanac"])
        self.assertEqual(self.store.explain(first["memory_id"])["keywords"], ["almanac", "timetable"])


class CaptureKeywordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = patch.dict(os.environ, {"MEMORYSAFE_DB_PATH": str(Path(self.temporary.name) / "memorysafe.sqlite3")})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = server._store()
        self.store.set_automatic_mode(True)

    def test_each_fact_takes_its_own_keywords(self) -> None:
        batch = server.automatic_capture(
            facts=["We use Postgres 17 in production", "Robin owns the billing service"],
            category="project",
            keywords=["database, sql", "invoices, payments"],
        )
        self.assertEqual(batch.saved, 2)
        self.assertEqual(self.store.explain(batch.results[0].memory_id)["keywords"], ["database", "sql"])
        self.assertEqual(self.store.explain(batch.results[1].memory_id)["keywords"], ["invoices", "payments"])

    def test_a_secret_cannot_ride_in_as_a_keyword(self) -> None:
        batch = server.automatic_capture(
            facts=["Robin owns the billing service"], category="project",
            keywords=["invoices, sk_live_ABCDEFGHIJKLMNOPQRSTUV0123"],
        )
        self.assertEqual(batch.saved, 1)
        self.assertEqual(self.store.explain(batch.results[0].memory_id)["keywords"], [])


class CapacityOrderTests(Base):
    """MEMORYSAFE_MAX_ACTIVE: which unprotected memories give way first."""

    def note(self, text: str, category: str = "other") -> str:
        return str(self.store.remember(text, category, source="automatic")["memory_id"])

    def state(self, memory_id: str) -> str:
        return str(self.store.explain(memory_id)["state"])

    def test_a_rule_nobody_looked_up_outlives_status_notes_nobody_looked_up(self) -> None:
        """Never-recalled went first, and a constraint saved once has no recalls to show,
        so a rule nobody had needed to look up was evicted before a week-old status note."""
        rule = self.note("The crane must be inspected before any lift over two tonnes")
        status = [self.note(text) for text in (
            "Waiting on the supplier to confirm the pallet count",
            "Looked into the forklift booking with the yard office",
            "Chased the surveyor about the east boundary sketch",
        )]
        with patch.dict(os.environ, {"MEMORYSAFE_MAX_ACTIVE": "3"}):
            self.note("Ordered more hi-vis vests in medium and large")
        self.assertEqual(self.state(rule), "active")
        self.assertEqual(sorted(self.state(one) for one in status), ["active", "evicted", "evicted"])

    def test_a_memory_that_was_used_outlives_newer_ones_that_were_not(self) -> None:
        used = self.note("The gate code rota is kept by the site office")
        for _ in range(3):
            self.store.find("gate code rota", 1)
        newer = [self.note(text) for text in (
            "Asked the yard office about skip collection times",
            "Told the surveyor the east boundary sketch can wait",
        )]
        with patch.dict(os.environ, {"MEMORYSAFE_MAX_ACTIVE": "2"}):
            self.note("Booked the cherry picker through the hire desk")
        self.assertEqual(self.state(used), "active")
        self.assertIn("evicted", [self.state(one) for one in newer])

    def test_protected_memories_and_the_new_one_are_never_evicted(self) -> None:
        kept = self.store.remember("Never store raw chicken above ready-to-eat food", "decision")["memory_id"]
        with patch.dict(os.environ, {"MEMORYSAFE_MAX_ACTIVE": "1"}):
            new = self.note("Looked into the walk-in thermostat reading")
        self.assertEqual((self.state(kept), self.state(new)), ("active", "active"))


if __name__ == "__main__":
    unittest.main()
