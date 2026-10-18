#!/bin/bash
# Real replay v5 (exploratory; GATES_R13_PREREG.md s8). Private text never leaves /workspace/gates_proto/real/.
# The source is the existing read-only copy; the live store, its server and port 8765 are never touched.
# Arms: shipped (this code, flags off), r12 (r12 code: REPLAY_SRC=/tmp/r12tree/src), r13 (this code).
cd /workspace/gates_proto/real
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home
S=/workspace/gates_proto/repo/tests/bench/real_replay/real_replay.py
mkdir -p runs_v5 work_v5
for rep in 0 1 2; do for ur in "" "--user-resolutions"; do for mode in lived noprotect; do for cap in 0 46 69; do
  case $((rep % 3)) in 0) order="shipped r12 r13";; 1) order="r13 shipped r12";; *) order="r12 r13 shipped";; esac
  for arm in $order; do
    if [ "$arm" = r12 ]; then SRC=/tmp/r12tree/src; else SRC=; fi
    REPLAY_SRC=$SRC python $S --source source/memorysafe_copy.sqlite3 --arm $arm --mode $mode --cap $cap --rep $rep --out runs_v5 --work work_v5 $ur
  done; done; done; done; done >> replay_v5.log 2>&1
echo ALL_DONE >> replay_v5.log
