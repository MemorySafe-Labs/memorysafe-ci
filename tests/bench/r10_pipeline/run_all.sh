#!/bin/bash
# Round 10 pipeline, fixed in GATES_R10_PREREG.md. Resumable: every step skips per-run files that exist.
set -u
cd /workspace/gates_proto/r10
cp -n /workspace/gates_proto/repo/tests/bench/r10_pipeline/{runjobs.sh,jobs_heldout.txt,jobs_latency.txt,run_replay_v2.sh} .
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home
REPO=/workspace/gates_proto/repo
echo "start $(date)" >> pipeline.log
grep -q ALL_DONE heldout.log 2>/dev/null || bash runjobs.sh jobs_heldout.txt heldout.log 7
echo "heldout done $(date)" >> pipeline.log
grep -q ALL_DONE latency.log 2>/dev/null || bash runjobs.sh jobs_latency.txt latency.log 1
echo "latency done $(date)" >> pipeline.log
if [ ! -f profile.json ]; then
  (cd $REPO && PYTHONPATH=src python tests/bench/gates_profile.py --sizes 1000 5000 --runs 12 --writes 30 \
     --arms shipped r7x_nogoal_useful_ext r10_stale --template-cache /tmp/r6cache \
     --interleave-seed 10 --run-dir /workspace/gates_proto/r10/profile_runs --json /workspace/gates_proto/r10/profile.json) > profile.log 2>&1
fi
echo "profile done $(date)" >> pipeline.log
if [ ! -f suite.log ] || ! grep -q " passed" suite.log; then
  (cd $REPO && PYTHONPATH=src timeout 2400 python -m pytest -q tests -p no:cacheprovider) > suite.log 2>&1
fi
echo "suite done $(date)" >> pipeline.log
grep -q ALL_DONE /workspace/gates_proto/real/replay_v2.log 2>/dev/null || bash run_replay_v2.sh
echo "replay v2 done $(date)" >> pipeline.log
echo PIPELINE_DONE >> pipeline.log
