"""Task recall must include applicable rules without diluting direct answers."""
import tempfile
import unittest
from pathlib import Path

from memorysafe_chatgpt.storage import MemoryStore


class RecallContextCases:
    # Also run against the Hosted app role and its real PostgreSQL read policies.
    def seed(self):
        self.backend_fact = self.store.remember(
            "The backend of project Atlas is written in Python 3.12 with FastAPI.", "project"
        )["memory_id"]
        self.formatting = self.store.remember(
            "I prefer code formatted with Black and line length 100.", "preference"
        )["memory_id"]
        self.frontend = self.store.remember(
            "The Atlas frontend uses TypeScript with React 18 and Vite.", "project"
        )["memory_id"]
        self.styling = self.store.remember(
            "Atlas styling uses Tailwind CSS, no CSS-in-JS.", "decision"
        )["memory_id"]

    def test_endpoint_recalls_formatting_without_shared_words(self):
        self.seed()
        hits = self.store.find("Write a new FastAPI endpoint for Atlas in Python", 5)
        self.assertEqual(hits[0]["memory_id"], self.backend_fact)
        self.assertIn(self.formatting, {h["memory_id"] for h in hits})

    def test_component_recalls_project_styling(self):
        self.seed()
        hits = self.store.find("Add a React TypeScript component for the Atlas frontend", 5)
        self.assertEqual(hits[0]["memory_id"], self.frontend)
        self.assertIn(self.styling, {h["memory_id"] for h in hits})

    def test_vocabulary_bridges_are_not_specific_to_the_reported_tools(self):
        self.store.remember("The Juniper service uses Rust for its backend.", "project")
        rule = self.store.remember("Use tabs for code indentation.", "preference")["memory_id"]
        self.assertIn(rule, {h["memory_id"] for h in self.store.find("Write a Rust function for Juniper", 5)})
        self.store.remember("Juniper frontend components use Vue.", "project")
        rule = self.store.remember("Juniper styles use SCSS.", "decision")["memory_id"]
        self.assertIn(rule, {h["memory_id"] for h in self.store.find("Add a Vue component for Juniper", 5)})

    def test_context_does_not_displace_direct_matches_or_exceed_limit(self):
        self.seed()
        query = "Add a React TypeScript component for the Atlas frontend"
        self.assertEqual([h["memory_id"] for h in self.store.find(query, 1)], [self.frontend])
        self.assertLessEqual(len(self.store.find(query, 2)), 2)

    def test_an_unrelated_project_decision_is_not_supplemental_context(self):
        self.seed()
        wrong = self.store.remember("Borealis styling uses Bootstrap CSS.", "decision")["memory_id"]
        hits = self.store.find("Add a React TypeScript component for the Atlas frontend", 20)
        self.assertNotIn(wrong, {h["memory_id"] for h in hits})

    def test_unrelated_preferences_and_unmatched_queries_stay_out(self):
        self.seed()
        coffee = self.store.remember("I prefer black coffee.", "preference")["memory_id"]
        hits = self.store.find("Write a FastAPI endpoint for Atlas in Python", 20)
        self.assertNotIn(coffee, {h["memory_id"] for h in hits})
        self.assertEqual(self.store.find("How do I roast a chicken", 5), [])
        self.assertEqual(self.store.find("Build a Kotlin app for Zephyr", 5), [])

    def test_forgotten_rule_is_not_returned_and_read_only_recall_does_not_record(self):
        self.seed()
        self.store.forget(self.formatting, confirm=True)
        with self.store._session() as connection:
            before = connection.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
        hits = self.store.find("Write a FastAPI endpoint for Atlas in Python", 5, record=False)
        self.assertNotIn(self.formatting, {h["memory_id"] for h in hits})
        with self.store._session() as connection:
            after = connection.execute("SELECT COUNT(*) FROM decision_events").fetchone()[0]
        self.assertEqual(before, after)


class LocalRecallContextTests(RecallContextCases, unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MemoryStore(Path(self.directory.name) / "recall.sqlite3")
