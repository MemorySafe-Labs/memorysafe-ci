"""Independent review probes: synthetic temporary stores only."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from memorysafe_chatgpt import storage, gates, gates_r6, gates_stale
from memorysafe_chatgpt.lifecycle import record_relation

clean = {k: v for k, v in os.environ.items() if not k.startswith('MEMORYSAFE_')}
flags = dict(MEMORYSAFE_GATE_CAPACITY='1', MEMORYSAFE_GATE_USEFUL='1',
             MEMORYSAFE_GATE_EXTINCTION='1', MEMORYSAFE_GATE_STALE='1',
             MEMORYSAFE_GATE_R6_W_GOAL='0', MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED='99')

with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**clean, **flags,
        'MEMORYSAFE_STATE_DIR': tmp, 'MEMORYSAFE_DB_PATH': tmp+'/m.sqlite3'}, clear=True):
    s = storage.MemoryStore(Path(tmp)/'m.sqlite3')
    a = s.remember('The cobalt notebook contains sketches', 'other', importance=.1)['memory_id']
    b = s.remember('A silver bicycle stands near the garage', 'other', importance=.2)['memory_id']
    with s._session() as c:
        record_relation(c, old_memory_id=a, new_memory_id=b, decision='REVIEW', status='open',
                        reason='Synthetic open review', evidence='test', confidence=.5, source='test', created_at=storage._now())
        moved = gates.enforce_capacity(s,c,1,keep_id=b)
        print('OPEN_CONFLICT_EVICTED', a in moved)

    with patch.object(storage, '_now', return_value='2026-01-01T00:00:00+00:00'):
        durable = s.remember('The building was installed on its permanent concrete foundation in 1980', 'other')['memory_id']
    with patch.object(storage, '_now', return_value='2026-03-01T00:00:00+00:00'):
        s._r10_last_sweep = None
        s.remember('A violet umbrella hangs beside the entrance', 'other')
    with s._session() as c:
        print('DURABLE_HISTORICAL_FACT_FLAGGED', durable in gates_stale.stale_ids(c))

    s.restore(a, confirm=True)
    with s._session() as c:
        fc = gates_r6._feature_cache(s)
        before = gates_r6._snapshot(c,fc,None).rows[a][3]
    # Separate database connection models another assistant changing an existing row.
    import sqlite3
    with sqlite3.connect(s.database_path) as other:
        other.execute('UPDATE memories SET category=? WHERE id=?', ('project',a))
    with s._session() as c:
        cached = gates_r6._snapshot(c,fc,None).rows[a][3]
        actual = c.execute('SELECT category FROM memories WHERE id=?',(a,)).fetchone()[0]
        print('CACHE_MISSES_CATEGORY_CHANGE', cached != actual)

    with s._session() as c:
        gates_stale._flag(s,c,b,a,'synthetic newer activity','2099-01-01T00:00:00+00:00')
    with patch.dict(os.environ, {'MEMORYSAFE_GATE_STALE':'0'}):
        s.restore(b,confirm=True)
    with s._session() as c:
        print('RESTORE_WHILE_OFF_LEAVES_FLAG', b in gates_stale.stale_ids(c))
        gates_r6.after_resolution(s,c,'revert',b,a,'2099-01-02T00:00:00+00:00')
        print('REVERT_LEAVES_STALE_EVIDENCE', b in gates_stale.stale_ids(c))

    with s._session() as c:
        ix = gates.index_for(s)
        gates._current_book(c,ix)
        for mid in list(ix.norm):
            ix.neighbours(mid)
    s.forget(b,confirm=True)
    saved = gates.save_indexes()
    path = Path(str(s.database_path)+'.gates-index')
    print('NORMAL_EXIT_SAVE_WRITES_FORGOTTEN_TEXT', bool(saved and path.exists() and 'silver bicycle' in path.read_text()))
    gates._INDEXES.clear()

scored = [(3., {'id':'old'}), (2., {'id':'middle'}), (1., {'id':'new'})]
gates_stale.stale_reorder(scored, {'old':{'middle'}, 'middle':{'new'}})
print('CHAIN_ORDER_VIOLATION', [r['id'] for _,r in sorted(scored,key=lambda x:x[0],reverse=True)] != ['new','middle','old'])
