from contextlib import closing
import sqlite3, unittest
from test_gates_r13 import Case
from memorysafe_chatgpt import gates, gates_r6, gates_stale

class Independent(Case):
    def test_log_gap_invalidates_original_text_features(self):
        mid=self.put('Goal: launch the amber notebook')
        fc=gates_r6._feature_cache(self.store)
        with self.store._session() as c:
            book=gates_r6._snapshot(c,fc,None)
            gates_r6._feats(fc,c,[mid],book.rows)
            self.assertTrue(fc.by_id[mid][1][2])
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('UPDATE memories SET content=? WHERE id=?',('Goal launch the amber notebook',mid))
            for i in range(gates.LOG_KEEP+2):
                c.execute('UPDATE memories SET importance=? WHERE id=?',(.2+(i%2)/10,mid))
        with self.store._session() as c:
            book=gates_r6._snapshot(c,fc,None)
            gates_r6._feats(fc,c,[mid],book.rows)
            self.assertEqual(fc.by_id[mid][1][2],gates_r6.features('Goal launch the amber notebook')[2], 'log-gap rebuild reused old original-text features')

    def test_repair_detects_altered_trigger_with_current_marker(self):
        self.put('amber notebook'); self.put('violet bicycle')
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('DROP TRIGGER memorysafe_gate_changes_upd')
            c.execute('CREATE TRIGGER memorysafe_gate_changes_upd AFTER UPDATE ON memories BEGIN /* memorysafe-gate-log v2 */ SELECT 1; END')
        with self.store._session() as c:
            self.assertFalse(gates.change_log_installed(c))
            self.assertFalse(gates.change_log_status(c)['installed'])
            gates.ensure_change_log(c)
            before=gates.log_position(c)
            c.execute("UPDATE memories SET category='project'")
            self.assertGreater(gates.log_position(c),before,'altered trigger treated as valid')

    def test_repair_checks_table_even_with_all_markers(self):
        self.put('amber notebook'); self.put('violet bicycle')
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('ALTER TABLE memorysafe_gate_changes ADD COLUMN extra TEXT')
        with self.store._session() as c:
            gates.ensure_change_log(c)
            self.assertTrue(gates.change_log_status(c)['table_ok'],'fast path skipped shape validation')

    def test_historical_coordination_not_age_demoted(self):
        text='The permit was pending and under review in 2020.'
        mid=self.put(text)
        self.later(days=60)
        self.store._r10_last_sweep=None
        self.put('A violet umbrella hangs beside the doorway')
        with self.store._session() as c:
            self.assertNotIn(mid,gates_stale.stale_ids(c),f'historical note classified {gates_r6.status_kind(text)}')

class Followup(Case):
    def test_repair_gap_discards_features(self):
        mid=self.put('Goal: launch the amber notebook')
        fc=gates_r6._feature_cache(self.store)
        with self.store._session() as c:
            book=gates_r6._snapshot(c,fc,None)
            gates_r6._feats(fc,c,[mid],book.rows)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('DROP TRIGGER memorysafe_gate_changes_upd')
            c.execute('UPDATE memories SET content=? WHERE id=?',('Goal launch the amber notebook',mid))
        with self.store._session() as c:
            book=gates_r6._snapshot(c,fc,None)
            gates_r6._feats(fc,c,[mid],book.rows)
            self.assertFalse(fc.by_id[mid][1][2])

    def test_ambiguous_clause_review_not_demotion(self):
        mid=self.put('The Halvorsen permit was pending and under review in 2020.')
        self.later(days=60)
        self.put('Called Halvorsen about the drawings')
        with self.store._session() as c:
            self.assertNotIn(mid,gates_stale.stale_ids(c))
            self.assertIn(mid,gates_stale.status_reviews(c))
        self.assertEqual(gates_r6.status_kind('The permit is pending and under review.'),'present')

    def test_malformed_marker_is_repaired(self):
        self.put('amber notebook');self.put('violet bicycle')
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('DROP TRIGGER memorysafe_gate_changes_upd')
            c.execute('CREATE TRIGGER memorysafe_gate_changes_upd AFTER UPDATE ON memories BEGIN /* memorysafe-gate-log vx */ SELECT 1; END')
        with self.store._session() as c:
            self.assertFalse(gates.change_log_status(c)['installed'])
            gates.ensure_change_log(c)
            self.assertTrue(gates.change_log_status(c)['installed'])

if __name__=='__main__': unittest.main(verbosity=2)
