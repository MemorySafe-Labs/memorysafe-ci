#!/bin/bash
# Round 14 pipeline, fixed in GATES_R14_PREREG.md. Resumable: every step skips per-run files / logs that exist.
# Box only. Never touches the live store, its server, port 8765, or the user's Macs.
set -u
R=/workspace/gates_proto/r14
mkdir -p $R && cd $R
P=/workspace/gates_proto/repo/tests/bench/r14_pipeline
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home
REPO=/workspace/gates_proto/repo
B=$REPO/tests/bench
log() { echo "$1 $(date)" >> $R/pipeline.log; }
log start
grep -q ALL_DONE heldout.log 2>/dev/null || R13_SRC=/tmp/r13tree/src bash $P/runjobs.sh $P/jobs_heldout.txt heldout.log 7
log "heldout done"
grep -q ALL_DONE latency.log 2>/dev/null || R13_SRC=/tmp/r13tree/src bash $P/runjobs.sh $P/jobs_latency.txt latency.log 1
log "latency done"
if [ ! -f profile.json ]; then
  (cd $REPO && R13_SRC=/tmp/r13tree/src PYTHONPATH=src python $B/gates_profile.py --sizes 1000 5000 --runs 12 --writes 30 \
     --arms shipped r13 r14 r14_off_log --template-cache /tmp/r6cache \
     --interleave-seed 14 --run-dir $R/profile_runs --json $R/profile.json) > profile.log 2>&1
fi
log "profile done"
CP="python $B/cold_probe_mac.py --templates /tmp/r14/cp_templates --base-src /tmp/d9tree/src --r13-src /tmp/r13tree/src"
mkdir -p cold
[ -f cold/normal.json ]      || (cd $REPO && $CP --runs 21 --order-seed 141 --json $R/cold/normal.json) > cold/normal.log 2>&1
[ -f cold/single_core.json ] || (cd $REPO && $CP --runs 21 --order-seed 142 --cpu 0 --json $R/cold/single_core.json) > cold/single_core.log 2>&1
[ -f cold/throttled.json ]   || (cd $REPO && $CP --runs 11 --order-seed 143 --cpu 0 --throttle 50 --json $R/cold/throttled.json) > cold/throttled.log 2>&1
[ -f cold/quota.json ]       || (cd $REPO && bash $P/in_quota.sh 50 $CP --runs 11 --order-seed 144 --json $R/cold/quota.json) > cold/quota.log 2>&1
[ -f cold/cold_cache.json ]  || (cd $REPO && $CP --runs 11 --order-seed 145 --cpu 0 --cold-cache --json $R/cold/cold_cache.json) > cold/cold_cache.log 2>&1
[ -f cold/cold_pyc.json ]    || (cd $REPO && $CP --runs 11 --order-seed 146 --cpu 0 --cold-pyc --json $R/cold/cold_pyc.json) > cold/cold_pyc.log 2>&1
log "cold first write done"
[ -f importtime/importtime.json ] || bash $P/importtime.sh $R/importtime > importtime.log 2>&1
log "importtime done"
if [ ! -f suite.log ] || ! grep -q " passed" suite.log; then
  (cd $REPO && PYTHONPATH=src timeout 2400 python -m pytest -q tests -p no:cacheprovider --deselect tests/test_stdio_transport.py) > suite.log 2>&1
fi
log "suite done"
if [ ! -f findings_junit.xml ]; then
  (cd $REPO && PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_gates_r14.py --junitxml $R/findings_junit.xml) > findings_pytest.log 2>&1
  (cd $REPO && PYTHONPATH=src python tests/fixtures/lumi_r13/edge_probes.py) > edge_probes.log 2>&1
  (cd $REPO && PYTHONPATH=src python tests/fixtures/lumi_r13/original_r10_probes.py) > original_r10_probes.log 2>&1
fi
log "findings done"
grep -q ALL_DONE /workspace/gates_proto/real/replay_v6.log 2>/dev/null || bash $P/run_replay_v6.sh
(cd /workspace/gates_proto/real && python $B/real_replay/real_replay_rescore.py --runs runs_v6 --out rescore_v6.json \
   && python check_v6.py > check_v6.json) >> $R/pipeline.log 2>&1
(cd $REPO && PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_gates_r10_private.py tests/test_gates_r11_private.py tests/test_gates_r14_private.py) > /workspace/gates_proto/real/private_tests_r14.log 2>&1
log "replay v6 done"
echo PIPELINE_DONE >> $R/pipeline.log
