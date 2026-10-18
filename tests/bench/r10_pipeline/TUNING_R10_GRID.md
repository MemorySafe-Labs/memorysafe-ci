# Round 10 tuning grid (declared before any tuning run)

Arm tuned: `r10_stale` = r9 candidate (`r7x_nogoal_useful_ext`) + `MEMORYSAFE_GATE_STALE=1`.
Only the three round-10 parameters are tuned; every r6-r9 parameter stays frozen.

| parameter (override `MEMORYSAFE_GATE_R6_<NAME>`) | values | default |
|---|---|---|
| STALE_DAYS (status claim not reconfirmed for N days -> flag) | 21, 30, 45 | 30 |
| STALE_NEWER_DAYS (newer activity on the same name at least N days later -> flag) | 7, 14 | 14 |
| STALE_PENALTY (rank penalty for a flagged claim) | 0.05, 0.15, 0.30 | 0.15 |

18 points. ENTITY_DF_MAX stays 0.05 (not tuned).

Tuning data: seeds 0-7 only, benches `real` (realistic, cap 200), `goal`, `stale` (new). The hard bench
is not used for tuning (its runs have no simulated clock, so the stale parameters cannot act there;
it is still a held-out guard). Baselines on the same seeds: `shipped` and `r7x_nogoal_useful_ext`.

Constraints (means over seeds 0-7):
1. stale bench `durable_demoted_n` = 0 on every seed;
2. stale bench `false_supersession` <= shipped;
3. stale bench `keep_both_respected` >= shipped;
4. stale bench `timestale_found_top3` >= 0.95 (a flagged note must stay findable);
5. realistic `useful_facts_kept`, `hit_preference`, `hit_project` >= shipped - 0.01;
6. goal `goal_serving_survival` >= r9 candidate - 0.01.

Objective among feasible points: J = realistic (latest_fact_correct - stale_leak)
+ stale (status_latest_correct - status_stale_top1). Ties (|dJ| <= 0.005): the default point, then the
larger STALE_DAYS, then the smaller STALE_PENALTY. If no point is feasible, the default is frozen and
that is reported.
