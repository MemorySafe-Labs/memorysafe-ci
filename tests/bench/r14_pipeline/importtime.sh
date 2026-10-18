#!/bin/bash
# -X importtime breakdown (GATES_R14_PREREG.md s5): pristine 0.4.19, r13 flags on (before), r14 flags off,
# r14 flags on (after). Flags on = the modules the first gated write loads (preload()). Also records whether
# the lazily loaded prototype code (gates_proto, gates_r6_lazy) was loaded (it must not be).
OUT=${1:-/workspace/gates_proto/r14/importtime}
mkdir -p $OUT
PY=/workspace/gates_proto/venv_r6/bin/python
F="MEMORYSAFE_GATE_CAPACITY=1 MEMORYSAFE_GATE_USEFUL=1 MEMORYSAFE_GATE_EXTINCTION=1 MEMORYSAFE_GATE_STALE=1 MEMORYSAFE_GATE_FAST=1 MEMORYSAFE_GATE_R6_W_GOAL=0 MEMORYSAFE_GATE_R6_GOAL_MIN_SHARED=99"
CODE='import memorysafe_chatgpt.storage as s, os, sys
if any(k.startswith("MEMORYSAFE_GATE_") for k in os.environ):
    import memorysafe_chatgpt.gates, memorysafe_chatgpt.gates_r6, memorysafe_chatgpt.gates_stale
    from memorysafe_chatgpt import gate_flags; gate_flags.preload()
print("LAZY_LOADED", sorted(m for m in sys.modules if m.endswith(("gates_proto", "gates_r6_lazy"))), file=sys.stderr)'
for i in $(seq 1 7); do
  env -i HOME=/tmp/r6home PATH=$PATH PYTHONPATH=/tmp/d9tree/src $PY -X importtime -c "$CODE" 2> $OUT/base_$i.log
  env -i HOME=/tmp/r6home PATH=$PATH PYTHONPATH=/tmp/r13tree/src $F $PY -X importtime -c "$CODE" 2> $OUT/r13on_$i.log
  env -i HOME=/tmp/r6home PATH=$PATH PYTHONPATH=/workspace/gates_proto/repo/src $PY -X importtime -c "$CODE" 2> $OUT/r14off_$i.log
  env -i HOME=/tmp/r6home PATH=$PATH PYTHONPATH=/workspace/gates_proto/repo/src $F $PY -X importtime -c "$CODE" 2> $OUT/r14on_$i.log
done
$PY - "$OUT" <<'PY'
import sys, re, glob, json, statistics, collections
out = sys.argv[1]; res = {}
for arm in ("base", "r13on", "r14off", "r14on"):
    per = collections.defaultdict(list); lazy = set()
    for f in glob.glob(f"{out}/{arm}_*.log"):
        for ln in open(f):
            m = re.match(r"import time:\s+(\d+) \|\s+(\d+) \|(\s*)(\S+)", ln)
            if m:
                per[m.group(4)].append((int(m.group(1)), int(m.group(2))))
            if ln.startswith("LAZY_LOADED"):
                lazy.add(ln.split(" ", 1)[1].strip())
    keep = [k for k in per if k.startswith("memorysafe_chatgpt") or k in ("tiktoken", "sqlite3", "json", "re", "bisect", "heapq")]
    res[arm] = {k: {"self_us": statistics.median(x[0] for x in per[k]), "cum_us": statistics.median(x[1] for x in per[k]),
                    "n": len(per[k])} for k in sorted(keep)}
    res[arm]["_lazy_loaded"] = sorted(lazy)
json.dump(res, open(f"{out}/importtime.json", "w"), indent=1)
for arm, d in res.items():
    print(arm, {k: d[k]["cum_us"] for k in d if k.startswith("memorysafe")}, "lazy:", d["_lazy_loaded"])
PY
