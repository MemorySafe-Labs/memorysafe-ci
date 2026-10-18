#!/bin/bash
# Exploratory attribution arm (GATES_R13_PREREG.md s6): the round-13 code with ONLY the open-conflict
# fix (reviewer A P1, L1) reverted -- unresolved_ids() returns nothing and _move_out has no open-conflict clause.
set -e
D=/tmp/r13abl_L1
rm -rf $D && mkdir -p $D && git -C /workspace/gates_proto/repo archive "${1:-HEAD}" src | tar -x -C $D
/workspace/gates_proto/venv_r6/bin/python - <<'PY'
p = '/tmp/r13abl_L1/src/memorysafe_chatgpt/gates.py'
s = open(p).read()
a = '    return frozenset(str(r[0]) for r in connection.execute(\n        "SELECT old_memory_id FROM memory_relations WHERE status = \'open\' "'
assert s.count(a) == 1
s = s.replace(a, '    return frozenset()\n' + a)
b = '''        "AND id NOT IN (SELECT old_memory_id FROM memory_relations WHERE status = 'open' "
        "UNION SELECT new_memory_id FROM memory_relations WHERE status = 'open')",'''
assert s.count(b) == 1
s = s.replace(b, '        "",')
open(p, 'w').write(s)
PY
echo "ablation tree ready: $D/src"
