#!/bin/bash
# Real replay v3 (exploratory; GATES_R11_PREREG.md section 7). Private text never leaves /workspace/gates_proto/real/.
# The source is the existing read-only copy; the live store, its server and port 8765 are never touched.
cd /workspace/gates_proto/real
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home
S=/workspace/gates_proto/repo/tests/bench/real_replay/real_replay.py
mkdir -p runs_v3 work
for rep in 0 1 2; do for ur in "" "--user-resolutions"; do for mode in lived noprotect; do for cap in 0 46 69; do
  case $((rep % 3)) in 0) order="shipped r10 r10fast r11";; 1) order="r11 shipped r10 r10fast";; *) order="r10fast r11 shipped r10";; esac
  for arm in $order; do
    python $S --source source/memorysafe_copy.sqlite3 --arm $arm --mode $mode --cap $cap --rep $rep --out runs_v3 --work work $ur
  done; done; done; done; done >> replay_v3.log 2>&1
echo ALL_DONE >> replay_v3.log
