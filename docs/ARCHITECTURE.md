# Durin — Operational Architecture

> Complete reference for understanding Durin's internals: what each system does,
> how it works, and **why** it was designed that way.
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
- Deliberation V3 (single-call multi-perspective, post-error only)
- Plan system (fast-path execute→verify, escalate to full plan on failure)
- Temperature modulation per phase
- Structured telemetry (posture, deliberation, rate limits)
- Hook factory that auto-wires posture + plan (with integrated deliberation)

**Why fork instead of plugin?** Nanobot's hook system is enough for posture and plan,
but temperature modulation, forced verification blocking, and context injection require
tighter integration than a plugin API allows. The fork keeps upstream compatibility
while allowing deep behavioral changes.

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
│     ├── PlanHook: inject phase prompt (fast path or full)    │
│     ├── PlanHook: deliberation (only after verify failure)   │
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

**Why hooks instead of hard-coded logic?** Posture and plan are orthogonal concerns
that should be independently toggleable. The hook interface (`before_iteration`,
`after_iteration`) lets us compose behaviors: posture-only, plan-only, both, or
neither (nanobot mode). The `CompositeHook` runs them in sequence.

---

## 3. Posture System

### Why It Exists

LLMs have a fixed behavioral profile per conversation: same caution level from start to
finish. Real engineers adapt — more careful after a test failure, more exploratory when
stuck. Posture gives the agent a continuous behavioral vector that shifts in response to
events, modulating how the agent approaches each iteration.

**Empirical evidence:** Posture significantly improves resolution rate over the base
agent. The primary mechanism is caution increase after failures, which prevents the
agent from repeating the same mistake. See `docs/06_log_benchmark.md` for data.

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

**Why 5 axes?** Reduced from the original 7 — `persistence` and `curiosity` never
meaningfully diverged from mean. 5 axes cover the behavioral space without redundancy.

**Why return-to-mean (homeostasis)?** Without it, axes drift monotonically toward
extremes after a few events. Return force ensures the agent naturally resets between
tasks and doesn't get permanently "scared" or "reckless" from a single bad experience.

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

Posture influences LLM temperature via the PlanHook:
- High caution → slightly lower temperature in EXECUTE/VERIFY (more deterministic edits)
- High exploration → slightly higher temperature in INVESTIGATE (more creative search)
- Modulation range: ±0.05 on top of phase base temperature

**Why not wider modulation?** Experiments with ±0.15 caused erratic behavior in
EXECUTE phase (hallucinated code). The ±0.05 range is enough to nudge without
destabilizing generation quality.

---

## 4. Deliberation System (V3)

### What It Is

A single LLM call that generates 3 perspectives (Critic → Explorer → Pragmatic) plus
a synthesis, using a **separate API call** from the main agent conversation. The output
is parsed by marker regex and injected as a system message into the agent's context.

**It is NOT part of the agent's own reasoning** — it's an external analysis step that
adds ~17-20s of latency (the time for the separate LLM inference call).

### Why a Separate Call (Not Inline)

An alternative would be injecting multi-perspective instructions into the agent's own
prompt and letting it reason from multiple angles in-line. We chose a separate call because:

1. **Temperature isolation**: Deliberation uses temp=0.4 (creative analysis), while
   EXECUTE uses temp=0.15 (deterministic code). A single conversation can't use both.
2. **Context control**: The deliberation prompt includes only investigation excerpts +
   posture snapshot, not the full conversation. This prevents the LLM from anchoring
   on its own prior reasoning.
3. **Parseability**: Forced [MARKER] format in a dedicated call is reliable. In-line,
   the agent often skips the structure or merges perspectives.

**Trade-off**: 17-20s latency per deliberation call. Acceptable because deliberation
only fires after a verification failure (see below), not on every task.

### Why Post-Error Only (Not Preventive)

**Why not preventive (V2)?** Preventive deliberation (firing before every task) was
neutral-to-harmful in benchmarks: the agent either ignored it or followed bad advice.
Speculating about problems without concrete failure data produced unreliable guidance.

**V3 design** (current): Deliberation fires only after verify failure. At that point:
- `previous_failure` provides concrete error context (not speculation)
- The Critic can analyze *what actually went wrong* instead of guessing risks
- The agent has already tried and failed, so alternative perspectives have real value

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
1. Agent's fast-path fix FAILS verification → cycle escalation
2. PlanHook resets to INVESTIGATE phase (cycle 2+)
3. Agent investigates with failure context, calls update_plan → INVESTIGATE→PLAN
4. PlanHook._run_deliberation(context) fires with previous_failure context
5.   → 1 separate LLM call (temp=0.4) with forced ordering:
      [CRITIC] → [EXPLORER] → [PRAGMATIC] → [SYNTHESIS]
6.   → _parse_response(): regex split by markers → Perspective tuples + synthesis
7.   → Logs full result to telemetry (3 perspectives + synthesis + posture + timing)
8. → render_for_injection() → injected as system message into agent context
```

### Perspective Ordering

The ordering Critic → Explorer → Pragmatic is intentional:
- **Critic first**: Identifies risks without a solution to defend. No anchoring bias.
- **Explorer second**: Proposes alternatives knowing the risks, forced to add new info.
- **Pragmatic third**: Direct path, but conditioned by risks + alternative — can't
  ignore them.
- **Synthesis**: Merges, resolves contradictions explicitly.

Each perspective must add new information — the prompt enforces this with
"Do NOT repeat what was said in previous perspectives."

---

## 5. Plan System

### Why It Exists

Base LLM agents (nanobot) often:
1. Edit code without reading enough context first
2. Never verify their fix works (no test run)
3. When a fix fails, retry the same approach instead of re-investigating

The plan system enforces structure: forced verification before completion, and
escalation to a full investigation cycle when the direct approach fails.

### Why Fast Path First (Not Always Full Plan)

**Why not always-plan?** Benchmarks showed identical patches whether the agent
investigated first or jumped straight to editing — but with ~45s extra overhead per task.
Full investigation only adds value when the direct approach fails.

**Current design**: Start with EXECUTE→VERIFY (fast path). Only escalate to the full
cycle when verification fails. This gives nanobot-equivalent speed on easy problems
while retaining the plan's value on hard problems.

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
| `plan` | Any task that edits code | Fast path + escalation + forced verification |

### Fast Path + Escalation
```
EXECUTE → VERIFY ──┐
                   │ pass → complete_goal
                   │ fail ↓
         INVESTIGATE → PLAN → EXECUTE → VERIFY ─┐
              ↑     (deliberation)               │ (fail)
              └──────────────────────────────────┘
```

**Cycle 1 (fast path)**: Agent starts at EXECUTE — reads code, makes edit, verifies
directly. No investigation overhead, no deliberation. Equivalent speed to a base agent.
The only constraint: `complete_goal` is blocked until a successful `exec` (forced verify).

**Cycle 2+ (full plan)**: If verification fails, escalates to INVESTIGATE with failure
context. When agent transitions to PLAN, deliberation fires (now analyzing concrete
failure, not speculating). Full INVESTIGATE → PLAN → EXECUTE → VERIFY cycle.

### Phases and Temperature

| Phase | Base Temp | When | Rationale |
|---|---|---|---|
| EXECUTE (cycle 1) | 0.15 | Fast path: read + edit + verify | Low temp for precise code edits |
| INVESTIGATE | 0.5 | After failure: understand what went wrong | Higher temp for exploratory reading |
| PLAN | 0.4 | Define steps with deliberation input | Moderate for reasoning |
| EXECUTE (cycle 2+) | 0.15 | Implement planned fix | Low temp for precise edits |
| VERIFY | 0.1 | Run tests/commands | Lowest temp — just execute commands |

**Why different temperatures per phase?** A single temperature forces a trade-off:
high enough for creative investigation but too high for precise code editing. Phase
temperatures let us be exploratory when reading and deterministic when writing.

### Forced Verification

`complete_goal` is **blocked** until verification passes:
- After any edit, `verify_passed` is set to False
- Only a successful `exec` call (no error) sets `verify_passed = True`
- Calling `complete_goal` without verification returns an error message

**Why force it?** Without this, the agent declares "done" after editing without
testing. In benchmarks, this was the single biggest source of false-positive patches:
code that looked correct but failed tests.

### Phase Transitions (inferred automatically)
| Transition | Trigger |
|---|---|
| EXECUTE → VERIFY | `exec` detected after edits (cycle 1 fast path) |
| VERIFY → INVESTIGATE | Error in exec → escalate to full plan (cycle 2+) |
| INVESTIGATE → PLAN | `update_plan("add", ...)` is called → deliberation fires |
| PLAN → EXECUTE | Edit tool (`edit_file` or `write_file`) detected |
| EXECUTE → VERIFY | `exec` detected after edits (cycle 2+ planned path) |

**Why inferred instead of explicit?** Requiring the agent to call `set_phase()` adds
tool call overhead and the agent often forgets. Inferring from tool usage (edit → we're
executing, exec after edit → we're verifying) is more reliable and transparent.

### Intelligent Stop (cycle 2+)

When PLAN phase is entered on cycle > 1, a self-evaluation prompt is injected:
> "Your previous fix FAILED verification. Do you have a genuinely DIFFERENT approach?
> If not, call complete_goal with what you learned."

**Why no max_cycles limit?** A hard limit (e.g., max 3 cycles) either cuts off
solvable problems or wastes iterations on unsolvable ones. The self-evaluation prompt
lets the model make a judgment call: "I have a new idea" (continue) or "I've exhausted
my approaches" (stop gracefully). This avoids both premature termination and infinite
loops.

### Emitted Stimuli (posture ↔ plan bridge)
| Event | When | Postural Effect |
|---|---|---|
| `verify_pass` | Tests pass in VERIFY | caution −0.10 |
| `verify_fail` | Tests fail in VERIFY | caution +0.15, depth +0.10 |
| `cycle_restart` | Verify fail → new cycle | discipline +0.05, exploration +0.10 |
| `plan_complex` | Plan exceeds >3 items | depth +0.10 |

**Why bridge posture and plan?** Verification results are the strongest behavioral
signal. A test failure should make the agent more cautious (posture) AND trigger
re-investigation (plan). The stimulus bridge ensures both systems react coherently.

### Persistence
Each session with `plan` tier generates:
- `plans/{session_key}/plan.json` — current state (tier, phase, items, cycle_count)
- `plans/{session_key}/events.jsonl` — event log (for post-hoc analysis)

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

**Why is Deliberation a service inside PlanHook, not a separate hook?** Deliberation
needs to fire at a specific point in the plan lifecycle (INVESTIGATE→PLAN transition,
cycle 2+) and inject its output into the agent context before the next LLM call.
A separate hook would need complex coordination with PlanHook about timing. As an
injected service, PlanHook controls exactly when deliberation runs.

### Inter-hook Communication

`AgentHookContext.external_stimulus_events: list[str]` allows PlanHook to emit postural
events (VERIFY_PASS, VERIFY_FAIL, CYCLE_RESTART, PLAN_COMPLEX) that PostureHook
consumes in its next iteration.

`AgentHookContext.temperature_override: float | None` allows PlanHook to set the LLM
temperature for the current iteration based on phase + posture.

**Why not direct hook-to-hook references?** The event list keeps hooks decoupled.
PostureHook doesn't know PlanHook exists — it just reacts to events. This means posture
works identically whether plan is enabled or not.

---

## 7. Provider and Rate Limit Handling

### Retry Logic

All LLM calls go through `LLMProvider.chat_with_retry()` which delegates to
`_run_with_retry()`:

| Mode | Delays | Max Attempts | Use Case |
|---|---|---|---|
| `standard` | 1s, 2s, 4s | 3 | Normal agent iterations |
| `persistent` | 1s → 60s cap | Until 10 identical errors | Long-running operations |

The provider distinguishes transient errors (retryable: 429 rate limit, 500/502/503/504
server errors, timeouts) from permanent errors (quota exhausted, billing issues).

### Rate Limit Detection

HTTP 429 responses are classified into two categories:
- **Retryable**: `rate_limit_exceeded`, `too_many_requests`, `overloaded_error`
- **Non-retryable**: `insufficient_quota`, `quota_exceeded`, `billing_hard_limit_reached`

The provider extracts `Retry-After` headers (seconds or HTTP-date format) and uses
them as retry delays when available, falling back to exponential backoff otherwise.

### Structured Telemetry

Rate limit events are logged to the session's JSONL telemetry file:
- `provider.rate_limit` — each retry attempt (attempt number, delay, status code, error)
- `provider.rate_limit_exhausted` — when all retries fail

**Why structured telemetry in addition to loguru?** Loguru logs are human-readable but
hard to aggregate across benchmark runs. JSONL events can be filtered, counted, and
correlated with other telemetry (posture changes, deliberation timing) programmatically.

---

## 8. Nanobot Inheritance — What We Don't Touch

| Subsystem | Location | Notes |
|---|---|---|
| Agent loop orchestration | `agent/loop.py` | Coordinates channels → runner |
| Runner (iteration loop) | `agent/runner.py` | Executes iterations, tools, hooks |
| Session/memory | `session/`, `agent/memory.py` | Dream consolidation, compaction |
| Tools | `agent/tools/` | 14 registered tools |
| Providers | `providers/` | LLM backends (with Durin's telemetry additions) |
| Channels | `channels/` | Telegram, Discord, WebSocket, etc. |
| Bus | `bus/` | Async message passing |
| Config | `config/schema.py` | Pydantic config with posture/delib sections |

**Why keep nanobot's structure intact?** Minimizes merge conflicts when pulling upstream
changes. Durin's additions are in separate modules (`posture/`, `deliberation/`,
`plan/`) or injected via hooks — we don't modify nanobot's core loop.

---

## 9. Telemetry

`telemetry/logger.py` — writes JSONL events per session to `~/.cache/durin/telemetry/`.

### Registered Events
| Event Type | Payload | When |
|---|---|---|
| `posture.initial` | Axis values at startup | Session start |
| `posture.change` | Axes, deltas, stimulus events | Each vector update |
| `deliberation.result` | Perspectives, synthesis, timing, posture | After deliberation call |
| `deliberation.error` | Error message | Deliberation failure |
| `provider.rate_limit` | Attempt, delay, status code, error | Each retry on rate limit |
| `provider.rate_limit_exhausted` | Total attempts, error | All retries failed |

Plan events are stored separately in `plans/{session_key}/events.jsonl`:
| Event Type | Payload | When |
|---|---|---|
| `tier_set` | Tier value, reason | Agent declares execution mode |
| `phase_transition` | From/to phase, cycle | Phase change |
| `plan_item_added` | Item description, cycle | Step added to plan |
| `plan_item_completed` | Item description, cycle | Step marked done |
| `verify_result` | Outcome (pass/fail), cycle | Verification result |

**Why two separate event streams?** Telemetry events are per-session across the whole
agent lifetime (posture, provider). Plan events are per-task within a session. Keeping
them separate avoids interleaving unrelated concerns and makes per-task analysis cleaner.

---

## 10. Evaluation Infrastructure

### Scripts
| Script | Purpose |
|---|---|
| `scripts/swebench_eval.py` | Benchmark orchestrator (`--docker-internal`, `--agent`, `--concurrency`) |
| `scripts/swebench_docker.py` | Docker container lifecycle (image build, internal/external modes) |
| `scripts/swebench_run_inside.py` | Agent runner inside Docker container (entrypoint for both agents) |
| `scripts/simulate_posture_session.py` | Manual posture session simulation |

### Docker-Internal Mode (recommended)

Agent runs INSIDE the SWE-bench container. This eliminates host path contamination
and ensures the agent sees the exact same filesystem as the test harness.

**Architecture: 3-layer Docker images**
```
sweb.env.{platform}    ← Base: conda + Python version (shared across repos)
  └─ sweb.eval.{instance} ← Instance: repo cloned + compiled + pip install -e .[test]
       └─ durin.eval.{instance} ← Durin layer: conda env 'durin' with agent deps
```

**Two isolated conda environments inside each container:**
- `testbed`: Project dependencies. Untouched by the agent. Tests run here.
- `durin`: Agent dependencies (pydantic, httpx, tiktoken, etc.). Completely isolated.

**Why two envs?** Previous external mode (agent on host, exec via docker exec) caused
test contamination: agent's pip packages leaked into the test environment, causing false
passes/failures. Separate conda envs guarantee the agent never modifies the test
environment.

**Volume mounts:**
- `/opt/durin` (read-only): Agent source code from host
- `/output` (read-write): Results written by agent (patch.diff, result.json, telemetry)

```bash
# Durin (full features: posture + plan + post-error deliberation)
python scripts/swebench_eval.py --docker-internal --agent durin --instance-ids ...

# Nanobot (base agent, no hooks — for A/B comparison)
python scripts/swebench_eval.py --docker-internal --agent nanobot --no-deliberation --instance-ids ...

# Control concurrency to avoid rate limits
python scripts/swebench_eval.py --docker-internal --concurrency 1 --instance-ids ...
```

**Why `--concurrency` defaults to 1?** Each instance makes ~10-20 LLM API calls.
Running N instances in parallel multiplies API load by N. With rate-limited APIs
(especially during benchmarks), parallel execution causes 429 errors and wasted retries.
Sequential execution is slower but reliable. Increase only when the API quota permits.

Results stored in `benchmarks/swebench_5/`.

---

## 11. Tests

```bash
pytest tests/deliberation/ -v   # Engine, synthesis, service, types, history, plan integration
pytest tests/posture/ -v         # Vector, homeostasis, stimulus, phrase, goal_bias
pytest tests/plan/ -v            # Plan hook, types, store, tools
pytest tests/ -q                 # Full suite (3300+ tests)
```

---

## 12. References

For benchmark data and design evolution, see:
- `docs/06_log_benchmark.md` — Benchmark results and analysis
- `docs/07_design_plan_and_stimuli.md` — Design decisions and stimuli changes
- `docs/05_log_guiding_thread.md` — Implementation evolution
