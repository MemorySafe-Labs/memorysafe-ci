import os, tempfile, sqlite3
from pathlib import Path
from unittest.mock import patch
from memorysafe_chatgpt import storage, gates, gates_r6
flags={'MEMORYSAFE_GATE_CAPACITY':'1','MEMORYSAFE_GATE_USEFUL':'1','MEMORYSAFE_GATE_STALE':'1'}
with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {**flags,'MEMORYSAFE_STATE_DIR':d}):
 s=storage.MemoryStore(Path(d)/'m.sqlite3')
 a=s.remember('Goal: launch the amber notebook','other',importance=.2)['memory_id']
 fc=gates_r6._feature_cache(s)
 with s._session() as c:
  book=gates_r6._snapshot(c,fc,None)
  gates_r6._feats(fc,c,[a],book.rows)
  print('GOAL_BEFORE',fc.by_id[a][1][2])
 with sqlite3.connect(s.database_path) as c:
  c.execute('UPDATE memories SET content=? WHERE id=?',('Goal launch the amber notebook',a))
 with s._session() as c:
  book=gates_r6._snapshot(c,fc,None)
  gates_r6._feats(fc,c,[a],book.rows)
  print('CONTENT_ONLY_CACHE_STALE',fc.by_id[a][1][2] != gates_r6.features('Goal launch the amber notebook')[2])
 try:
  with s._session() as c:
   c.execute('UPDATE memories SET category=? WHERE id=?',('project',a))
   gates_r6._snapshot(c,fc,None)
   raise RuntimeError('simulate aborted write')
 except RuntimeError: pass
 with s._session() as c:
  cached=gates_r6._snapshot(c,fc,None).rows[a][3]
  real=c.execute('SELECT category FROM memories WHERE id=?',(a,)).fetchone()[0]
  print('ROLLBACK_CACHE_STALE',cached != real,cached,real)
 # Trigger activity persists after gates disabled.
 with s._session() as c: before=gates.log_position(c)
 with patch.dict(os.environ,{k:'0' for k in flags}):
  s.remember('A purple umbrella rests beside the door','other',importance=.2)
 with s._session() as c: print('LOG_WRITES_AFTER_FLAGS_DISABLED',gates.log_position(c)>before)
 for text in ['The furnace was installed in 1980. The permit is pending.', 'The archive quotes "The permit is pending".', 'The permit was previously pending.']:
  print('STATUS',repr(text),gates_r6.status_kind(text))
 gates_r6.entities('Goal launch the amber notebook')
 inner=gates_r6.entities.__wrapped__
 s.forget(a,confirm=True)
 hits=inner.cache_info().hits
 inner('Goal launch the amber notebook')
 print('INNER_ENTITY_CACHE_RETAINS_FORGOTTEN_TEXT',inner.cache_info().hits>hits)
