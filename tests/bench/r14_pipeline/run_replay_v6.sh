#!/bin/bash
# Real replay v6 (exploratory; GATES_R14_PREREG.md s7). Private text never leaves /workspace/gates_proto/real/.
# The source is the existing read-only copy; the live store, its server and port 8765 are never touched.
# Arms: shipped (this code, flags off), r13 (r13 code: REPLAY_SRC=/tmp/r13tree/src), r14 (this code).
cd /workspace/gates_proto/real
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home
S=/workspace/gates_proto/repo/tests/bench/real_replay/real_replay.py
mkdir -p runs_v6 work_v6
for rep in 0 1 2; do for ur in "" "--user-resolutions"; do for mode in lived noprotect; do for cap in 0 46 69; do
  case $((rep % 3)) in 0) order="shipped r13 r14";; 1) order="r14 shipped r13";; *) order="r13 r14 shipped";; esac
  for arm in $order; do
    if [ "$arm" = r13 ]; then SRC=/tmp/r13tree/src; else SRC=; fi
    REPLAY_SRC=$SRC python $S --source source/memorysafe_copy.sqlite3 --arm $arm --mode $mode --cap $cap --rep $rep --out runs_v6 --work work_v6 $ur
  done; done; done; done; done >> replay_v6.log 2>&1
echo ALL_DONE >> replay_v6.log
