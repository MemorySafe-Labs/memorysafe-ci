#!/bin/bash
# Round 12 pipeline, fixed in GATES_R12_PREREG.md. Resumable: every step skips per-run files that exist.
set -u
cd /workspace/gates_proto/r12
P=/workspace/gates_proto/repo/tests/bench/r12_pipeline
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home
REPO=/workspace/gates_proto/repo
echo "start $(date)" >> pipeline.log
grep -q ALL_DONE heldout.log 2>/dev/null || bash $P/runjobs.sh $P/jobs_heldout.txt heldout.log 7
echo "heldout done $(date)" >> pipeline.log
grep -q ALL_DONE latency.log 2>/dev/null || bash $P/runjobs.sh $P/jobs_latency.txt latency.log 1
echo "latency done $(date)" >> pipeline.log
if [ ! -f profile.json ]; then
  (cd $REPO && PYTHONPATH=src python tests/bench/gates_profile.py --sizes 1000 5000 --runs 12 --writes 30 \
     --arms shipped r10_stale r11 r12 --template-cache /tmp/r6cache \
     --interleave-seed 12 --run-dir /workspace/gates_proto/r12/profile_runs --json /workspace/gates_proto/r12/profile.json) > profile.log 2>&1
fi
echo "profile done $(date)" >> pipeline.log
if [ ! -f suite.log ] || ! grep -q " passed" suite.log; then
  (cd $REPO && PYTHONPATH=src timeout 2400 python -m pytest -q tests -p no:cacheprovider --deselect tests/test_stdio_transport.py) > suite.log 2>&1
fi
echo "suite done $(date)" >> pipeline.log
grep -q ALL_DONE /workspace/gates_proto/real/replay_v4.log 2>/dev/null || bash $P/run_replay_v4.sh
(cd /workspace/gates_proto/real && python $REPO/tests/bench/real_replay/real_replay_rescore.py --runs runs_v4 --out rescore_v4.json) >> pipeline.log 2>&1
(cd $REPO && PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_gates_r10_private.py tests/test_gates_r11_private.py) > /workspace/gates_proto/real/private_tests_r12.log 2>&1
echo "replay v4 done $(date)" >> pipeline.log
echo PIPELINE_DONE >> pipeline.log
