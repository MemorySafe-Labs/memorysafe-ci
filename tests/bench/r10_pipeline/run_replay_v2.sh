#!/bin/bash
# Real replay v2 (exploratory; GATES_R10_PREREG.md section 7). Private text never leaves /workspace/gates_proto/real/.
cd /workspace/gates_proto/real
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home
S=/workspace/gates_proto/repo/tests/bench/real_replay/real_replay.py
mkdir -p runs_v2 work
for rep in 0 1 2 3 4; do for ur in "" "--user-resolutions"; do for mode in lived noprotect; do for cap in 0 46 69; do
  case $((rep % 3)) in 0) order="shipped candidate r10";; 1) order="r10 shipped candidate";; *) order="candidate r10 shipped";; esac
  for arm in $order; do
    python $S --source source/memorysafe_copy.sqlite3 --arm $arm --mode $mode --cap $cap --rep $rep --out runs_v2 --work work $ur
  done; done; done; done; done >> replay_v2.log 2>&1
echo ALL_DONE >> replay_v2.log
