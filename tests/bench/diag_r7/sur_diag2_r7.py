import sys, collections
sys.argv = sys.argv[:1]
from sur_diag import run
T = collections.Counter()
for s in range(8):
    for arm in ("ext", "sur+ext"):
        o = run(s, arm)
        for d in o.values(): T[(arm, d["outcome"])] += 1
for k, v in sorted(T.items(), key=str): print(v, k)
