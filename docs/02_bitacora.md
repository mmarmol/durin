# Bitácora — What we discarded and why

> Captures the *why* behind every component we built and removed. Read this **before** proposing to rebuild anything similar — the patterns of failure here are the ones to not repeat.

---

## How to use this document

Each entry describes:
- **What it was** (the mechanism)
- **Why it was tried** (the original theory)
- **What was learned** (experiment results, with references)
- **Why it was discarded** (the conclusion)
- **Lesson** (the generalizable principle)

When proposing a new component, check first whether it matches a pattern listed in *Lessons* below. If it does, the burden of proof is high.

---

## Discarded: Posture Vector

**What it was**: 5-axis vector (caution, exploration, depth, discipline, conformity) with stable means, variance bounds, return-to-mean homeostasis, and updates driven by a stimulus event table. A short "posture phrase" derived from the vector was injected into the system prompt at turn start.

**Why it was tried**: Intuition that an agent should have stable "character" that biases all deliberation. Original design (Doc 02, since removed) explicitly framed it as "guiding thread" — the temperament of the mountaineer.

**What was learned**:
- SWE-bench V6 (May 2026): Durin with full stack scored 3/9 vs Nanobot pelado 3/9. Zero delta.
- V8 multi-condition test (5 conditions × 3 scenarios × 2 trials): posture_only average score 5.00, identical to baseline 5.00.
- V8 combined: posture + plan averaged 4.33 vs baseline 5.00 (-0.67), driven by -2pts on scenario_3 where posture default phrase ("Execute what was requested without deviation") biased the agent toward symptom fixes instead of root cause.
- Agent strategies catalog (`04_agent_strategies_catalog.md`): **no production agent** uses dynamic behavioral weights. Hermes Agent's RL-based behavior optimization is offline, not real-time.

**Why discarded**: Zero measurable benefit alone, measurable harm when combined with another mechanism. No production precedent. The mechanism (thin abstract phrases) is fundamentally different from what industry uses to shape behavior (rich specific rules).

**Lesson**: "Stable character" as an abstract instruction (`"be cautious"`) doesn't change LLM behavior. Specificity does (Aider, PartialOrderEval). If we want to revisit this concept, it has to be rich content keyed to context, not vector-driven phrases.

---

## Discarded: Plan System (tiers, phases, forced verify, cycle escalation)

**What it was**: Two execution tiers (`DIRECT` for trivial tasks, `PLAN` for code edits). The PLAN tier ran a fast path (EXECUTE → VERIFY) and escalated to a full cycle (INVESTIGATE → PLAN → EXECUTE → VERIFY) if verification failed. A forced-verification gate blocked `complete_goal` until an `exec` had succeeded.

**Why it was tried**: The user's daily frustration was that LLMs declare "done" without verifying. The plan tier system was meant to enforce verification structurally.

**What was learned** (V7/V8 with real `PlanHook` + real pytest):
- **forced_verify_gate**: 0 blocks in 24 trials. The agent calls pytest naturally without being forced.
- **cycle_escalation**: 0 escalations in 24 trials. Verify always passed first try in our scenarios.
- **phase prompts can HURT**: scenario_3 baseline (no plan) got 5/5 by re-iterating and finding the root cause. With PlanHook, the VERIFY-phase prompt `"If it passes (exit code 0), you may complete"` stopped iteration at symptom-fix-passes-test, scoring 3/5.
- ~25% more tokens per task with no quality gain.

**Why discarded**: The mechanisms designed to "enforce verification" assume the agent skips verification — empirically false. The explicit phase prompts can cut off productive exploration. The escalation/gate machinery literally never activated.

**Lesson**: Don't design for a failure mode you haven't empirically observed. The agent's "premature completion" problem is real in user reports, but in tests with a competent model and basic tools, it doesn't manifest the way these mechanisms expected.

---

## Discarded: Deliberation V3 (single-call multi-perspective)

**What it was**: One LLM call generating Critic → Explorer → Pragmatic → Synthesis sections, fired at the INVESTIGATE→PLAN transition of cycle 2+.

**Why it was tried**: Inspired by Mind Evolution / multi-agent debate research. The earlier V1 (multi-call) was too expensive, V2 was simplified, V3 collapsed to a single call with structured sections.

**What was learned**:
- Never fired in any experiment. Its trigger depends on `cycle_escalation`, which never fired (see above).
- V6 self-review test (structurally equivalent: same model, structured prompt, asked to consider multiple angles): 12/12 triggered, **0 score change**.
- Devin's multi-perspective architecture uses **separate models** for Planner/Coder/Critic. Single-model role-playing is not the same thing.
- Confirms the academic finding (Reflexion, Constitutional AI literature): same-model self-verification has the same blind spots as the original generation.

**Why discarded**: The trigger never fires in realistic conditions. The mechanism (structured prompt in one model) is empirically equivalent to ineffective self-review.

**Lesson**: Multi-perspective deliberation works when perspectives genuinely differ (different models, different training). Forcing one model to "be Critic then Explorer" is structured prompt engineering, not deliberation.

---

## Discarded: Phase-aware temperatures (0.5 / 0.4 / 0.15 / 0.1)

**What it was**: Different LLM sampling temperatures per phase — high for INVESTIGATE (exploration), lower for EXECUTE (determinism), lowest for VERIFY.

**Why it was tried**: Intuition that exploration benefits from sampling diversity and execution benefits from determinism.

**What was learned**:
- Agent catalog research: industry consensus is **single low temperature** (0.0–0.3) for coding agents (Aider, SWE-agent, Cline/Roo, OpenCode all default near 0).
- Only ThinkCoder (academic paper, ACL 2025) does phase variation; no production agent does it.
- V8 applied phase temperatures and showed no measurable improvement over baseline single temp.

**Why discarded**: Novel without evidence. Tied to the plan-phase system which itself was refuted.

**Lesson**: Novelty for novelty's sake. The industry has converged on a pattern (single low temp) for good empirical reasons. Deviating requires evidence we don't have.

---

## Discarded: Pre-completion Critic (V3/V4)

**What it was**: A separate LLM call before `complete_goal` succeeded, reviewing the work with "clean context" (V3) or against generated acceptance criteria (V4).

**Why it was tried**: User's daily pain — "you said done but missed X". An external reviewer could catch this.

**What was learned**:
- V3 generic Critic (no criteria): approved 10/12 trials, 2 rejections with no measurable score effect. Reasoning: without explicit criteria, the Critic doesn't know what to look for.
- V4 Critic + auto-generated criteria: **scored 1.16 points worse than baseline on average**. The auto-generated criteria were too narrow (generated by the same model with the same blind spots), and giving the agent those narrow criteria caused it to focus literally on them and miss broader concerns.
- The Critic prompt + the agent share a model, hence share blind spots. The Critic in V4 approved 3/3 fixes that missed exemptions even though "use is_tax_exempt" was a derivable criterion.

**Why discarded**: Same-model verification doesn't work. Auto-generated criteria amplify blind spots rather than counteracting them.

**Lesson**: External verification needs *genuinely different* signals — ground truth from tests, or a different model family, or explicit human-authored criteria. Same model + clean context isn't enough.

---

## Discarded: Self-review loop (V6)

**What it was**: Before `complete_goal` was accepted, the system injected a structured self-review prompt asking the agent to walk through 5 questions (re-state task, list edits, list unread files, distinguish root cause vs symptom, identify likely gaps).

**Why it was tried**: If a Critic (separate call) doesn't work, maybe the agent reviewing its own work *with full context* does — Camino B from the user's framing.

**What was learned**:
- 12/12 trials triggered the self-review prompt.
- **0/12 trials changed score**. The agent dutifully answered the 5 questions, then confirmed completion. Cost 2–4 extra iterations and ~25% more tokens for no quality gain.

**Why discarded**: Direct empirical refutation. The agent confirms its own work as "complete" even when prompted to look critically.

**Lesson**: Forcing self-reflection through structured prompts does not surface blind spots the agent didn't already see. The model treats the review as a checklist to pass, not a chance to question.

---

## Discarded: SWE-bench as benchmark

**What it was**: 9 mixed-repo instances from SWE-bench Lite, run with Durin (full stack) vs Nanobot (baseline). Conducted May 2026 (V5/V5b/V6 series).

**Why it was tried**: Standard industry coding benchmark, allows direct comparison with academic agents.

**What was learned**:
- V6 final result: Durin 3/9, Nanobot 3/9. Same instances resolved (astropy-12907, astropy-14995, django-14999).
- 6/9 failures were model-comprehension issues (e.g., numpy chararray view semantics) that no agent-layer mechanism can fix.
- SWE-bench measures "can the LLM produce the right patch", not "can the agent run a process".

**Why discarded** (as a benchmark for *agent* improvements): SWE-bench rewards model capability, not agent-layer choices. For agent work, future benchmarks should be τ-bench (policy adherence + recovery), GAIA (multi-step tool use), or task suites with clear process value.

**Lesson**: Choose benchmarks that test what your component is supposed to change. A benchmark dominated by raw model capability won't show agent-layer differences even if they're real elsewhere.

---

## What we KEEP, and why

### Plumbing (industrial standard, not differentiator)
Basic ReAct loop, tool registry, sandbox (bwrap/docker), session management, multi-channel infrastructure, providers, subagents, MCP support, compaction. These all work, are standard across competitors, are necessary for any agent to function.

### Telemetry (generic only)
`TelemetryLogger` class, `log()` method, `log_rate_limit`/`log_rate_limit_exhausted`, `get_session_logger`. Smart-layer-specific methods (`log_posture_*`, `log_deliberation_*`) were removed. The skeleton remains to support future general execution tracking (iterations, tool calls, tokens, prompts).

### Memory design (Doc 03)
Not yet built. Validated by industry pattern (Hermes +40%). Lower risk than rebuilding "smart" layers because the design is grounded in well-known retrieval and projection patterns.

---

## Synthesized lessons / decision rules

Refer to these when proposing a new component:

1. **No component without empirical or industrial precedent.** "Intuitively it should help" is not enough. Either a published study, or a production agent that ships it, or a controlled experiment we can run.

2. **Mechanisms must demonstrably activate.** If a key code path (e.g. forced gate, escalation, deliberation trigger) doesn't fire in realistic tests, the component is pure overhead even if its concept is sound.

3. **Same-model self-verification is a known anti-pattern.** Confirmed by V3/V4/V6 and academic literature. Verification needs either ground truth (tests) or genuinely different models (Devin pattern).

4. **Specificity beats abstraction.** Empirically validated (Aider +33-41pts, PartialOrderEval +58pts). Generic phrases ("be cautious") do not change LLM behavior; concrete rules do.

5. **Three-trial minimum for any quantitative claim.** Single-shot LLM results are dominated by stochasticity. V8 N=2 was already borderline.

6. **Ceiling-effect scenarios are not tests.** If baseline already gets 5/5, no intervention can be measured. Design scenarios that have measurable variance, or measure on benchmarks with real difficulty.

7. **Distrust "dynamic state" without a clear retrieval target.** Posture failed in part because the vector had nothing concrete to bias — no memory to filter, no skill library to choose from. Dynamic mechanisms only make sense if there's a meaningful library to switch between.

8. **The bottleneck is usually the model, not the process.** SWE-bench V6 conclusively showed this. Agent-layer changes can't fix what the underlying model fails to comprehend.

---

## Source experiments (cross-references)

Detailed traces, raw scores, and per-scenario breakdowns:
- `05_log_swebench.md`: SWE-bench V5/V6 results and rationale for discontinuation
- `06_log_experiments.md`: V3-V8 experimental log (Critic, criteria, self-review, full Durin stack)
- `scripts/hypothesis_test/`: experiment scripts (kept for reference and reproducibility)

---

## Last updated: 2026-05-18
