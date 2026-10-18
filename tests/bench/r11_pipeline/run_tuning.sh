#!/bin/bash
# Round 11 tuning (seeds 0-7, grid in TUNING_R11_GRID.md). Resumable: existing per-run files are skipped.
set -u
mkdir -p /workspace/gates_proto/r11/tuning
cd /workspace/gates_proto/r11
grep -q ALL_DONE tuning.log 2>/dev/null || bash /workspace/gates_proto/repo/tests/bench/r11_pipeline/runjobs.sh /workspace/gates_proto/repo/tests/bench/r11_pipeline/jobs_tuning.txt tuning.log 7
. /workspace/gates_proto/venv_r6/bin/activate
python /workspace/gates_proto/repo/tests/bench/r11_pipeline/select_tuning.py /workspace/gates_proto/r11/tuning > /workspace/gates_proto/r11/tuning_result.json 2> /workspace/gates_proto/r11/tuning_select.err
echo TUNING_DONE >> tuning.log
