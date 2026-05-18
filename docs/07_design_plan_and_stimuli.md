# Design: Plan, Stimuli, and Reflection

> Design decisions post-benchmark May 2026.
> What to implement, what to document for later, and why.

---

## 1. Plan System — IMPLEMENT

### Problem

The agent executes reactively without a plan. In tasks with few iterations (2-3), it jumps directly to editing without exploring or verifying. Concrete example: `astropy-6938` — the agent applied an incorrect fix in 2 iters without reading the full file or running tests. With a plan that enforces the cycle, it would have been at minimum 4 phases.

### State of the Art — Existing Agents

| Agent | Explicit plan | Mandatory verification | Live plan | Re-planning | History for learning |
|---|---|---|---|---|---|
| **Hermes (NousResearch)** | Yes — trained `<PLAN>/<EXECUTION>/<REFLECTION>` tokens | No (soft gate) | Yes — persistent goals, multi-level memory | ReAct loop + RL (Atropos) | Yes — persistent cross-session memory |
| **Devin (Cognition)** | Yes — upfront plan visible to user | Iterative (tests in loop) | Yes — updates when decomposing | Decompose→Execute→Analyze failure→Retry | Multi-hour context, no public learning |
| **OpenHands/OpenDevin** | Not rigid — CodeAct paradigm | Sandbox available, not mandatory | Event stream (event-sourced) | Observation-action loop with error feedback | Event stream stored, deterministic replay |
| **SWE-Agent (Princeton)** | No — pure ReAct with ACI tools | Can run tests, not forced | No — context window is the "memory" | Error messages via ACI feedback | No cross-session |
| **Agentless (UIUC)** | Yes — rigid 3-phase pipeline | **Mandatory** — regression + reproduction tests | No (static) | No retry, samples multiple candidates | No |
| **AutoCodeRover** | Semi — short plan per iteration | Tests on generated patches | Iterative context that evolves | Re-generates patches if tests fail | No |
| **Claude Code (/plan)** | Yes — read-only mode for planning | Human reviews before executing | Plan as persistent markdown | Human re-enters plan mode | No |
| **Aider** | Yes — Architect/Editor split | Linter + configurable tests, auto-fix | No formal | Automatic retry on lint/test failures | Git as implicit log |

### Academic Patterns (2024-2025)

| Pattern | Mechanism | Key property |
|---|---|---|
| **ReAct** | Thought→Action→Observation loop | Dynamic, no upfront plan |
| **Plan-and-Execute** (LangChain) | Planner generates plan, Executor executes steps | Planning/execution separation |
| **Plan-Act-Correct-Verify** (2024) | 4 iterative modules | Explicit verifier — outperforms ReAct on complex tasks |

**Key finding**: Agents with mandatory verification (Agentless) consistently outperform those that "can verify but are not forced to" (SWE-Agent, OpenHands). Plan-as-TODO works because LLMs do not skip steps from a checklist.

### Proposed Design for Durin — 3-Tier Model + Fixed Cycle

**Benchmark insight**: The agent declares victory without verifying (5/5 "resolved" internally, only 3/5 pass real tests). The plan system forces verification.

#### 3-Tier Execution Model

Not every task needs a full plan. The LLM chooses the appropriate tier via tool call at the start:

```
┌─────────────────────────────────────────────────────────────┐
│  TIER 1 — DIRECT                                            │
│  Answers, trivial edits.                                    │
│  No artifacts. Hook does not intervene.                     │
│  Example: "What does this function do?" / "Rename X to Y"  │
├─────────────────────────────────────────────────────────────┤
│  TIER 2 — EXECUTE + VERIFY                                  │
│  Clear bug fix, localized change.                           │
│  Hook injects reminder to verify post-edit.                 │
│  No plan or persistent log.                                 │
│  Example: Fix a failing test, single-file change            │
├─────────────────────────────────────────────────────────────┤
│  TIER 3 — FULL PLAN                                         │
│  Multi-step, uncertainty, structural changes.               │
│  Mandatory fixed cycle + incremental plan + log.            │
│  Example: New feature, bug without clear cause, refactoring │
└─────────────────────────────────────────────────────────────┘
```

**Tier selection**: The model receives instructions about the 3 modes in the system prompt and declares which to use via `set_execution_mode(tier)` as a tool call. The hook captures the declaration and enforces the corresponding behavior.

**Why tool call and not pattern detection**: It is an explicit gate without ambiguity. Allows the hook to react immediately without parsing free text.

#### Fixed Cycle (Tier 3 only)

**Philosophy**: We do not generate a complete plan once. The plan EMERGES from a repeating cycle:

```
┌──────────────────────────────────────────────┐
│  FIXED CYCLE (Tier 3 only):                  │
│                                              │
│    INVESTIGATE → PLAN → EXECUTE → CONFIRM    │
│        ↑                              │      │
│        └──────── if fails ────────────┘      │
└──────────────────────────────────────────────┘
```

- **INVESTIGATE**: Read files, understand context. Cannot edit.
- **PLAN**: Formulate/update concrete steps. Plan grows incrementally.
- **EXECUTE**: Apply changes (edit, write).
- **CONFIRM**: Verify (exec tests, validation). Real oracle — CANNOT be skipped.

The plan is **incremental** (like agile methodologies): each cycle can add steps, modify existing ones, or mark them complete. The log records each modification.

#### Artifacts per Tier

| Tier | Plan on disk | Log (events.jsonl) | Posture reacts |
|---|---|---|---|
| 1 | No | No | Yes (normal stimuli) |
| 2 | No | No | Yes + VALIDATION_SUCCESS/FAILURE |
| 3 | Yes | Yes | Yes + Layer 2 (CONFIRM) + Layer 3 (bias) |

Only Tier 3 generates persistent artifacts. The others are "free" in overhead.

### Implementation

```python
# durin/plan/types.py

class ExecutionTier(StrEnum):
    DIRECT = "direct"           # Tier 1: answers, trivial
    EXECUTE_VERIFY = "execute_verify"  # Tier 2: edit + verify
    FULL_PLAN = "full_plan"     # Tier 3: complete cycle

class Phase(StrEnum):
    INVESTIGATE = "investigate"
    PLAN = "plan"
    EXECUTE = "execute"
    CONFIRM = "confirm"

@dataclass
class PlanItem:
    description: str
    status: Literal["pending", "in_progress", "done", "failed"]
    added_at_cycle: int
    completed_at_cycle: int | None = None

@dataclass  
class PlanState:
    tier: ExecutionTier
    goal: str
    items: list[PlanItem]          # Only populated in Tier 3
    current_phase: Phase | None    # None for Tier 1-2
    cycle_count: int
```

```python
# durin/plan/tool.py — Tool that the LLM calls to declare tier

class SetExecutionModeTool(Tool):
    """The LLM declares which execution tier to use."""
    name = "set_execution_mode"
    parameters = {
        "tier": {"type": "string", "enum": ["direct", "execute_verify", "full_plan"]},
        "reason": {"type": "string", "description": "Why this tier (1 sentence)"}
    }
```

```python
# durin/plan/hook.py — PlanHook

class PlanHook(AgentHook):
    """Manages execution tiers. Only enforces cycle for Tier 3."""
    
    _state: PlanState
    _store_path: Path  # workspace/plans/{session_key}/
    
    async def before_iteration(self, ctx):
        if self._state.tier == ExecutionTier.DIRECT:
            return  # No intervention
        
        if self._state.tier == ExecutionTier.EXECUTE_VERIFY:
            # Only injects post-edit reminder: "Verify with tests"
            if self._detected_edit_last_iter:
                ctx.inject("Remember to verify your change with tests before declaring complete.")
            return
        
        # Tier 3: Injects full plan state + phase
        # "[Plan] Cycle 2 | Phase: CONFIRM
        #  1. [✓] Fix replace in fitsrec.py  
        #  2. [→] Verify with tests
        #  Required action: run the relevant tests."
        
    async def after_iteration(self, ctx):
        # 1. Detect current phase by tools used
        # 2. Detect phase transitions
        # 3. Capture CONFIRM result (oracle)
        # 4. Update plan items
        # 5. Emit posture events
        # 6. Append to log on disk
```

### Prompt Injection (what the LLM sees)

```
[Plan System]
Cycle 1 | Phase: EXECUTE
Current plan:
  1. [✓] Understand: output_field is numpy chararray view (in-place)
  2. [→] Implement: use output_field[...] = for in-place writing
  3. [ ] Verify: run tests astropy.io.fits
Log: 
  [C1-INVEST] Read fitsrec.py:1255-1270, chararray.replace() returns copy
```

The LLM CANNOT declare "done" with step 3 pending — it is an active TODO.

### Disk Storage (for auditing and learning)

```
workspace/plans/{session_key}/
  events.jsonl    — append-only, each cycle event
  summary.json    — at completion: outcome, cycles, plan evolution
```

```jsonl
{"ts": ..., "type": "cycle_start", "cycle": 1, "phase": "investigate"}
{"ts": ..., "type": "phase_transition", "from": "investigate", "to": "plan"}
{"ts": ..., "type": "plan_item_added", "item": "Fix replace in fitsrec.py", "cycle": 1}
{"ts": ..., "type": "phase_transition", "from": "execute", "to": "confirm"}
{"ts": ..., "type": "confirm_result", "outcome": "fail", "signal": "pytest exit=1"}
{"ts": ..., "type": "cycle_start", "cycle": 2, "phase": "investigate"}
{"ts": ..., "type": "plan_item_modified", "item": "Use [...] assignment", "reason": "replace returns copy"}
{"ts": ..., "type": "confirm_result", "outcome": "pass", "signal": "pytest exit=0"}
{"ts": ..., "type": "plan_completed", "cycles": 2, "total_iters": 8}
```

**Future value**: This log enables detecting patterns (which tasks require >2 cycles, what type of initial plans tend to be incorrect, correlation between insufficient investigation and confirmation failures).

---

## 2. Stimuli — 3-Layer Model

### Posture Adjustment Architecture

Stimuli operate at 3 complementary frequencies that coexist:

```
Layer 1 — PER-ITERATION (fast, already implemented):
  step_succeeded, consecutive_3, STUCK, MULTI_FILE_EDIT...
  Micro-adjustments: ±0.02-0.08 per event
  Function: react to the immediate moment

Layer 2 — PLAN CYCLE PHASE TRANSITION (medium, new):
  CONFIRM pass/fail, cycle 2+ started, re-plan triggered
  Macro-adjustments: ±0.10-0.15 per transition
  Function: react to the real result (oracle)

Layer 3 — PLAN CREATION/RE-CREATION (slow, new):
  When generating the plan: evaluate complexity → initial bias
  One-shot: adjustment at cycle start
  Function: prepare posture for what is coming
```

### Layer 1: Per-iteration Stimuli (IMPLEMENTED)

| Event | Axes | Delta | Status |
|---|---|---|---|
| `STEP_FAILED` | caution +0.10, depth +0.05 | | ✓ |
| `CONSECUTIVE_SUCCESSES_3` | exploration +0.02, depth -0.03 | | ✓ |
| `CONSECUTIVE_FAILURES_3` | caution +0.15, conformity -0.10 | | ✓ |
| `MULTI_FILE_EDIT` | discipline | +0.08 | ✓ |
| `VALIDATION_SUCCESS` | caution -0.05, exploration -0.03 | | ✓ |
| `VALIDATION_FAILURE` | caution +0.10, depth +0.08 | | ✓ |
| `STUCK_NO_PROGRESS` | exploration +0.10, depth +0.10 | | ✓ |
| `PHASE_TRANSITION` | depth | -0.10 | ✓ |

**Removed**: `STEP_SUCCEEDED` no longer affects caution. Signal too weak (absence of error ≠ progress). Caution only decreases with real oracle (VALIDATION_SUCCESS).

### Weight Calibration — Behavioral Targets

Config caution: mean=0.6, variance=0.15, return_force=0.3, bounds=[0.30, 0.90]

| Scenario | Target | Simulated result | Criterion |
|---|---|---|---|
| 3 consecutive failures | Caution ~0.85+ | Peak 0.858, recovers in 8 iters | Agent completely rethinks |
| 1 VALIDATION_FAILURE | +10-15% | +12% (0.60→0.67) | Investigates before re-editing |
| 1 VALIDATION_SUCCESS | Drops slightly | -6% (0.60→0.565) | Confidence without relaxing |
| Normal operation | Stable at mean | 0.600 constant | No drift |
| Fail→investigate→test passes | Natural recovery | 0.67→0.589 | Healthy cycle |
| 2 iters without test (case 6938) | Must NOT drop | 0.600 stable | No false reward |

**Intentional asymmetry**: raising caution (+0.10) is 2x lowering it (-0.05). Losing confidence is easy, earning it requires oracle.

**Future evolution**: The plan log records posture at each decision + outcome. After N sessions it enables correlating posture ranges with resolve rate → adjust deltas to maintain empirically optimal range.

### Layer 2: Plan Cycle Stimuli (TO BE IMPLEMENTED)

| Event | Axes | Delta | Trigger |
|---|---|---|---|
| `CONFIRM_PASS` | caution -0.10, exploration -0.05 | | Tests pass in CONFIRM phase |
| `CONFIRM_FAIL` | caution +0.15, depth +0.10 | | Tests fail in CONFIRM phase |
| `CYCLE_2_PLUS` | discipline +0.05, depth +0.05 | | Start of cycle 2 or later |
| `REPLAN_TRIGGERED` | exploration +0.10 | | Plan modified due to failure |

### Layer 3: Plan Bias (TO BE IMPLEMENTED)

| Plan signal | Adjustment | Reason |
|---|---|---|
| Plan with >3 steps | depth +0.10, caution +0.05 | Complex task, needs care |
| Plan with 1 step | keep defaults | Simple task, do not overthink |
| Re-plan (cycle 2+) | caution +0.10, exploration +0.05 | First approach failed, needs alternatives |

### Relationship Between Layers

The layers do NOT contradict each other — they operate at different frequencies:
- Layer 1: "this individual step went well" (weak signal, frequent)
- Layer 2: "the complete cycle worked/failed" (strong signal, infrequent)
- Layer 3: "this problem is going to be hard" (contextual signal, once per task)

The layers are additive. Existing ones are not removed — only stronger signals are added at more significant moments.

---

## 3. Metacognition / Self-Reflection — DOCUMENT (do not implement yet)

### Why Not Implement Now

Evidence from benchmark and literature:
- Without external oracle (tests pass/fail), reflection reinforces own errors
- Diminishing returns after 1-2 iterations
- ReMA (NeurIPS 2025) requires training via RL — inaccessible
- Reflexion (Shinn 2023) reflects BETWEEN attempts, not mid-task
- Our benchmark showed that LLM evaluators do not help without real verification

### When It Would Make Sense

- When we have **plan with CONFIRM cycle** → the oracle (tests) gives real signal
- When we have **persistent log** → data to detect patterns
- When tasks are **sufficiently long** (>50 iterations) to justify the cost

### Key Insight: Oracle + Learning

Success/failure events (oracle) have dual value:
1. **Immediate**: decide whether to re-plan for the current task
2. **Future**: material for adjusting postural defaults by task type

This connects with future consolidation: the success/failure moments marked in the log become data for agent evolution.

---

## 4. Carry-posture Bug Fix — ✓ IMPLEMENTED

Separate `current_value` from `mean` in the schema. Fix applied in commit `453a070`.

---

## 5. Benchmark Evolution — May 2026

### V5 (5 astropy, external mode → docker-internal)

See `docs/06_log_benchmark.md` for full data. Key findings:
- Posture: 40% → 80% resolution (+100% relative)
- Deliberation V2: neutral-to-harmful
- Carry-posture: geometric drift bug found and fixed

### V5b (5 astropy, docker-internal, plan comparison)

- Always-plan vs no-plan: **identical patches**, ~45s overhead wasted
- Led to fast-path design

### V6 (10 mixed instances, docker-internal, fast-path + post-error delib)

- Durin: 9/9 patches (sympy skipped, Docker OOM)
- Nanobot: 9/10 patches (sympy NO PATCH after 902s)
- 3/9 identical patches, 6/9 different approaches
- SWE-bench evaluation pending
- Durin timing data lost (process crash)

### Resolved Issues
- **6938 no-verify regression**: Plan system now forces verification
- **Depth axis static**: Now moves (0.42-0.73) with new stimuli
- **Carry-posture drift**: Fixed (commit 453a070)

---

## 6. Updated Implementation Order

```
1. [✓] Fix carry-posture bug (commit 453a070)
2. [✓] New layer 1 stimuli (9 new rules, 12→21 total)
3. [✓] Re-benchmark V5 — validated: posture moves, no regression
4. [✓] Plan system: fast-path execute→verify + escalation on failure
   4a. [✓] Types: ExecutionTier, Phase, PlanItem, PlanState
   4b. [✓] Tools: set_execution_mode, update_plan (auto-discoverable)
   4c. [✓] PlanHook: fast-path + full cycle + forced verification
   4d. [✓] PlanStore: plan.json + events.jsonl persistence
   4e. [✓] Prompt injection: tier instructions in system prompt
5. [✓] Connect plan → layer 2 stimuli (verify_pass/fail, cycle_restart, plan_complex)
6. [✓] Post-error deliberation V3 (single-call, integrated in PlanHook)
7. [✓] Benchmark V5b — fast-path vs always-plan comparison (docker-internal)
8. [✓] Benchmark V6 — 10 mixed instances, Durin vs Nanobot (docker-internal)
9. [ ] SWE-bench evaluation of V6 patches (pending)
10. [ ] Deliberation V3 rewrite (clean dead code from V2, single-call engine)
11. [ ] Plan bias layer 3 (initial adjustment by complexity)
12. [ ] Docker optimization (conda-pack to reduce disk usage)
13. [ ] Metacognition (when plan + oracle are stable)
```

---

## 6. References

| System/Paper | Year | Relevance for Durin |
|---|---|---|
| **Hermes Agent (NousResearch)** | 2025 | Trained PLAN/EXECUTION/REFLECTION tokens, persistent goals, multi-level memory. Closest model to our design |
| **Devin 2.0 (Cognition)** | 2025 | Live plan visible to user, iteration until success, 18% planning improvement |
| **Agentless (UIUC)** | 2024 | MANDATORY verification is what makes it work. 32% SWE-bench |
| **Plan-Act-Correct-Verify** | 2024 | 4 modules with explicit verifier outperforms ReAct. Validates our cycle |
| **OpenHands** | 2025 | Event-sourced state, CodeAct. Event stream stored for replay |
| **SWE-Agent (Princeton)** | 2024 | ReAct without plan = without mandatory verification = inferior |
| **Manus Context Engineering** | 2025 | Plan-file as attention manipulation. 30% tokens in rewrites |
| **ReMA (NeurIPS)** | 2025 | Meta-agent monitors progress. Requires RL, inspiration for future |
| **Mind Evolution (DeepMind)** | 2025 | Evolution with LLM. Inspired original deliberation |
| **Cambridge Position Paper** | 2025 | Formal framework: metacognitive knowledge + planning + evaluation |

---

## Date: 2026-05-18
