# Benchmark Log — SWE-bench evaluation

> Objective evaluation of Durin as an agentic system using SWE-bench Lite.
> Direct comparison against nanobot baseline and between Durin conditions.

---

## Objective

Measure whether Durin's posture and deliberation systems add measurable value over the base agent.

---

## Setup

### API
- **Endpoint**: `https://api.z.ai/api/coding/paas/v4` (OpenAI-compatible)
- **Main model**: `glm-5.1` (754B MoE, Z.ai/Zhipu)
- **Deliberation model**: `glm-5-turbo` (for generators)
- **Context window**: 200K tokens
- **Temperature**: 0.1 (main), 0.7 (generators)

### Scripts
- `scripts/swebench_eval.py` — Durin (with/without delib, with/without carry-posture)
- `scripts/swebench_nanobot_eval.py` — Nanobot base (no posture, no delib)

### Evaluation
- Docker Desktop (ARM, swebench v4.1.0)
- `swebench.harness.run_evaluation` — applies patch, runs issue tests

---

## Experimental Conditions

| # | Condition | Posture | Deliberation | Carry |
|---|---|---|---|---|
| 1 | Nanobot base | OFF | OFF | — |
| 2 | Durin without deliberation | ON (fresh) | OFF | No |
| 3 | Durin delib V2 | ON (fresh) | ON (V2) | No |
| 4 | Carry without deliberation | ON (carry) | OFF | Yes |
| 5 | Carry + deliberation | ON (carry) | ON (V2) | Yes |

**Carry-posture**: `posture_final` from instance N becomes `posture_initial` of instance N+1.

---

## Results — 2026-05-17

### 5 instances: astropy (12907, 14182, 14365, 14995, 6938)

| Condition | Resolved | Rate | Avg Time | Avg Iters | Posture Events | Delib Events |
|---|---|---|---|---|---|---|
| 1. Nanobot base | 2/5 | 40% | 91.7s | 12.8 | 0 | 0 |
| 2. Durin without delib | 4/5 | 80% | 235.3s | 30.2 | 156 | 0 |
| 3. Durin delib V2 | 3/5 | 60% | 232.3s | 30.8 | 155 | 10 |
| 4. Carry without delib | 3/5 | 60% | 209.5s | 28.6 | 271 | 10 |
| 5. Carry + delib | 4/5 | 80% | 198.9s | 24.8 | 233 | 10 |

### Per-instance Matrix

| Instance | Nanobot | Without delib | Delib V2 | Carry | Carry+D |
|---|---|---|---|---|---|
| 12907 (easy) | ✓ | ✓ | ✓ | ✓ | ✓ |
| 14182 (hard) | ✗ | ✗ | ✗ | ✗ | **✓** |
| 14365 | ✗ | ✓ | ✓ | ✗ | ✓ |
| 14995 (easy) | ✓ | ✓ | ✓ | ✓ | ✓ |
| 6938 | ✗ | ✓ | ✗ | ✓ | ✗ |

---

## Analysis

### Main Findings

1. **Posture adds real value**: Nanobot 40% → Durin 80% (+100% relative). The extra iterations induced by caution produce more correct patches.

2. **Deliberation V2 is neutral-to-negative in fresh**: Condition 3 (60%) is worse than condition 2 (80%). Fresh-posture deliberation loses a case that without-delib solves.

3. **Carry + delib is the best combination**: Only one that solves 14182 (the hardest). Also the fastest of the Durin conditions (199s vs 235s) with fewer iterations (24.8 vs 30.2).

4. **6938 is inconsistent**: Solved by conditions 2 and 4 but not 3 and 5. Suggests that deliberation diverts the agent in this specific case.

5. **N=5 is insufficient** for statistical conclusions. The difference between 3/5 and 4/5 is a single case.

### Postural Trajectory — Carry

**Problem: geometric drift**

In carry-posture, the final value is used as new mean (bug in `swebench_eval.py:191`). This causes:
- `exploration`: 0.5 → 0.567 → 0.700 → 0.833 → 0.967 → 1.000 (saturation)
- `caution`: rises consistently without limit
- `depth`, `discipline`: always 0.500 (never activated)

**Without carry (fresh)**, all runs converge to similar values:
- `exploration` ≈ 0.567
- `caution` ≈ 0.6-0.8
- Everything else fixed

### Dead Axes

| Axis | Why it does not move | Current stimulus | Problem |
|---|---|---|---|
| depth | `GOAL_AMBIGUOUS` requires iter without tools or content (never occurs) | +0.10 if ambiguous | No signal for "you need to think more" |
| discipline | `EXPLICIT_PROTOCOL` requires markers in system prompt (never present) | +0.10 if protocol | No signal for "follow a process" |

### Deliberation Overhead

| Version | Overhead per instance | Extra calls |
|---|---|---|
| V1 (evaluators, multi-round) | ~400s (+170%) | ~15 LLM calls |
| V2 (without evaluators, 1 round) | ~12s (+5%) | 3 LLM calls |

V2 eliminated the overhead but also eliminated the value: without real evaluation, perspectives are injected unfiltered.

---

## Identified Problems

1. **No plan system**: The agent executes reactively without planning. Nanobot uses 13 iters, Durin uses 30 — but not because it plans more, rather because posture makes it validate more.

2. **Carry-posture has a mean bug**: The fix is trivial — when carrying, set only `current_value` without changing `mean`.

3. **Insufficient stimuli**: 2 of 5 axes never activate. New signals linked to real progress are needed (phase changes, stuck detection, multi-file patterns).

4. **Deliberation does not help without a plan**: Perspectives are injected at the start and dilute over 30 reactive iterations. For them to help, they would need to accompany a plan that updates.

---

## Design Decisions (next steps)

See `docs/07_design_plan_and_stimuli.md` for details.

---

## Saved Data

Folder: `benchmarks/swebench_5/`

| File | Content |
|---|---|
| `*_predictions.jsonl` | Patches in SWE-bench format |
| `*_stats.json` | Aggregated metrics per condition |
| `*_detailed.jsonl` | Per-instance: tools, iters, posture_final |
| `eval_reports/*.json` | SWE-bench evaluation results |

---

## Date: 2026-05-17
