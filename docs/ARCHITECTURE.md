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
- Plan system (2-tier execution with forced verification)
- Temperature modulation per phase
- Postural telemetry
- Hook factory that auto-wires posture + plan (with integrated deliberation)

---

## 2. Iteration Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    AgentRunner.run()                          │
│  for iteration in range(max_iterations):  [default: 200]     │
│                                                              │
│  1. Context governance (microcompact, snip, budget)          │
│  2. Build AgentHookContext(iteration, messages)              │
│  3. hook.before_iteration(context)                           │
│     ├── PostureHook: iter 0 → goal_bias + protocol_bias     │
│     ├── PlanHook: INVESTIGATE→PLAN triggers deliberation     │
│     ├── PlanHook: inject phase prompt                        │
│     └── PlanHook: set temperature_override for this phase    │
│  4. LLM request → response (with phase temperature)         │
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
| Event | Affected Axis(es) | Trigger |
|---|---|---|
| `STEP_FAILED` | caution +0.10, depth +0.05 | Error or tool failure |
| `CONSECUTIVE_SUCCESSES_3` | exploration +0.02, depth −0.03 | 3 consecutive successes |
| `CONSECUTIVE_FAILURES_3` | caution +0.15, conformity −0.10 | 3 consecutive failures |
| `CRITICAL_ACTION` | caution +0.10 | Tool in CRITICAL_TOOLS set |
| `VALIDATION_SUCCESS` | caution −0.05, exploration −0.03 | Tests pass |
| `VALIDATION_FAILURE` | caution +0.10, depth +0.08 | Tests fail |
| `STUCK_NO_PROGRESS` | exploration +0.10, depth +0.10 | No progress detected |
| `PHASE_TRANSITION` | depth −0.10 | Phase change in plan cycle |
| `VERIFY_PASS` | caution −0.10, exploration −0.05 | Plan: tests pass in VERIFY |
| `VERIFY_FAIL` | caution +0.15, depth +0.10 | Plan: tests fail in VERIFY |
| `CYCLE_RESTART` | discipline +0.05, exploration +0.10 | Verify fail → new cycle |
| `PLAN_COMPLEX` | depth +0.10 | Plan: >3 items |

### Posture → Temperature Modulation
Posture also influences LLM temperature via the PlanHook:
- High caution → slightly lower temperature in EXECUTE/VERIFY (more deterministic edits)
- High exploration → slightly higher temperature in INVESTIGATE (more creative search)
- Modulation range: ±0.05 on top of phase base temperature

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

### When It Deliberates
- Transition INVESTIGATE → PLAN in plan mode
- Re-activates on cycle restart (VERIFY fail → new cycle → new investigation → PLAN)
- On retry: `previous_failure` enriches the prompt with what failed before

---

## 5. Plan System (2 Tiers)

### Key Files
| File | Responsibility |
|---|---|
| `plan/types.py` | `ExecutionTier`, `Phase`, `PlanItem`, `PlanState`, `PHASE_TEMPERATURE` |
| `plan/hook.py` | `PlanHook` — injects instructions, infers transitions, enforces verification |
| `plan/store.py` | `PlanStore` — persistence (plan.json + events.jsonl per session) |
| `agent/tools/plan.py` | Tools: `set_execution_mode`, `update_plan` (auto-discoverable) |

### The 2 Execution Tiers
| Tier | When | What the hook does |
|---|---|---|
| `direct` | Simple questions, trivial edits | Nothing — no overhead |
| `plan` | Any task that edits code | Fixed cycle + forced verification + temperature modulation |

### Fixed Cycle (plan tier)
```
INVESTIGATE → PLAN → EXECUTE → VERIFY ─┐
     ↑                                  │ (fail)
     └──────────────────────────────────┘
```

- **INVESTIGATE**: Read, understand context. NO edits. (temp: 0.5)
- **PLAN**: Define steps via `update_plan(add, ...)`. Last step must be verification. (temp: 0.4)
- **EXECUTE**: Implement. Edit files. (temp: 0.15)
- **VERIFY**: Run tests/commands. Must pass (exit 0) before completion allowed. (temp: 0.1)

### Forced Verification
`complete_goal` is **blocked** until verification passes:
- After any edit, `verify_passed` is set to False
- Only a successful `exec` call (no error) sets `verify_passed = True`
- Calling `complete_goal` without verification returns an error message

### Phase Transitions (inferred automatically)
| Transition | Trigger |
|---|---|
| INVESTIGATE → PLAN | `update_plan("add", ...)` is called |
| PLAN → EXECUTE | Edit tool (`edit_file` or `write_file`) detected |
| EXECUTE → VERIFY | `exec` detected after edits |
| VERIFY → INVESTIGATE | Error in exec → restart cycle |

### Intelligent Stop (cycle 2+)
When PLAN phase is entered on cycle > 1, a self-evaluation prompt is injected:
> "Your previous fix FAILED verification. Do you have a genuinely DIFFERENT approach?
> If not, call complete_goal with what you learned."

The model decides whether to continue or stop — no arbitrary max_cycles limit.

### Temperature Per Phase
| Phase | Base Temp | Rationale |
|---|---|---|
| INVESTIGATE | 0.5 | Exploration, needs flexibility |
| PLAN | 0.4 | Reasoning, moderate |
| EXECUTE | 0.15 | Editing code, maximum determinism |
| VERIFY | 0.1 | Running tests, precision |

### Emitted Stimuli (posture ↔ plan bridge)
| Event | When | Postural Effect |
|---|---|---|
| `verify_pass` | Tests pass in VERIFY | caution −0.10 |
| `verify_fail` | Tests fail in VERIFY | caution +0.15, depth +0.10 |
| `cycle_restart` | Verify fail → new cycle | discipline +0.05, exploration +0.10 |
| `plan_complex` | Plan exceeds >3 items | depth +0.10 |

### Persistence
Each session with `plan` tier generates:
- `plans/{session_key}/plan.json` — current state (tier, phase, items, cycle_count)
- `plans/{session_key}/events.jsonl` — event log

---

## 6. Hook Factory

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
`AgentHookContext.external_stimulus_events: list[str]` allows PlanHook to emit postural events (VERIFY_PASS, VERIFY_FAIL, CYCLE_RESTART, PLAN_COMPLEX) that PostureHook consumes in its next iteration.

`AgentHookContext.temperature_override: float | None` allows PlanHook to set the LLM temperature for the current iteration based on phase + posture.

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
- `plan.verify_result` — verification result (pass/fail)

---

## 9. Evaluation Scripts

| Script | Purpose |
|---|---|
| `scripts/swebench_eval.py` | Benchmark Durin on SWE-bench Lite |
| `scripts/swebench_nanobot_eval.py` | Benchmark base nanobot (no posture/delib) |
| `scripts/simulate_posture_session.py` | Manual posture session simulation |

Results stored in `benchmarks/swebench_5/`.

---

## 10. Tests

```bash
pytest tests/deliberation/ -v   # Engine, synthesis, service, types, history
pytest tests/posture/ -v         # Vector, homeostasis, stimulus, phrase, goal_bias
pytest tests/plan/ -v            # Plan hook, types, store, tools
pytest tests/ -q                 # Full suite (3300+ tests)
```
