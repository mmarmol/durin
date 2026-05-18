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

## Design Decisions (next steps from V5)

See `docs/07_design_plan_and_stimuli.md` for details.

---

## Results V5b — 2026-05-17 (Docker-Internal Mode)

### 5 astropy instances, plan-always vs no-plan

Comparison of always-plan (INVESTIGATE→PLAN→EXECUTE→VERIFY) vs base agent:

| Condition | Patches | Avg Time | Avg Iters |
|---|---|---|---|
| Durin (full plan always) | 5/5 | 139s | 16.6 |
| Nanobot (no plan) | 5/5 | 94s | 10.4 |

**Key finding**: Patches were **identical in all 5 cases**. Investigation overhead added ~45s without improving quality. Led to fast-path design (execute→verify first, escalate on failure).

**Case study (astropy-6938)**: Preventive deliberation recommended wrong fix (`output_field = output_field.replace()` vs correct `output_field[:] = ...`). Agent followed bad advice → 10 extra iterations. Nanobot solved in 3.

---

## Results V6 — 2026-05-18 (Docker-Internal, 10 Mixed Instances)

### Changes tested
- Fast-path execute→verify (cycle 1)
- Post-error deliberation only (fires after verify failure, not preventively)
- Posture system active for Durin
- 10 instances from 6 different repos (astropy, django, flask, pytest, scikit-learn, sympy)

### Nanobot V6 Results (full data)

| Instance | Time | Iters | Tool Calls | Patch |
|---|---|---|---|---|
| astropy-12907 | 127s | 10 | 10 | YES |
| astropy-14182 | 246s | 20 | 20 | YES |
| astropy-14365 | 175s | 14 | 14 | YES |
| astropy-14995 | 155s | 17 | 17 | YES |
| astropy-6938 | 32s | 3 | 3 | YES |
| django-14999 | 172s | 17 | 17 | YES |
| flask-4045 | 111s | 15 | 15 | YES |
| pytest-5221 | 76s | 11 | 11 | YES |
| scikit-learn-13497 | 131s | 14 | 14 | YES |
| sympy-16503 | 902s | 0 | 0 | NO |

**Averages (9 patched)**: 136.1s, 13.4 iters, 13.4 tool calls

### Durin V6 Results (partial — timing lost due to process crash)

| Instance | Patch |
|---|---|
| astropy-12907 | YES |
| astropy-14182 | YES |
| astropy-14365 | YES |
| astropy-14995 | YES |
| astropy-6938 | YES |
| django-14999 | YES |
| flask-4045 | YES |
| pytest-5221 | YES |
| scikit-learn-13497 | YES |
| sympy-16503 | SKIPPED (OOM, Docker had 8GB, dual build exhausted memory) |

**9/9 patches generated** (sympy not attempted due to Docker OOM during image build).

**NOTE**: Durin stats.json was not written — process crashed before stats collection. Timing and tool call data lost. Only patches survived in the JSONL.

### Patch Comparison (Durin vs Nanobot)

| Instance | Identical? | Notes |
|---|---|---|
| astropy-12907 | YES | Same fix |
| astropy-14182 | NO | Different approaches |
| astropy-14365 | NO | Different approaches |
| astropy-14995 | NO | Different approaches |
| astropy-6938 | YES | Same fix |
| django-14999 | NO | Different approaches |
| flask-4045 | NO | Different approaches |
| pytest-5221 | NO | Different approaches |
| scikit-learn-13497 | YES | Same fix |

**3/9 identical patches, 6/9 different approaches**.

### SWE-bench Evaluation Results (V6)

| Instance | Durin | Nanobot | Patch identical |
|---|---|---|---|
| astropy-12907 | ✅ RESOLVED | ✅ RESOLVED | YES |
| astropy-14182 | ❌ | ❌ | NO |
| astropy-14365 | ❌ | ❌ | NO |
| astropy-14995 | ✅ RESOLVED | ✅ RESOLVED | YES |
| astropy-6938 | ❌ | ❌ | YES |
| django-14999 | ✅ RESOLVED | ✅ RESOLVED | NO |
| flask-4045 | ❌ | ❌ | NO |
| pytest-5221 | ❌ | ❌ | NO |
| scikit-learn-13497 | ❌ | ❌ | YES |
| sympy-16503 | — (OOM) | — (NO PATCH) | — |

**Durin: 3/9 = 33%** | **Nanobot: 3/9 = 33%**

### V6 Analysis

1. **Identical resolution rate**: Both agents resolve the same 3 instances despite 6/9 different patches. The different approaches did not improve or worsen resolution.
2. **6938 regression persists**: Both agents generate patches that fail SWE-bench evaluation. This is the case where the agent edits without verifying — the plan system's forced verification should address this, but it didn't trigger in fast-path.
3. **django-14999 resolved with different patches**: Both solve the same bug via different code approaches. Shows the problem has multiple valid solutions.
4. **scikit-learn-13497 identical but not resolved**: Same patch, same failure. Suggests the fix is wrong regardless of agent.
5. **Sympy gap**: Neither agent attempted sympy (Durin OOM, Nanobot NO PATCH after 902s). Potential differentiator with Docker memory increase.

### Nanobot Tool Usage Patterns (V6)

| Tool | Total Calls | Avg per Instance |
|---|---|---|
| read_file | 39 | 4.3 |
| exec | 34 | 3.8 |
| grep | 20 | 2.2 |
| edit_file | 10 | 1.1 |
| long_task | 9 | 1.0 |
| complete_goal | 9 | 1.0 |
| list_dir | 2 | 0.2 |

### Infrastructure Issues

1. **Docker OOM**: Building 2 durin.eval images simultaneously with 8GB RAM caused OOM (exit code 137) on sympy. Fix: increase Docker memory to 12GB.
2. **Stats crash**: Durin eval script crashed before writing stats.json. Detailed JSONL also empty. Only the SWE-bench format JSONL with patches survived.
3. **Disk usage**: 10 instances consumed ~56GB of Docker images (~44GB base + durin layers).

---

## Resolved Problems (from V5 analysis)

| Problem | Status | Resolution |
|---|---|---|
| No plan system | ✅ RESOLVED | Plan system implemented with fast-path + escalation (2026-05-17) |
| Carry-posture mean bug | ✅ RESOLVED | Fixed: carry sets current_value only, not mean. Commit 453a070 |
| Insufficient stimuli (dead axes) | ✅ PARTIALLY | depth now moves (0.42-0.73). discipline still static. New stimuli: VALIDATION_*, STUCK, PHASE_TRANSITION |
| Deliberation not helping without plan | ✅ RESOLVED | V3: post-error only deliberation, fires after verify failure |

---

## Saved Data

Folder: `benchmarks/swebench_5/`

| File | Content |
|---|---|
| `*_predictions.jsonl` | Patches in SWE-bench format |
| `*_stats.json` | Aggregated metrics per condition |
| `*_detailed.jsonl` | Per-instance: tools, iters, posture_final |
| `eval_reports/*.json` | SWE-bench evaluation results |

V6 files:
| File | Content |
|---|---|
| `2026-05-18_docker_v6_durin.jsonl` | 9 Durin patches (SWE-bench format) |
| `2026-05-18_docker_v6_nanobot.jsonl` | 10 Nanobot entries (9 patches + 1 no-patch) |
| `2026-05-18_docker_v6_nanobot_detailed.jsonl` | Full Nanobot stats (timing, tools, posture) |
| `2026-05-18_docker_v6_nanobot_stats.json` | Nanobot aggregated stats |
| `2026-05-18_docker_v6_durin_stats.json` | Empty (crash before write) |

---

## Last updated: 2026-05-18
