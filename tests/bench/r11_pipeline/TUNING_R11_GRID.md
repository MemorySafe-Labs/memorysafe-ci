# Round 11 tuning grid (declared before any tuning run)

Arm tuned: `r11` = `r10_stale` + `MEMORYSAFE_GATE_FAST=1` (exact-equivalent speed-ups, nothing to tune)
+ `MEMORYSAFE_GATE_GOAL2=1` (explicit goal / "that mattered" signals, `gates_goal.py`). Every r6-r10
parameter stays frozen. The r10 arm's `R6_W_GOAL=0` and `R6_GOAL_MIN_SHARED=99` stay set; with GOAL2 on
the round-7 word-overlap rule is not consulted at all.

| parameter (override `MEMORYSAFE_GATE_R6_<NAME>`) | values | default |
|---|---|---|
| GOAL_ENTITY (entity membership counts at all) | 0, 1 | 1 |
| GOAL_KEYWORD (entity membership also needs one non-name goal key word; only with GOAL_ENTITY=1) | 0, 1 | 1 |
| TAG_EVIDENCE (a caller goal tag alone is a sign of use: rarity bonus / rare quota) | 0, 1 | 1 |
| RECONF_MIN (restatements that count as "mattered"; 99 = off) | 2, 99 | 2 |

12 points (GOAL_ENTITY=0: 2 x 2; GOAL_ENTITY=1: 2 x 2 x 2). W_GOAL2 = 0.20 and W_MATTER = 0.20 are fixed,
not tuned: in development runs (seeds 0-7, declared below) the weight value made no difference, because
any sign of use already lifts a memory above the unprotected throwaways.

## Caller-tag model in the goal bench (declared now, fixed for tuning and held-out)

`gates_realistic_benchmark.add_goals`: with its own random stream (`seed * 104729 + 31`), so the workload
is otherwise identical, the simulated caller tags each goal-serving fact with its goal's id with
probability TAG_P_SERVING = 0.5, and each goal distractor (by mistake) with probability
TAG_P_DISTRACTOR = 0.1. All arms receive the same tags; arms without GOAL2 ignore them (no effect, no
row written). The primary and guard metrics are computed over ALL serving facts and ALL distractors,
tagged or not. New descriptive metrics: `goal_serving_survival_untagged`, `goal_serving_survival_tagged`,
`goal_distractor_retained_untagged` (the gate's own latching, without caller mistakes), `caller_tags`.

## Tuning data

Seeds 0-7 only, benches `real` (realistic, cap 200, no goals: only RECONF_MIN can act there) and `goal`.
The stale bench (cap 1000, nothing is evicted) and the hard bench are not used for tuning; both stay
held-out guards. Baselines on the same seeds: `shipped` and `r10_stale`.

## Constraints (means over seeds 0-7)

1. goal `goal_distractor_retained` <= shipped + 0.015 (half the held-out 3pp non-inferiority margin);
2. goal `goal_serving_survival` >= r10_stale;
3. realistic `stale_leak` <= r10 + 0.01, `latest_fact_correct` >= r10 - 0.01, `hit_critical` >= r10 - 0.01;
4. realistic `throwaways_retained` <= r10 + 0.01;
5. realistic `useful_facts_kept`, `hit_preference`, `hit_project` >= shipped - 0.01;
6. 0 wrong merges.

Objective among feasible points: maximise goal `goal_serving_survival`. Ties (<= 0.005): lower goal
`goal_distractor_retained`, then the simpler point (GOAL_ENTITY 0 before 1, TAG_EVIDENCE 0 before 1,
RECONF_MIN 99 before 2, GOAL_KEYWORD 1 before 0).

If no point is feasible: freeze the point with the lowest goal `goal_distractor_retained` among the
points meeting constraints 2-6 (ties: higher survival, then the simpler point); if none meets 2-6, freeze
the defaults. Either way it is reported as "no feasible point".

## Development knowledge (declared)

Before this grid was written I ran, on seeds 0-7 only (goal bench), these development configurations:
r11 defaults; GOAL_KEYWORD 0; W_GOAL2 0.4; GOAL_ENTITY 0; RECONF_MIN 99; TAG_EVIDENCE 0 with W_GOAL2 0.2
and 0.4; and the round-7 word-overlap rule on r10. Observed (means, seeds 0-7): r10 serving 61.5%,
distractors 31.2%; r11 defaults 85.4% / 41.7%; tags only (GOAL_ENTITY 0) 82.3% / 34.4%. So the entity
rule latched onto several pool distractors ("a shoe shop near the Lisbon marathon route", "a meetup
about AWS solutions", "the dentist's office near Harlow"), and the 10% caller mis-tags alone cost about
3pp of distractor retention. The goal pool's texts are the same in tuning and held-out seeds; the
entity rule was written after reading that pool, so its held-out goal-bench result is not independent
of the pool (reported as a limitation).

Diagnosis of the r10 no-goal survival (seeds 0-7, `gates_r11_goal_diag.py`): of 96 serving facts, the 33
with a durable anchor (date, amount, "must", "deadline"...) survive 100% under r10 (31% under shipped),
the 16 in auto-protected categories survive 100% under both, and the 50 with no anchor and no other
sign survive 26% (shipped 24%). So r10's ~61% comes from the anchor/sign-of-use rule, not from any
goal signal; a goal signal can only add on the 50 unanchored facts. An anchored fact that also gets a
goal tag gets no further keep benefit (already a sign of use), so nothing is counted twice in keep
decisions. The r11 descriptive split by tag shows where the gain comes from.
