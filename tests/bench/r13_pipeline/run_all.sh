#!/bin/bash
# Round 13 pipeline, fixed in GATES_R13_PREREG.md. Resumable: every step skips per-run files / logs that exist.
set -u
R=/workspace/gates_proto/r13
mkdir -p $R && cd $R
P=/workspace/gates_proto/repo/tests/bench/r13_pipeline
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home
REPO=/workspace/gates_proto/repo
log() { echo "$1 $(date)" >> $R/pipeline.log; }
log start
grep -q ALL_DONE heldout.log 2>/dev/null || bash $P/runjobs.sh $P/jobs_heldout.txt heldout.log 7
log "heldout done"
if ! grep -q ALL_DONE abl_L1.log 2>/dev/null; then
  bash $P/make_abl_L1.sh >> abl_L1.log 2>&1
  grep -- "--arm r12 " $P/jobs_heldout.txt | sed 's#/r13/heldout#/r13/abl_L1#' > abl_L1_jobs.txt
  R12_SRC=/tmp/r13abl_L1/src bash $P/runjobs.sh abl_L1_jobs.txt abl_L1.log 7
fi
log "ablation L1 done"
grep -q ALL_DONE latency.log 2>/dev/null || bash $P/runjobs.sh $P/jobs_latency.txt latency.log 1
log "latency done"
if [ ! -f profile.json ]; then
  (cd $REPO && PYTHONPATH=src python tests/bench/gates_profile.py --sizes 1000 5000 --runs 12 --writes 30 \
     --arms shipped r12 r13 --template-cache /tmp/r6cache \
     --interleave-seed 13 --run-dir $R/profile_runs --json $R/profile.json) > profile.log 2>&1
fi
log "profile done"
FWARMS="base base_cap shipped shipped_cap r12 r13 r12_under r13_under"
[ -f fw_normal.json ] || (cd $REPO && python tests/bench/gates_r13_firstwrite.py --runs 21 --arms $FWARMS --order-seed 131 --json $R/fw_normal.json) > fw_normal.log 2>&1
[ -f fw_single.json ] || (cd $REPO && python tests/bench/gates_r13_firstwrite.py --cpu 0 --runs 21 --arms $FWARMS --order-seed 132 --json $R/fw_single.json) > fw_single.log 2>&1
log "first write done"
[ -f importtime/importtime.json ] || bash $P/importtime.sh $R/importtime > importtime.log 2>&1
log "importtime done"
if [ ! -f suite.log ] || ! grep -q " passed" suite.log; then
  (cd $REPO && PYTHONPATH=src timeout 2400 python -m pytest -q tests -p no:cacheprovider --deselect tests/test_stdio_transport.py) > suite.log 2>&1
fi
log "suite done"
if [ ! -f findings_pytest.log ] || ! grep -q " passed" findings_pytest.log; then
  (cd $REPO && PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_gates_r13.py) > findings_pytest.log 2>&1
  (cd $REPO && PYTHONPATH=src python $P/lumi_review_checks.py) > findings_probes.log 2>&1
fi
log "findings done"
grep -q ALL_DONE /workspace/gates_proto/real/replay_v5.log 2>/dev/null || bash $P/run_replay_v5.sh
(cd /workspace/gates_proto/real && python $REPO/tests/bench/real_replay/real_replay_rescore.py --runs runs_v5 --out rescore_v5.json \
   && python check_v5.py > check_v5.json) >> $R/pipeline.log 2>&1
(cd $REPO && PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_gates_r10_private.py tests/test_gates_r11_private.py) > /workspace/gates_proto/real/private_tests_r13.log 2>&1
log "replay v5 done"
echo PIPELINE_DONE >> $R/pipeline.log
