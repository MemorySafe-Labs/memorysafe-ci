"""Independent release review. Run with PYTHONPATH set to the target plugin src.

These assertions express required behavior; failures reproduce release blockers.
Synthetic temporary stores only. No installation or live memory access.
"""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from memorysafe_chatgpt.storage import MemoryStore


class PluginReview(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'MEMORYSAFE_MAX_ACTIVE': '',
            'MEMORYSAFE_DB_PATH': str(Path(self.tmp.name) / 'memory.db'),
            'MEMORYSAFE_STATE_DIR': str(Path(self.tmp.name) / 'state'),
        })
        self.env.start()
        self.store = MemoryStore(Path(self.tmp.name) / 'memory.db')

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_lower_confidence_negation_cannot_replace_manual_via_merge(self):
        text = 'The deployment is approved for the production server.'
        original = self.store.remember(text, 'project')
        incoming = self.store.remember(
            'The deployment is not approved for the production server.', 'project', source='agent')
        self.assertEqual(self.store.explain(original['memory_id'])['content'], text)
        self.assertEqual(incoming.get('lifecycle'), 'needs_review')

    def test_distinct_systems_do_not_supersede_each_other(self):
        original = self.store.remember('The backend database uses Postgres.', 'project')
        self.store.remember('The analytics database switched from Postgres to MySQL.', 'project')
        self.assertEqual(self.store.explain(original['memory_id'])['state'], 'active')

    def test_staging_and_production_versions_are_separate(self):
        original = self.store.remember('The staging service runs version 1.2.3.', 'project')
        self.store.remember('The production service now runs version 2.3.4.', 'project')
        self.assertEqual(self.store.explain(original['memory_id'])['state'], 'active')

    def test_two_dated_meetings_need_evidence_of_replacement(self):
        original = self.store.remember('The Apollo team meeting is on 3 Nov 2026.', 'project')
        self.store.remember('The Apollo team meeting is on 10 Nov 2026.', 'project')
        self.assertEqual(self.store.explain(original['memory_id'])['state'], 'active')

    def test_currency_changes_are_not_exact_duplicates(self):
        original = self.store.remember('The client budget is $50,000.', 'project')
        incoming = self.store.remember('The client budget is €50,000.', 'project')
        self.assertNotEqual(incoming['memory_id'], original['memory_id'])

    def test_capacity_cannot_discard_stronger_unresolved_fact(self):
        os.environ['MEMORYSAFE_MAX_ACTIVE'] = '1'
        original = self.store.remember('The daily standup is at 9am.', 'other')
        incoming = self.store.remember('The daily standup moved to 10am.', 'other', source='agent')
        self.assertEqual(incoming.get('lifecycle'), 'needs_review')
        self.assertEqual(self.store.explain(original['memory_id'])['state'], 'active')


class AdditionalGuards(unittest.TestCase):
    setUp = PluginReview.setUp
    tearDown = PluginReview.tearDown

    def test_legacy_normalized_currency_key_stays_searchable_and_separate(self):
        dollar = self.store.remember('The client budget is $50,000.', 'project')
        euro = self.store.remember('The client budget is €50,000.', 'project', source='agent')
        self.assertEqual(euro.get('lifecycle'), 'needs_review')
        again = self.store.remember('The client budget is €50,000.', 'project', source='agent')
        self.assertEqual(again['memory_id'], euro['memory_id'])
        self.assertEqual(self.store.explain(dollar['memory_id'])['content'], 'The client budget is $50,000.')

    def test_close_dates_are_reviewed_before_similarity(self):
        original = self.store.remember('The Apollo meeting at the downtown office is on 21 Nov 2026.', 'project')
        incoming = self.store.remember('The Apollo meeting at the downtown office is on 22 Nov 2026.', 'project')
        self.assertEqual(incoming.get('lifecycle'), 'needs_review')
        self.assertEqual(self.store.explain(original['memory_id'])['state'], 'active')

    def test_changed_named_deadline_and_explicit_reschedule_still_work(self):
        for old, new in (
            ('The Apollo deadline is 21 Nov 2026.', 'The Apollo deadline is 22 Nov 2026.'),
            ('The team meeting is on 21 Nov 2026.', 'The team meeting moved to 22 Nov 2026.'),
        ):
            original = self.store.remember(old, 'project')
            incoming = self.store.remember(new, 'project')
            self.assertEqual(incoming.get('lifecycle'), 'superseded_previous')
            self.assertEqual(self.store.explain(original['memory_id'])['state'], 'superseded')

    def test_same_product_version_on_another_machine_needs_review(self):
        original = self.store.remember('Widget 1.2.3 is installed on staging.', 'project')
        self.store.remember('Widget 2.3.4 is now installed on production.', 'project')
        self.assertEqual(self.store.explain(original['memory_id'])['state'], 'active')

    def test_replaced_version_on_another_machine_does_not_replace_this_one(self):
        original = self.store.remember('Widget 1.2.3 is installed on staging.', 'project')
        self.store.remember('Widget 2.3.4 is now installed on production, replacing 1.2.3.', 'project')
        self.assertEqual(self.store.explain(original['memory_id'])['state'], 'active')

    def test_near_identical_names_do_not_merge(self):
        text = 'Alexandra owns the release approval for the production server.'
        original = self.store.remember(text, 'project')
        incoming = self.store.remember('Alexandre owns the release approval for the production server.', 'project', source='agent')
        self.assertNotEqual(incoming['memory_id'], original['memory_id'])
        self.assertEqual(self.store.explain(original['memory_id'])['content'], text)

    def test_lower_confidence_format_variant_cannot_replace_text(self):
        text = 'I prefer concise answers in every reply'
        original = self.store.remember(text, 'preference')
        self.store.remember('I prefer concise answers in every single reply', 'preference', source='agent')
        self.assertEqual(self.store.explain(original['memory_id'])['content'], text)

    def test_later_capacity_pressure_keeps_both_unresolved_sides(self):
        os.environ['MEMORYSAFE_MAX_ACTIVE'] = '1'
        original = self.store.remember('The daily standup is at 9am.', 'other')
        incoming = self.store.remember('The daily standup moved to 10am.', 'other', source='agent')
        self.store.remember('The side entrance faces the garden.', 'other')
        for record in (original, incoming):
            self.assertEqual(self.store.explain(record['memory_id'])['state'], 'active')
        self.store.resolve_conflict(incoming['conflict_id'], 'keep_both', confirm=True)
        self.store.remember('The loading bay faces the river.', 'other')
        self.assertEqual(self.store.health()['active_memories'], 1)


if __name__ == '__main__':
    unittest.main()
