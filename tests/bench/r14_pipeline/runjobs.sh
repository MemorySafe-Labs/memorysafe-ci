#!/bin/bash
# usage: runjobs.sh JOBFILE LOG PARALLEL
. /workspace/gates_proto/venv_r6/bin/activate
export HOME=/tmp/r6home PYTHONPATH=/workspace/gates_proto/repo/src
cat "$1" | xargs -P "${3:-7}" -I CMD bash -c 'CMD' >> "$2" 2>&1
echo ALL_DONE >> "$2"
