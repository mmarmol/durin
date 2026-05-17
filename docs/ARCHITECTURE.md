# Durin — Operational Architecture

> Quick-reference document for understanding Durin's internals.
> **Keep updated** when modifying core modules.

---

## 1. Origin and Relationship with Nanobot

Durin is a fork of [nanobot](vendor/nanobot/) (lightweight agent framework). It inherits:
- Agent loop (`runner.py`), message bus, channels, tools, session management
- Provider structure (Anthropic, OpenAI-compat, Azure, Bedrock, etc.)
- Skills, commands, memory (Dream consolidation)
- `long_task` / `complete_goal` for objective tracking

**Durin adds** on top of nanobot:
- Posture system (5-axis behavioral vector)
- Deliberation V3 (single-call multi-perspective + merge)
- Plan system (3 execution tiers + fixed cycle + event log)
- Postural telemetry
- Hook factory that auto-wires posture + plan (with integrated deliberation)

---

## 2. Iteration Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    AgentRunner.run()                          │
│  for iteration in range(max_iterations):                     │
│                                                              │
│  1. Context governance (microcompact, snip, budget)          │
│  2. Build AgentHookContext(iteration, messages)              │
│  3. hook.before_iteration(context)                           │
│     ├── PostureHook: iter 0 → goal_bias + protocol_bias     │
│     ├── PlanHook: INVESTIGATE→PLAN triggers deliberation     │
│     └── PlanHook: inject tier instructions / phase prompt    │
│  4. LLM request → response                                  │
│  5. Parse response (tool_calls, content, reasoning)          │
│  6. If tool_calls:                                           │
│     a. hook.before_execute_tools(context)                    │
│     b. Execute tools (sequential or concurrent)              │
│     c. Append tool results to messages                       │
│  7. hook.after_iteration(context)                            │
│     ├── PostureHook: detect events → update vector           │
│     └── PlanHook: infer phase transitions, emit stimuli      │
│  8. If no tool_calls → final_content → break                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Posture System

### Key Files
| File | Responsibility |
|---|---|
| `posture/vector.py` | Data model: `PostureVector`, `AxisState`, `AxisName` |
| `posture/hook.py` | `PostureHook` — lifecycle hook that detects events and updates vector |
| `posture/stimulus.py` | `StimulusTable` — event → per-axis delta mapping |
| `posture/homeostasis.py` | `update_vector` — return-to-mean + stimulus + clamp |
| `posture/goal_bias.py` | Cold-start: keywords in goal → initial deltas |
| `posture/phrase.py` | Translates vector to textual phrase for prompt injection |
| `posture/persistence.py` | Save/load vector between sessions |

### The 5 Axes
| Axis | Default Mean | Variance | Return Force | Function |
|---|---|---|---|---|
| caution | 0.6 | 0.15 | 0.3 | Risk weighting |
| exploration | 0.4 | 0.20 | 0.4 | Explore vs exploit |
| depth | 0.5 | 0.20 | 0.5 | Think vs act quickly |
| discipline | 0.5 | 0.15 | 0.2 | Follow protocol vs improvise |
| conformity | 0.7 | 0.15 | 0.3 | Accept vs question task |

### Update Formula (each iteration)
```
1. Return to mean:  value += return_force × (mean − value)
2. Apply stimulus:  value += delta × (variance / 0.15)
3. Clamp:           value ∈ [mean − 2×variance, mean + 2×variance]
```

### Active Stimuli
| Event | Affected Axis(es) | Delta | Code Trigger |
|---|---|---|---|
| `STEP_FAILED` | caution +0.10, depth +0.05 | Error or tool failure |
| `CONSECUTIVE_SUCCESSES_3` | exploration +0.02, depth −0.03 | 3 consecutive successes |
| `CONSECUTIVE_FAILURES_3` | caution +0.15, conformity −0.10 | 3 consecutive failures |
| `GOAL_AMBIGUOUS` | depth +0.10 | Iter with no tools, no content, no error (rare) |
| `USER_CORRECTED` | conformity +0.05 | Messages injected into session |
| `USER_APPROVED_RISKY` | caution −0.05 | (Not active as trigger) |
| `CRITICAL_ACTION` | caution +0.10 | Tool in CRITICAL_TOOLS set |
| `EXPLORATORY_TASK` | exploration +0.10 | (Only via goal_bias keywords) |
| `EXPLICIT_PROTOCOL` | discipline +0.10 | Markers like "## checklist" in system prompt |
| `MULTI_FILE_EDIT` | discipline +0.08 | Editing >1 file in one iteration |
| `VALIDATION_SUCCESS` | caution −0.05, exploration −0.03 | Tests pass (real oracle) |
| `VALIDATION_FAILURE` | caution +0.10, depth +0.08 | Tests fail |
| `STUCK_NO_PROGRESS` | exploration +0.10, depth +0.10 | No progress detected |
| `PHASE_TRANSITION` | depth −0.10 | Phase change in plan cycle |
| `CONFIRM_PASS` | caution −0.10, exploration −0.05 | Plan: tests pass in CONFIRM |
| `CONFIRM_FAIL` | caution +0.15, depth +0.10 | Plan: tests fail in CONFIRM |
| `CYCLE_RESTART` | discipline +0.05, exploration +0.10 | Plan: cycle restart |
| `PLAN_COMPLEX` | depth +0.10, caution +0.05 | Plan: >3 items (one-shot) |

**Note**: `STEP_SUCCEEDED` was **removed** — absence of error ≠ real progress. Caution only decreases with oracle (VALIDATION_SUCCESS, CONFIRM_PASS) or explicit approval (USER_APPROVED_RISKY).

### Asymmetric Caution Design
- Raising caution: +0.10 to +0.15 per negative event (strong signal)
- Lowering caution: −0.05 to −0.10 only with real validation (weak signal, requires oracle)
- Philosophy: an over-cautious agent is slow but safe; an under-cautious agent generates false positives

### Known Issues (May 2026 benchmark)
- **carry-posture** has a bug: sets `current_value` as new `mean`, causing geometric drift
- **depth** and **discipline** now activate via MULTI_FILE_EDIT, VALIDATION_FAILURE, PHASE_TRANSITION (fixed)
- **exploration** reduced to +0.02 for CONSECUTIVE_SUCCESSES_3 (was +0.05, over-stimulated)

---

## 4. Deliberation System (V3)

Single-call multi-perspective deliberation. Not a standalone hook — it's a service injected into PlanHook.

### Key Files
| File | Responsibility |
|---|---|
| `deliberation/engine.py` | `DeliberationEngine` — 1 LLM call with structured prompt |
| `deliberation/service.py` | `DeliberationService` — orchestrates engine + telemetry |
| `deliberation/synthesis.py` | `render_for_injection()` — formats output for agent context |
| `deliberation/types.py` | `Perspective`, `DeliberationResult`, `DeliberationContext`, `HistoryEntry` |
| `deliberation/modulator.py` | Posture modulates prompt intensity per section |
| `deliberation/history.py` | Ring buffer of past deliberations |

### V3 Flow
```
1. PlanHook detects transition INVESTIGATE → PLAN (first update_plan call)
2. → before_iteration: PlanHook._run_deliberation(context)
3.   → Builds DeliberationContext with investigation findings
4.   → DeliberationService.deliberate(context)
5.     → 1 LLM call with forced ordering: [CRITIC] → [EXPLORER] → [PRAGMATIC] → [SYNTHESIS]
6.     → _parse_response(): regex split by markers → Perspective tuples + synthesis
7.   → Logs full result to telemetry (3 perspectives + synthesis + posture + timing)
8. → render_for_injection() → injected as system message
```

### Ordering for Divergence
- **Critic first**: identifies risks without a prior solution to defend
- **Explorer second**: proposes alternative without an "obvious" path established
- **Pragmatic third**: direct path, incorporating risks from the critic
- **Synthesis**: active merge of all 3 perspectives

### Injection Format
```
[Pre-analysis deliberation]

Risks identified: {critic}
Alternative considered: {explorer}
Direct approach: {pragmatic}

Synthesis: {merge of the 3 perspectives}
```

### Posture Modulation
- `caution > 0.7` → exhaustive critic
- `caution < 0.4` → brief critic
- `exploration > 0.6` → radical explorer
- `depth > 0.7` → detailed perspectives (3-5 sentences)
- default → concise perspectives (1-3 sentences)

### When It Deliberates
- Transition INVESTIGATE → PLAN in full_plan mode
- Re-activates on cycle restart (CONFIRM fail → new cycle → new investigation → PLAN)
- On retry: `previous_failure` enriches the prompt with what failed before

### Telemetry
Each deliberation generates a complete JSONL event:
```json
{"type": "deliberation.result", "data": {"trigger": "investigate_to_plan", "cycle": 1,
  "model": "glm-5.1", "duration_ms": 6234, "posture": {"caution": 0.65},
  "perspectives": {"critic": "...", "explorer": "...", "pragmatic": "..."},
  "synthesis": "..."}}
```

---

## 5. Hook Factory

`agent/hook_factory.py` wires everything when building the agent:

```python
build_hooks_from_config(config) → [PostureHook, PlanHook]
# DeliberationService is injected INTO PlanHook (not a separate hook)
```

Order:
1. **PostureHook** first (vector initialized before PlanHook queries it)
2. **PlanHook** second (has deliberation service + posture_snapshot_fn internally)

The `CompositeHook` executes all hooks in sequence for each lifecycle event.

### Inter-hook Communication
`AgentHookContext.external_stimulus_events: list[str]` allows PlanHook to emit postural events (CONFIRM_PASS, CONFIRM_FAIL, CYCLE_RESTART, PLAN_COMPLEX) that PostureHook consumes in its next iteration.

---

## 6. Plan System (3 Tiers)

### Key Files
| File | Responsibility |
|---|---|
| `plan/types.py` | `ExecutionTier`, `Phase`, `PlanItem`, `PlanState` |
| `plan/hook.py` | `PlanHook` — injects instructions, infers transitions, emits stimuli |
| `plan/store.py` | `PlanStore` — persistence (plan.json + events.jsonl per session) |
| `agent/tools/plan.py` | Tools: `set_execution_mode`, `update_plan` (auto-discoverable) |

### The 3 Execution Tiers
| Tier | When | What the hook does |
|---|---|---|
| `direct` | Simple questions, trivial edits | Nothing — no overhead |
| `execute_verify` | Localized bug fix, single change | Reminder: "State expected outcome, then verify" after editing |
| `full_plan` | Multi-step, uncertainty, refactors | Fixed cycle + event log + postural stimuli |

### Fixed Cycle (full_plan only)
```
INVESTIGATE → PLAN → EXECUTE → CONFIRM ─┐
     ↑                                   │ (fail)
     └───────────────────────────────────┘
```

- **INVESTIGATE**: Read, understand context. NO edits.
- **PLAN**: Define steps via `update_plan(add, ...)`. Last step must be a verification step.
- **EXECUTE**: Implement. Edit files.
- **CONFIRM**: Execute the verification defined in the plan. Pass → done. Fail → new cycle.

### Phase Transitions (inferred automatically)
| Transition | Trigger |
|---|---|
| INVESTIGATE → PLAN | `update_plan("add", ...)` is called |
| PLAN → EXECUTE | Edit tool (`edit_file` or `write_file`) detected |
| EXECUTE → CONFIRM | `exec` detected after edits |
| CONFIRM → INVESTIGATE | Error in context (tests failed) → restart cycle |

### Emitted Stimuli (posture ↔ plan bridge)
| Event | When | Postural Effect |
|---|---|---|
| `confirm_pass` | Tests pass in CONFIRM | caution −0.10 (real oracle) |
| `confirm_fail` | Tests fail in CONFIRM | caution +0.15, depth +0.10 |
| `cycle_restart` | Confirm fail → new cycle | discipline +0.05, exploration +0.10 |
| `plan_complex` | Plan exceeds >3 items | depth +0.10, caution +0.05 (one-shot) |

### Persistence (event log)
Each session with `full_plan` generates:
- `plans/{session_key}/plan.json` — current state (tier, phase, items, cycle_count)
- `plans/{session_key}/events.jsonl` — event log (tier_set, plan_item_added, phase_transition, confirm_result)

### Design Philosophy
The agent **declares** its tier via tool call (`set_execution_mode`). The hook **enforces** the cycle if `full_plan` is chosen. This prevents the agent from "thinking" it solved the problem without verifying — the central bug detected in benchmarks (6938: agent declares victory without running tests).

---

## 7. Nanobot Inheritance — What We Don't Touch

| Subsystem | Location | Notes |
|---|---|---|
| Agent loop orchestration | `agent/loop.py` | Coordinates channels → runner |
| Runner (iteration loop) | `agent/runner.py` | Executes iterations, tools, hooks |
| Session/memory | `session/`, `agent/memory.py` | Dream consolidation, compaction |
| Tools | `agent/tools/` | 14 registered tools |
| Providers | `providers/` | LLM backends |
| Channels | `channels/` | Telegram, Discord, WebSocket, etc. |
| Bus | `bus/` | Async message passing |
| Config | `config/schema.py` | Pydantic config with posture/delib sections |

---

## 8. Telemetry

`telemetry/logger.py` — writes JSONL events per session to `~/.cache/durin/telemetry/`.

Registered events:
- `posture.initial` — vector at startup
- `posture.change` — each vector change (axes, deltas, events)
- `deliberation.start` — deliberation started
- `deliberation.result` — result (perspectives, synthesis, timing)
- `plan.tier_set` — tier declared by the agent
- `plan.phase_transition` — phase change in cycle
- `plan.confirm_result` — confirmation result (pass/fail)

---

## 9. What Doesn't Exist Yet

| Component | Status | Reference Doc |
|---|---|---|
| **Metacognition/reflection** | Not implemented | Investigated (ReMA, Reflexion), doubtful without oracle |
| **Memory graph** | Designed in docs, not implemented | `docs/03_durin_memoria.md` |
| **Context projection** | Not implemented | Design doc §4.1 |
| **Consolidation (sleep)** | Not implemented | Phase 3 roadmap |
| **Postural mean adjustment** | Not implemented | Phase 3 roadmap |
| **Evolutionary deliberation** | Designed, not implemented | Plan: mutation/crossover between rounds |

---

## 10. Evaluation Scripts

| Script | Purpose |
|---|---|
| `scripts/swebench_eval.py` | Benchmark Durin on SWE-bench Lite |
| `scripts/swebench_nanobot_eval.py` | Benchmark base nanobot (no posture/delib) |
| `scripts/simulate_posture_session.py` | Manual posture session simulation |

Results stored in `benchmarks/swebench_5/`.

---

## 11. Tests

```bash
pytest tests/deliberation/ -v   # Engine, synthesis, service, types, history
pytest tests/posture/ -v         # Vector, homeostasis, stimulus, phrase, goal_bias
pytest tests/plan/ -v            # Plan hook, types, store, tools
pytest tests/ -q                 # Full suite (3300+ tests)
```
