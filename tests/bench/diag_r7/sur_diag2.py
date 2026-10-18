import sys, collections
sys.argv = sys.argv[:1]
from sur_diag import run
T = collections.Counter()
for s in range(8):
    for arm in ("ext", "sur+ext", "sur(noprot)+ext"):
        o = run(s, arm)
        for d in o.values(): T[(arm, d["outcome"])] += 1
        if arm == "sur+ext":
            for d in o.values():
                if d["outcome"] == "leak": T[("sur+ext leak top protected", d["top"]["prot"])] += 1
        if arm == "ext":
            for d in o.values():
                if d["outcome"] == "leak": T[("ext leak top protected", d["top"]["prot"])] += 1
for k, v in sorted(T.items(), key=str): print(v, k)
