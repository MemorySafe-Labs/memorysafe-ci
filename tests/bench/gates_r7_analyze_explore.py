"""Round 7 EXPLORATORY analysis (not pre-registered): the pre-registered analysis code unchanged,
with the post-hoc arm r7x_nogoal_useful_ext appended to its arm list. Same tests and rule; any
"PASS" printed for the extra arm is descriptive only.

  python tests/bench/gates_r7_analyze_explore.py <same arguments as gates_r7_analyze.py>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gates_r7_analyze as A  # noqa: E402

A.ARMS.append("r7x_nogoal_useful_ext")
if __name__ == "__main__":
    A.main()
