# Implementation Log — Guiding Thread + Deliberation

> Current state of the posture vector implementation and the multi-generator deliberation system.

---

## General State

**Posture**: complete (7 original phases + expansions + gap closure).  
**Deliberation**: evolutionary (Mind Evolution: mutation + crossover + convergence by plateau).  
**Tests**: 3479 passing, 0 failures.  
**Last update date**: 2026-05-17.

---

## 1. Posture Vector (guiding thread)

### Implemented

| Component | File | Status |
|---|---|---|
| Vector (5 axes, AxisState) | `durin/posture/vector.py` | ✅ |
| Homeostasis (return to mean + clamp) | `durin/posture/homeostasis.py` | ✅ |
| Stimulus table (12 rules) | `durin/posture/stimulus.py` | ✅ |
| Posture phrase (deterministic table) | `durin/posture/phrase.py` | ✅ |
| Persistence (session metadata) | `durin/posture/persistence.py` | ✅ |
| PostureHook (lifecycle) | `durin/posture/hook.py` | ✅ |
| Config schema (PostureConfig) | `durin/config/schema.py` | ✅ |
| Advanced event detection | `durin/posture/hook.py` | ✅ |
| Goal-sensitive initialization | `durin/posture/goal_bias.py` | ✅ |

### Active Stimuli (detected automatically)

| Event | Condition | Effect |
|---|---|---|
| `STEP_FAILED` | Error or tool failure | caution +0.10, depth +0.05 |
| `STEP_SUCCEEDED` | Successful tool calls | caution −0.03 |
| `CONSECUTIVE_SUCCESSES_3` | 3+ consecutive successes | exploration +0.05 |
| `CONSECUTIVE_FAILURES_3` | 3+ consecutive failures | caution +0.15, conformity −0.10 |
| `USER_CORRECTED` | User injects message mid-turn | conformity +0.05 |
| `GOAL_AMBIGUOUS` | Empty iteration (no tools, no output) | depth +0.10 |
| `CRITICAL_ACTION` | Dangerous tool executed | caution +0.10 |
| `EXPLICIT_PROTOCOL` | System prompt with protocol markers | discipline +0.10 |

### Goal-sensitive initialization (§3.4)

At startup (iteration 0), the goal is scanned with deterministic keywords:

| Detected keywords | Axis | Delta |
|---|---|---|
| production, deploy, delete, force push, migration... | Caution | +0.10 |
| explore, research, brainstorm, alternatives... | Exploration | +0.10 |
| protocol, checklist, compliance, step by step... | Discipline | +0.10 |

Eliminates the cold-start problem: the vector reacts to the nature of the goal before the first step.

### Persistence and Decay

| Component | File | Status |
|---|---|---|
| Vector serialization | `durin/posture/persistence.py` | ✅ |
| Temporal decay (tau=4h) | `durin/posture/persistence.py:apply_time_decay` | ✅ |
| Restore with decay at turn start | `durin/agent/loop.py:_restore_posture_from_session` | ✅ |
| Save at turn end | `durin/agent/loop.py:_save_posture_state` | ✅ |

Formula: `value += (1 - exp(-elapsed/tau)) * (mean - value)` with tau=4 hours.  
After 4h inactive the vector will have decayed ~63% toward the mean. After 12h, ~95%.

### Pending / Future

- `USER_APPROVED_RISKY`: defined but no detector (requires semantic detection of user content)
- `EXPLORATORY_TASK`: defined but no detector (requires goal classification — partially covered by goal_bias)
- ~~`EXPLICIT_PROTOCOL`: defined but no detector~~ → ✅ Implemented (markers in system prompt)
- Reinforced decay by different goal (§6 of the design — currently only temporal)
- Adjustment of means by historical consolidation (Phase 3 of the design, deferred)

---

## 2. Evolutionary Deliberation (Mind Evolution)

### Implemented

| Component | File | Status |
|---|---|---|
| Types (Proposal, Verdict, RoundResult, ConvergenceReason) | `durin/deliberation/types.py` | ✅ |
| Scoring (weights by caution, threshold by depth) | `durin/deliberation/scoring.py` | ✅ |
| Generator (seeds round 1, evolution round 2+) | `durin/deliberation/generator.py` | ✅ |
| Evaluator (LLM score 0-10) | `durin/deliberation/evaluator.py` | ✅ |
| Director (pure decision, multi-round) | `durin/deliberation/director.py` | ✅ |
| Engine (evolutionary orchestrator + crossover) | `durin/deliberation/engine.py` | ✅ |
| **Structural modulator** | `durin/deliberation/modulator.py` | ✅ |
| DeliberationHook (pre-message injection) | `durin/deliberation/hook.py` | ✅ |
| Config (DeliberationConfig) | `durin/config/schema.py` | ✅ |
| Shared constants | `durin/deliberation/constants.py` | ✅ |

### Evolutionary Architecture (Mind Evolution)

| Round | Behavior | Inspiration |
|---|---|---|
| Round 1 | Divergent: short seeds from each perspective (pragmatic, explorer, critic) | Divergent thinking |
| Round 2+ | Evolutionary: generators receive previous winner + their own proposal + scores → refine | Mutation |
| Crossover | If gap between top 2 < 0.10, generates HYBRID proposal combining both | Genetic crossover |
| Convergence | By threshold (sufficient score), plateau (improvement < 0.05 between rounds), or max_rounds | Fitness plateau |

### Injection to the Main LLM

Deliberation is injected as a **system message before the last user message** (not in system prompt):
```
[Pre-analysis deliberation]

Recommended approach: [evolved winning proposal]
Identified risks: [critic's perspective]
Alternative considered: [best runner-up]
Confidence: high/medium/low
```

This **enriches** the main LLM so it builds a better plan, without dictating what to do.

### Structural Modulation (doc §4.2)

Posture does not only weight scoring — it **changes the deliberation architecture** on each invocation:

| Axis | Condition | Structural effect |
|---|---|---|
| Depth | < 0.3 | Critic is omitted (fast deliberation) |
| Depth | >= 0.6 | +1 extra generation round |
| Depth | >= 0.8 | +2 extra rounds (max 5 total) |
| Exploration | current value | Explorer temperature: `base + 0.3*(expl - 0.5)`, clamped [0.5, 1.2] |
| Conformity | < 0.3 | Explorer receives permission to question the task |
| Caution | > 0.7 | Pragmatic duplicated (variant with +0.15 temp) → 4 proposals |
| Caution | > 0.85 | Critic also duplicated → 5 proposals |
| Caution | current value | Dynamic drift threshold: `0.15 - 0.05*(caution-0.5)` range [0.10, 0.20] |
| Discipline | >= 0.6 | All generators receive protocol adherence suffix |
| Discipline | < 0.3 | Pragmatic +0.1 temperature (more flexible) |

Result: an agent with caution 0.9 generates 5 proposals and re-deliberates with drift >= 0.10 vs an agent with caution 0.3 that generates 3, tolerates drift up to 0.18. An agent with depth 0.9 deliberates up to 5 rounds; with low depth it omits the critic and uses maximum 3. High discipline forces adherence to procedure; low allows improvisation.

### Synthesis (enriched)

| Component | File | Status |
|---|---|---|
| SynthesisResult (structured) | `durin/deliberation/types.py` | ✅ |
| `synthesize()` → SynthesisResult | `durin/deliberation/synthesis.py` | ✅ |
| `render_synthesis()` → text | `durin/deliberation/synthesis.py` | ✅ |
| Posture-driven reasoning | automatic | ✅ |
| Alternatives (top 2 runners-up) | automatic | ✅ |
| Confidence (high/medium/low) | automatic | ✅ |

### Intelligent Triggering

| Trigger | When | Behavior |
|---|---|---|
| Planning moment | Iteration 0, no active goal | Deliberates and injects into system prompt |
| Critical action | `before_execute_tools` with dangerous tool | Re-deliberates and updates direction |
| Posture drift | Drift >=0.15 in any axis since last deliberation | Re-deliberates |
| Goal active skip | "Goal (active):" in system prompt | Skips deliberation (does not interfere with plan) |

### Verdict History

| Component | File | Status |
|---|---|---|
| VerdictHistory (ring buffer, max 20) | `durin/deliberation/history.py` | ✅ |
| VerdictEntry (frozen dataclass) | `durin/deliberation/types.py` | ✅ |
| `dominant_role()` (pattern in last 5) | `durin/deliberation/history.py` | ✅ |
| Serialize/deserialize | `durin/deliberation/history.py` | ✅ |
| Hook accumulates automatically | `durin/deliberation/hook.py` | ✅ |
| Persistence to session metadata | `durin/deliberation/persistence.py` | ✅ |
| Restore at turn start | `durin/agent/loop.py:_restore_verdict_history` | ✅ |
| Save at turn end | `durin/agent/loop.py:_save_verdict_history` | ✅ |

### Context Projection

Generators receive enriched context:

| Field | Source | Limit |
|---|---|---|
| `goal_summary` | Last user message | 500 chars |
| `active_objective` | "Goal (active):" in system prompt | 300 chars |
| `conversation_summary` | Last 5 assistant messages | 100 chars each |
| `previous_verdict_brief` | VerdictHistory.last | 80 chars |
| `recent_context` | Tool names to execute | — |

---

## 3. Infrastructure

### UI Visualization

| Channel | Component | Status |
|---|---|---|
| CLI (Rich) | `durin/cli/agent_ui_render.py` | ✅ |
| WebUI (React) | `webui/src/components/thread/PosturePanel.tsx` | ✅ |
| WebUI (React) | `webui/src/components/thread/DeliberationPanel.tsx` | ✅ |
| Hook → UI pipe | `emit_ui` callback in AgentHookContext | ✅ |

### Telemetry

| Event | Logger method | Data |
|---|---|---|
| `posture.initial` | `log_posture_initial` | snapshot 5 axes |
| `posture.change` | `log_posture_change` | axes, deltas, events |
| `deliberation.start` | `log_deliberation_start` | trigger, goal, posture |
| `deliberation.result` | `log_deliberation_result` | winner, scores, rounds, duration |
| `deliberation.skipped` | `log_deliberation_skipped` | reason |
| `deliberation.error` | `log_deliberation_error` | error msg |

File: JSONL append-only in `~/.cache/durin/telemetry/`.

### Automatic Wiring

| Component | File | Status |
|---|---|---|
| Hook factory (config → hooks) | `durin/agent/hook_factory.py` | ✅ |
| Providers: ollama, local (llama-cpp) | `durin/agent/hook_factory.py` | ✅ |
| AgentLoop.from_config auto-wiring | `durin/agent/loop.py` | ✅ |

---

## 4. Mapping to the Design Document

| Moment (doc §4) | Status | Notes |
|---|---|---|
| 1. Context projection | ⚡ Partial | Flat text, no graph. Context projection enriches with summary + objective + previous verdict |
| 2. Generation (SLMs) | ✅ Complete | 3 parallel generators with posture phrase + enriched context |
| 3. Evaluation (scores) | ✅ Complete | 2 evaluators (progress, reversibility), weights by caution |
| 4. Director (threshold) | ✅ Complete | Threshold by depth, multi-round (max_rounds + extra by depth, cap 5), under_doubt |
| 5. Synthesis (multi-perspective) | ✅ Complete | Recommended approach + Risks + Alternative + Confidence, injected as pre-user message |
| 6. Post-step adjustment | ✅ Complete | PostureHook.after_iteration detects events and updates vector |

| Cross-cutting aspect (doc §5-6) | Status | Notes |
|---|---|---|
| Persistence between sessions | ✅ Complete | Save/restore with temporal decay (tau=4h) |
| Hierarchical injection | ⚡ Partial | Plan-level skip (goal active), no subplan/step differentiation |
| Observability | ✅ Complete | JSONL telemetry + UI visualization + verdict history |

---

## 5. What is Missing (prioritized)

### Medium Priority

1. **Moment 1 complete (graph)** — current context projection is flat text. The design calls for graph node selection based on posture. Requires the graph memory system (doc 03).

2. **Remaining semantic detectors** — `USER_APPROVED_RISKY` (requires classification of user content), `EXPLORATORY_TASK` (partially covered by goal_bias, missing dynamic mid-turn detector).

3. **Explicit Moment 5** — currently the main LLM receives the direction and acts. The design suggests an explicit synthesis step where the LLM combines proposal + context + critiques into a detailed action plan.

4. **Posture biases tool selection** — posture affects deliberation but not the agent's bias toward safe vs risky tools. Would be a new "moment 2.5".

### Low Priority

5. **Hierarchical injection** (doc §5) — plan/subplan/step. Currently always injected or skipped by active goal. Optimization for long plans.

6. **Reinforced decay by different goal** (doc §6) — if the new goal is unrelated to the previous one, decay should be stronger. Today it is purely temporal.

7. **Empirical calibration of deltas** — current values in the stimulus table are educated guesses. Real data is needed to adjust.

8. **Adjustment of means by consolidation** (doc §6, Phase 3) — means are constants today. The memory "dream" could adjust them based on outcome history.

---

## 6. Tests by Module

| Directory | Tests | Focus |
|---|---|---|
| `tests/posture/` | ~95 | Vector, homeostasis, stimulus, phrase, hook, emit_ui, advanced triggers, persistence, goal_bias |
| `tests/deliberation/` | ~200 | Types, scoring, generator, evaluator, director, engine, hook, synthesis, history, context projection, triggering, persistence, emit_ui, integration, modulator |
| `tests/agent/test_hook_factory.py` | 5 | Factory wiring from config |
| `tests/telemetry/` | 14 | Logger, events, path sanitization |
| `tests/cli/test_agent_ui_render.py` | 10 | Rich panel rendering |

New total on the topic: **~300 tests dedicated to the guiding thread + deliberation**.

---

## 7. Design Decisions Made

1. **Pure functions for scoring/director** — zero I/O, deterministic, auditable.
2. **Frozen dataclasses** — immutability throughout the deliberation pipeline.
3. **Cheap local SLMs** — no cost pressure, deliberation is liberal.
4. **Posture reads, never writes from deliberation** — the posture hook updates the vector independently.
5. **Graceful degradation** — if Ollama is unavailable, a warning is logged and the agent works without deliberation.
6. **Drift as organic trigger** — no timer or manual heuristic needed to re-deliberate mid-plan.
7. **Goal active = skip deliberation** — when there is an active plan, do not interfere (the plan WAS already deliberated).
8. **Ring buffer (20 max)** — bounded verdict memory, does not grow indefinitely.
9. **Structural modulation, not just textual** — posture changes the architecture (which generators, how many rounds, what thresholds), not just the injected phrases.
10. **Goal bias with simple keywords** — no LLM for cold-start. Keyword heuristics are sufficient and predictable.
11. **Dynamic drift threshold** — caution modulates sensitivity to change, it is not a fixed number.
12. **Deliberation as enrichment, not directive** — the output gives perspectives (approach + risks + alternative) so the LLM thinks better, it does not tell it what to do.
13. **Trimodal convergence** — THRESHOLD (sufficient score), PLATEAU (improvement < 0.05), MAX_ROUNDS (cap). Each case reports its reason in the Verdict.
