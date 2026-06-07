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

> **Counterweight**: not every direction we tested failed. V9 produced what initially looked like a +20pp signal from SOUL.md specificity, but V9d (see `06_log_experiments.md`) showed that result was an artifact of an artificially low `max_tokens=4096` cap — with the cap raised to the model's API limit (131072), all three SOUL conditions tie at 90% pass rate. The remaining signal is **efficiency**: the right SOUL.md cuts the model's internal reasoning by 4-40× for the same correctness. The discarded items below represent dead ends; Phase 1 hardening (described last in this document) represents the small set of changes that survive both our experiments and external validation.

---

## The pivot: from cognitive manipulation to context orchestration

After V3–V9d and external validation from Gemini's analysis, the single most important conclusion is a framing shift in what the execution loop is for.

**Pre-pivot framing (refuted)**: "the execution loop manipulates the model's reasoning — Tree of Thoughts, Actor-Critic, multi-perspective deliberation, posture modulation, self-review, structured phase prompts. The model thinks more correctly because the loop forces it to."

**Post-pivot framing (validated by V3–V9d)**: the execution loop's job is to **inject empirical signal from the environment** into the model's context — not to teach the model to think. Modern frontier-reasoning models (glm-5.1, o-series, Claude thinking) already internalize deliberation via test-time compute; V9d's `reasoning_content` traces of 20k+ tokens per call confirm the model does its own Beam-Search-of-thought latent to the user. External scaffolding that re-implements this in Python is redundant.

What still has empirical value, post-pivot:

| What the loop is FOR | Example |
|---|---|
| Memory / cross-turn context | Memory graph (Doc 03), Reflexion-style failure memory |
| Tool execution + environment feedback | Tests, compiler output, runtime tracebacks |
| State tracking that prevents fixation | Hash-based loop detection (Phase 1, 1A) |
| Safety against tool race conditions | Topological batching (Phase 1, 1B) |
| Robust handling of model-output edge cases | Reasoning-truncation recovery (Phase 1, 2B) |

What the loop should NOT do, post-pivot:

| Anti-pattern | Why |
|---|---|
| Same-model self-verification | Refuted in V3/V4/V6 — model shares blind spots with its own critique |
| Static behavioral modulation (posture) | Refuted in V8 — 0pp delta, harmful when combined |
| Multi-phase deliberation in one model | Refuted in V6 — equivalent to one structured prompt |
| Forced verification gates | Refuted in V7/V8 — 0/24 hits, hurt scenario_3 |
| Cognitive-friction prompt wrapping on errors | Marginal on frontier models; LLM already deliberates |
| MCTS over LLM-evaluated branches | Cost prohibitive; redundant with model's internal reasoning |

The distinction Gemini drew (and our data supports): **MCTS over LLM-evaluated branches is dead**; **MCTS over environment-evaluated branches** (each node runs a compiler or test suite) is still alive — but only for offline autonomous workflows, not for synchronous interactive agents, due to cost.

Key heuristic for any future loop addition: **does this connect the model to a signal it doesn't already produce internally?** If yes (memory, tool output, prior failure trace), it's plausibly valuable. If no (the model can already do this in its reasoning phase), it's redundant.

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
- `06_log_experiments.md`: V3-V9d experimental log (Critic, criteria, self-review, full Durin stack, SOUL.md routing on Exercism)
- `scripts/hypothesis_test/`: experiment scripts (kept for reference and reproducibility)

---

## Phase 1 hardening — what we ADDED to the loop (May 2026)

The post-pivot framing left a small set of concrete improvements that ARE worth implementing — pure context/state orchestration, no cognitive manipulation. All three were added to `durin/agent/runner.py` in May 2026.

### 1A — Hash-based loop detection

**Problem observed**: Even frontier models occasionally fixate on a plan — they emit the same `(tool_name, arguments)` tuple in consecutive turns after that exact call already produced a hard failure (lookup error, exception, "Error: …" string). The model "sees" the failure in the message history but anchors on its plan.

**Fix**: turn-scoped `set[str]` of failed-call signatures (`sha256(tool_name + json.dumps(args, sort_keys=True))`). On a repeat hit we short-circuit with a synthetic "BLOCKED" tool result asking for a different approach. Per-turn scope only (environment state may change across turns).

**What it does NOT block**: pytest failures where the tool itself succeeded but the environment said "test failed" — those are valid signal for the model to fix the code and re-run.

**Test coverage**: `tests/agent/test_runner_tool_execution.py::test_loop_detection_*`

**Lineage**: Suggested by external review (Gemini, May 2026), confirmed by us as state-tracking infrastructure rather than cognitive intervention.

### 1B — Topological tool ordering

**Problem observed**: If the model emits `[edit_file(A), run_tests(A)]` and we parallel-execute, the test may run before the edit lands. Race condition.

**Fix**: walk tool calls in order, group only CONSECUTIVE `concurrency_safe` (read-only + non-exclusive) tools into parallel batches. Mutations and exclusives are singleton batches. Order is preserved — we never reorder. This was largely already present in Durin's `_partition_tool_batches`; we added a documenting comment and an explicit test for the interleaved (read, write, read) case.

**Why we never globally reorder**: `[read_config, edit_config, read_config]` is semantically distinct from `[read_config, read_config, edit_config]`. The model expects read-after-write to see the post-edit state. Order preservation is the only correct default.

**Test coverage**: `tests/agent/test_runner_tool_execution.py::test_runner_serializes_mutation_between_reads`

### 2B — Reasoning-phase truncation recovery

**Problem observed**: Reasoning models (glm-5.1, o-series, Claude thinking) emit deliberation in a separate `reasoning_content` field that counts against `max_tokens` but doesn't appear in `content`. When the cap strikes mid-reasoning, we observe:
- `finish_reason == "length"`
- `content` is blank
- `reasoning_content` is non-empty (often very long, e.g. 20k+ chars)

The default `LENGTH_RECOVERY_PROMPT` ("continue exactly where you left off") asks the model to resume mid-thought without the cue that it should wrap up. The empty-content retry path is also wrong — it re-sends the same prompt.

**Fix**: detect this specific signature, append the partial `reasoning_content` to the assistant message (preserving the chain-of-thought), and inject `REASONING_TRUNCATION_PROMPT` — a cue asking the model to briefly conclude its reasoning and emit the final answer or tool calls.

**Test coverage**: `tests/agent/test_runner_tool_execution.py::test_reasoning_truncation_triggers_specialized_recovery`

**Lineage**: Surfaced by V9d data — we noticed `response_chars` and `tokens_output` diverged dramatically (sometimes 22×) and traced it to `reasoning_content` being separately counted. Suggested as a hardening item by Gemini.

---

## What 2026 industry evidence taught us about test-gen loops (May 2026)

When evaluating AlphaCodium-style flow engineering as a next direction, a closer look at 2026 industry evidence surfaced two facts that softened our initial enthusiasm:

1. **The AlphaCodium leaderboard has not been refreshed for frontier models** — GPT-4o is still its top entry, not GPT-5 / Claude 4.7 / Gemini 3.x. BACE (GECCO '26, arxiv 2603.28653) is the most recent published refinement but reports only its own numbers on LiveCodeBench v6; no independent third-party reproduction of test-gen loops on frontier models exists publicly.
2. **Production agents (Codex `/goal`, Claude Code) clearly use test-execution loops internally**, but none have published isolated numbers — only end-to-end SWE-bench Verified scores that bundle many techniques.

**Conclusion**: the pattern's expected value is real but **less publicly measurable in 2026 than its citation count suggested**. Moved off the immediate roadmap pending a benchmark where single-call frontier models fail ≥40-50% — Exercism at 90% pass rate gives no headroom to measure a delta. Also worth noting: a full AlphaCodium loop is *language-specific* (needs pytest / jest / cargo / go test / etc. per language), which is a maintenance burden we can't ignore.

---

## What the agent strategies catalog surfaced (May 2026)

Reviewing `04_agent_strategies_catalog.md` through a filter of "language-agnostic, production-validated, small scope" surfaced something we'd been overlooking: SWE-agent's central insight that **tool I/O quality matters more than loop quality** (NeurIPS 2024). Most of our 2025–2026 experiments worked on loop quality (deliberation, posture, plan tiers, phase prompts) — the SWE-agent quick-wins that are *general* are at the tool I/O boundary:

| Strategy | SWE-agent claim | Status in Durin (May 2026) |
|---|---|---|
| **Windowed file viewer** (N-line window + navigation commands) | "Doubles SWE-bench score vs raw bash" | `ReadFileTool` already accepts offset+limit (default 2000), returns truncation hint. **No telemetry on usage.** |
| **Capped search + structured summary** | Reduces context contamination | `GrepTool` already has `head_limit` (default 250), pagination notes. **No telemetry on usage.** |

**External validation, the other way**: OpenHands (SDK 2026) does **not** do windowed file reads — it relies on a Condenser/summarizer that operates on history *after* it grows large. Their open issue #12353 (Jan 2026) requests "Context Offloading for Large Tool Outputs" — meaning the community wants exactly what SWE-agent already does. SWE-agent's approach is the more proactive one.

**The gap is measurement, not implementation**: with 1M-context frontier models, the original "doubles SWE-bench" delta may have shrunk substantially. We don't know our actual numbers. The right next step is telemetry, not tuning.

**Phase 2 direction (in progress, May 2026)**: instrument `read_file` and `grep` with per-call JSONL events (params, output size, truncation flags, follow-up read patterns), collect over a real workload, then decide if the 2000-line and 250-result defaults need tightening (toward SWE-agent's 100 / 50) or are already correct for 1M-context models. **Tighten only with data, never speculatively** — the user's concern is real: lowering limits could silently remove information the LLM needs.

---

## Discarded: Role-based SOUL.md routing (V9e closure, May 2026)

**What it was**: a router that classifies the incoming goal by task type (implementer, debugger, refactorer, reviewer, generalist) and injects the matching SOUL.md fragment as the system prompt. Inspired by Aider's +33-41pp edit-format A/B, PartialOrderEval's +58pp, and Hermes' skill-loop speedup.

**Why it was tried**: V9 v1 (May 14) gave what looked like a +20pp signal for the `specific` SOUL on Exercism. If different SOULs differentially helped different task types, a router could sum the virtues — matched-role beats any single SOUL or no SOUL.

**What was learned (V9d → V9e)**:
- V9d revealed the +20pp signal was an artifact: `max_tokens=4096` truncated the verbose `specific` SOUL's output, depressing its pass rate. With the cap raised to the model's API max (131072), all three conditions converged.
- V9e ran 107 exercises × 3 conditions (none / specific / generic_agent) on glm-5.1, single-call, whole-file edits, pytest as ground truth.
- **Pass rates: 69.2% / 71.0% / 73.8%** — 4.6pp gap, inside the noise floor (±4.4pp std for N=107).
- **Divergence analysis**: 25 exercises diverged across the 3 conditions. They distribute uniformly across the 6 possible patterns (χ² = 1.78, df=5, p ≫ 0.05). Per-condition sign tests: p=0.41–0.68. **Indistinguishable from random model variance**.
- **Error types**: nearly identical across conditions (~28/30/25 AssertionError each, 1/1/2 setup errors). When a condition fails, it fails the same way the others would fail. No "specific generates code with subtle TypeErrors" or similar differentiation.
- **Fail-set Jaccard similarity**: 0.57–0.61 between condition pairs. Most failures are shared difficulty, not differentiation.
- **Anecdotal patterns** (`none` alone passes 4 lyrics/text-format exercises like beer-song / food-chain / proverb; `generic_agent` alone passes 5 class-structure exercises like grade-school / paasio / pov / satellite; `specific` alone passes 3 edge-case algorithms) are statistically indistinguishable from Bernoulli noise at this N.

**Why it was discarded**:
- No correctness signal beyond noise. A router needs a measurable differential effect to justify the infrastructure (classifier LLM call, fragment library, integration, evaluation harness).
- Frontier reasoning models (glm-5.1 with 131k completion budget) already deliberate internally — the system prompt's role in steering decisions is small compared to the model's own deliberation. This is consistent with the broader "context orchestration > cognitive manipulation" pivot.
- Confirming the anecdotal patterns rigorously would require ≥50 repeated trials per (exercise, condition) to lift signal above noise — high cost for low actionable upside.

**What survives as a real effect**:
- **Token efficiency**: SOUL ≠ ∅ reduces median output tokens 3–5× and reasoning chars 2.84× at identical correctness. Robust signal across V9d and V9e.
- The benefit comes from **any non-empty SOUL**, not from matching role-to-task. A single generic engineering SOUL captures the effect without a router.

**Action**: set a single generic-engineering SOUL as Durin's default in `ContextBuilder`. No router. No fragment library.

**Lesson**: with frontier reasoning models, system-prompt content has more leverage on **how verbosely the model reasons** than on **whether it reaches the right answer**. Efficiency-shaped prompts (concise role, focused rules) win even when correctness gains evaporate. Future prompt experiments should measure both axes — correctness AND efficiency — and not collapse them into a single "improvement" claim.

**Files**:
- Script: `scripts/hypothesis_test/run_experiment_v9e_complement.py`
- Results: `scripts/hypothesis_test/v9_runs/results_v9e_seed42.jsonl` (321 trials)
- Analysis log: `06_log_experiments.md` (V9e entry)

---

## Last updated: 2026-05-19

## Sprint B — Permission-as-data agent modes (May 2026)

### Context — why this is NOT a repeat of V7/V8

V7/V8's PlanHook was refuted: 0/24 hits, -2pp on scenario_3. The mechanism was **forced behavior via code** (a hook that interceptated the loop and required `verify` before `complete_goal`). It coupled mode to runtime logic.

Sprint B is the opposite design: **modes are data, not code**. The loop has no conditional logic about "what plan mode does" — it only filters the tool surface using a frozenset declared in the `AgentMode` dataclass. The model retains full agency within the filtered surface. If it chooses to act outside plan mode, it can; the only constraint is which tools are exposed.

Three external implementations validated this approach (`docs/archive/34_external_agents_review.md`):
- **OpenCode**: per-tool ruleset with wildcards; modes selected by agent record
- **OpenClaude (= Claude Code)**: enum-style modes with `prePlanMode` restore pattern
- **Hermes**: thread-local tool whitelist for bg-review fork

We borrowed the simplest viable subset: explicit `allowed: frozenset[str]` (no wildcards — Durin has ~15 tools, fnmatch is over-engineering), `pre_plan_mode` restore from OpenClaude, and slash-command activation from Claude Code's UX.

### What survives, what doesn't

Sprint B does NOT make a correctness claim. V9e closed the door on "system prompt routing improves correctness on frontier models" — the same applies to mode-based prompt suffixes. We are NOT building plan-mode hoping it will improve solve rates.

What it DOES do:
- Gives the user (Marcelo, daily-driver use case) a way to ask the agent to plan before executing — same UX as Claude Code's Shift+Tab, but universal across channels
- Channels off the V7/V8 plan-tier pattern in a way that doesn't repeat the refuted mistake (data, not code)
- Establishes the infrastructure for additional modes (debugger, reviewer) without further refactor — each new mode is ~5 LOC
- Provides the read-only filtering primitive that Phase 2 (memoria via Hermes-style background-review fork) will need

### Implementation honesty

- Three slash commands (`/plan`, `/build`, `/mode`) work in every channel that uses the shared `CommandRouter` — zero per-channel code required for dispatch.
- WebUI gets autocomplete for free via the existing `<SlashCommandPalette>` + `/api/commands` endpoint.
- CLI and Telegram get dispatch but not autocomplete out of the box — both are ~10 LOC additions and documented as "future improvements" in `architecture/README.md`. They aren't blockers for daily-driver use.
- The `exit_plan_mode` tool surfaces the plan but does NOT auto-restore the previous mode. The user must run `/build` explicitly. This avoids the model jumping into execution without human review and works channel-agnostic (no UI dialog).
- 51 new tests; full suite 3,153.

### Lesson

The "permission-as-data vs forced-behavior" distinction matters. V7/V8 failed because they encoded *what to do* in the loop. Sprint B succeeds because it only encodes *what's available* — the model decides what to do within the available set. The mechanism is cheaper, simpler, and easier to reason about. Future scaffolding ideas should be pushed through this filter first: are we adding a *constraint on the environment* (data), or are we adding *logic about behavior* (code)? The first generalizes; the second tends to fight the model.

### Course-correction: file-based plan storage (May 2026)

**What I almost shipped**: Sprint B's first cut of `exit_plan_mode` took the plan as a string argument and returned it as the tool result body — no disk write. I justified this with "it's overkill for multi-channel" and shipped it.

**Why I was wrong**: Marcelo flagged it. I had incorrectly coupled "file-based plan storage" with "UI permission dialog" because Claude Code does both together. They are orthogonal. File-based plan + slash-command approval works perfectly cross-channel.

The argument-string MVP lost on every operational dimension that matters for daily-driver use:
- No persistence across context compaction
- No edit-before-approve (user has to rephrase in a follow-up message)
- No multi-turn refinement (every turn regenerates the plan from scratch)
- No post-mortem review
- Worse token efficiency (plan lives in message history)

**Fix**: refactored `ExitPlanModeTool` to write the plan to `<workspace>/.durin/plans/plan_<timestamp>.md`. The path is returned in the tool result and stashed on `session.metadata[active_plan_path]`. When `/build` approves, `cmd_build` migrates the key to `approved_plan_path`, which the next turn's `build_messages` injects into the runtime-context block as a one-shot reminder. The model then `read_file`s the (possibly user-edited) plan and executes.

**Lesson** — captured in user-memory (`feedback_no_value_less_mvp.md`): when evaluating two designs, the simpler-to-implement one is only correct if it doesn't lose on operational ergonomics. For daily-driver-grade features, list the operational use cases first and pick the design that serves them; the LOC delta is the secondary factor. Don't ship MVPs without real value.

### Compaction survival + session-scoped plan files (May 2026)

After the file-based refactor, two more refinements landed:

1. **Plan files scoped per session**. The first iteration wrote all plans to a flat `.durin/plans/plan_<timestamp>.md`. Marcelo flagged: *"creo que es relevante usar en el nombre del file, el id de session y un id de plan"*. Updated to `.durin/plans/<session-slug>/plan_<timestamp>.md` — the session key is sanitized (filesystem-safe), and the timestamp acts as the plan id within the session subdirectory. Concurrent chats no longer collide, and `ls .durin/plans/<session>/` shows just that conversation's plan history.

2. **Plan content survives compaction**. The original handoff via `approved_plan_path` was one-shot in the runtime-context block — fine for short post-approval workflows, but the plan reminder vanished as soon as auto-compact archived the surrounding messages. We replicated Claude Code's `plan_file_reference` attachment pattern: `cmd_build` also stashes `executing_plan_path` (persistent), and `autocompact._archive` reads that path and splices the plan content into the summary text. The plan now keeps being re-surfaced through arbitrary compactions until a new `/plan` clears it (a new plan supersedes the prior).

The post-approval system reminder also now suggests `todo_write`, mirroring the Claude Code wording (*"Start with updating your todo list if applicable"*). This is prompt engineering, not enforcement — the model is free to ignore it, but it connects plan mode to the existing TodoWrite tool that already serves as the progress tracker.

**Lesson reinforced** ([[no-value-less-mvp]]): the first cut shipped the one-shot reminder and would have failed silently in long sessions. The second cut (compaction-survived) was 40 LOC of carry-over logic but is what makes the feature actually work end-to-end. Asking "does this hold up under realistic operational conditions?" before declaring done is the discipline I keep needing to apply.

## Session archive — ground-truth log for Phase 2 memory (May 2026)

### Why this exists

Until now, when autocompact archived messages, the originals were discarded. The only thing that survived was an LLM-generated narrative summary in `session.metadata["_last_summary"]` (and a copy in `history.jsonl` via `Consolidator.archive`). For day-to-day operation this is fine — the user reads summaries, the model gets summaries. **But for the future memory subsystem (Phase 2)**, narrative-only is not enough:

- Memory can't tell "user asked X" from "summary says user asked X" unless markers are explicit.
- Memory can't replay tool calls, see exact arguments, or correlate `plan_event` → execution → outcome.
- Post-mortem analysis of failed turns is impossible — the failure stack is gone.
- Patterns by tool name, file path, or argument shape are uncomputable.

Marcelo's framing: *"esto va ser alimento puro para el [sistema de memoria]"*.

### Design

Per-session append-only JSONL at `~/.cache/durin/archive/<session-slug>.jsonl`. Each line is `{"ts": float, "kind": str, "data": {...}}` with six kinds covering the full state landscape: `message` (verbatim, no LLM rewriting), `tool_call` (auto-extracted), `tool_result`, `summary` (clearly tagged with `source`), `plan_event` (enter/exit/approved/superseded), `mode_switch`.

Two pieces enable post-processing without ambiguity:

1. **Verbatim message persistence**. The original dict — role, content, tool_calls, reasoning_content — goes to the archive untouched. This is the byte-for-byte record of what the model saw.

2. **Summary marker block**. Centralized in `format_summary_block()`. Wraps any LLM-generated summary in `=== ARCHIVED SUMMARY (source, last active TS, N msgs condensed) === / === END ARCHIVED SUMMARY ===`. When future memory (or any reader) processes prior turns, `is_summary_block()` answers "is this text narrative or real" in one line.

### Coverage today

- ✅ `autocompact._archive` writes each archived message + the summary
- ✅ `cmd_plan`, `cmd_build`, `cmd_mode` slash commands write `plan_event` + `mode_switch`
- ✅ `EnterPlanModeTool`, `ExitPlanModeTool` LLM tools write the same when called
- ✅ `format_summary_block` used by autocompact's `_format_summary`
- ⏸ `microcompact` and `snip` in the runner — deferred (investigate whether they discard data first)

### What this is NOT

- NOT enforcement, NOT cognitive scaffolding, NOT prompt engineering. Pure persistence.
- NOT a replacement for the existing `history.jsonl` (which keeps LLM summaries for re-injection at session reload). It's complementary: archive is structured for post-processing, history is structured for in-context replay.
- NOT a memory system yet. It's the **substrate** Phase 2 will read from. Replay APIs, indexing, promotion logic come later.

### Lesson

When designing for a future subsystem you can already name (Phase 2 memory), capture data in the most flexible form possible **now**. The cost of writing one JSONL line per event today is negligible; the cost of reconstructing tool_calls from narrative summaries six months from now is unbounded. The discipline is: if a piece of information will ever be useful, persist it structured. The previous design discarded everything except the narrative — that was easy to ship but eliminated the optionality the memory work depends on.

## Pivot: session immutable + per-session meta file (May 2026)

The work to add a "session archive" turned out to be founded on a misunderstanding of how the existing system handles compaction. Two clarifications drove the redesign:

1. **The LLM doesn't see the full `session.messages`** — it sees `messages[last_consolidated:]` capped at `max_messages` plus the latest summary. The cursor advances when `Consolidator.maybe_consolidate_by_tokens` decides the prompt exceeds budget. The raw messages stay on disk forever.
2. **The ONLY mechanism that destroyed disk state was `AutoCompact._archive`** — and it required `idleCompactAfterMinutes > 0` (default off). Most users never trip it.

So the "archive" we'd built was duplicating `session.json` for a case (`TTL > 0`) that almost never fires, while creating non-trivial wiring overhead (ContextVar binding, event extraction, per-call writes).

### What we removed

- `durin/session/archive.py` (module + ContextVar pattern + bulk helpers + summary marker centralization)
- Archive `ContextVar` wiring in `AgentLoop._dispatch_message`
- Archive writes in `autocompact`, plan tools, and slash commands
- The archive test file
- `durin/agent/autocompact.py` (entire module — `_archive`, `check_expired`, `prepare_session`, `_summaries` cache)
- `session_ttl_minutes` / `idleCompactAfterMinutes` / `sessionTtlMinutes` config field
- `AgentLoop.auto_compact` attribute, two `prepare_session` callers, and the periodic `check_expired` invocation in the main loop
- Two test files: `test_auto_compact.py`, `test_autocompact_unit.py`

### What we kept

`Consolidator.maybe_consolidate_by_tokens` continues advancing `session.last_consolidated` each turn when the budget would be exceeded — that's the legitimate context governance mechanism, and it works without touching `session.messages` on disk. The summary it produces lives in `session.metadata["_last_summary"]` and also in `history.jsonl`.

The summary marker wrapping (`=== ARCHIVED SUMMARY (consolidator, last active <ts>) === / === END ARCHIVED SUMMARY ===`) survived as a small static method `AgentLoop._format_pending_summary` — the marker convention is still valuable so any reader can distinguish summary text from real conversation.

### What we built instead

`durin/session/session_meta.py` — **one `.meta.json` per session**, sitting beside the existing `.jsonl`. Holds a chronological list of lifecycle events with a `type` discriminator for extensibility:

```json
{
  "session_key": "websocket:chat42",
  "events": [
    {
      "type": "plan",
      "id": "plan_20260519_143022_123",
      "title": "Refactor authentication module to use OAuth",
      "plan_path": ".durin/plans/.../plan_X.md",
      "created_at": "...",
      "approved_at": "...",
      "closed_at": null,
      "msg_index": { "approved": 240, "closed": null },
      "outcome": "executing"
    }
  ]
}
```

The `msg_index` field is the cross-reference back to `session.messages` — it lets the future memory subsystem slice raw messages by event scope without parsing.

Wired writes:
- `ExitPlanModeTool` appends a fresh plan event with title extracted from the plan markdown's first heading
- `/build` transitions the active plan to `outcome=executing` and records `msg_index.approved`
- `/plan` (new) closes the prior executing plan with `outcome=superseded` and `msg_index.closed`

Atomic writes (tmp + `os.replace`), best-effort failure handling (never breaks a tool call), and extensible to future event types without schema changes.

### Lesson

Premature design based on faulty mental model. I built the archive system to "preserve discarded data" without verifying the data was actually discarded — `session.messages` is in fact preserved by default. The user's pushback (*"si session.json queda para siempre, archive es redundante"*) forced re-verification, which surfaced that TTL itself was the only thing destroying data, and TTL existed for a niche case (server multi-session) that wasn't even active by default.

**Discipline**: before adding persistence infrastructure, verify what the existing system already persists. Don't build "memory" before checking whether the data is already on disk in some form.

Together with the previous "no-value MVPs" lesson, this is the same pattern from a different angle: **understand the current state first, design after** — not "design from intuition, then check the state at the end".

## Tools roadmap consolidation (May 2026)

After the archive/autocompact pivot landed (sessions are now immutable, meta sidecar replaces archive), we revisited what tools to build next.

### The exercise

Took the comparative review from `archive/34_external_agents_review.md`, surfaced what each of the 4 reference agents (OpenHands, Hermes, OpenCode, OpenClaude) exposes as tools, then filtered through Marcelo's daily-driver priorities. Result: a 12-item ordered list of tools to add, captured in `roadmap.md` §"Tools roadmap".

### What Marcelo flagged as priority

- **Vision tool** (delegate to a vision-capable model from preset) — gateway to multimodal without forcing the primary model to be multimodal
- **Document extraction** (PDF/Office/OCR enriched) — practical for daily-driver document workflows
- **Browser** — research workflows
- **TodoWrite** — universally adopted; pairs with plan mode
- **Subagent lifecycle expansion** — today only `spawn`; needs list/get/get_output/stop/status for daily-driver long-running work
- **Cron + Sleep + Monitor + RemoteTrigger** — async orchestration bundle, mostly trivial

### What I'd missed in my initial review

Returning to the 4 repos with the right filter surfaced:
- `AskUserQuestion` (OpenClaude) / `clarify` (Hermes) — interactive prompt to user mid-turn
- `session_search` (Hermes) — search within own session.messages
- `BriefTool` (OpenClaude) — compact sub-task descriptions before delegate
- `WorkflowTool` (OpenClaude) — named multi-step flows
- `kanban_*` (Hermes, 8 tools) — more structured than TodoWrite

The first two made it onto the list (high value, low complexity). The rest were rejected for scope reasons.

### Explicit rejections (with reason)

| Rejected | Why |
|---|---|
| `apply_patch` (Codex envelope) | Only useful with OpenAI-family models, we use glm-5.1 |
| LSP-as-tool | Per-language maintenance burden refuted in doc 07 |
| Worktree (git) | No multi-branch workflows in our use cases |
| `kanban_*` | Over-structured vs TodoWrite simple, no concrete demand |
| `TeamCreate/Delete` (swarms) | Over-engineering without case |
| `mixture_of_agents` | N× cost without demonstrated value |
| Channel-specific integrations (Discord/Feishu/HomeAssistant/etc.) | Channels system already exists, one-off integrations don't scale |

### Decision rule going forward

Tools added to the roadmap must meet either:
- **(a)** Language-agnostic AND adopted by ≥2 of the 4 reference agents
- **(b)** Explicitly flagged by the user for daily-driver use

This rule formalizes what V9e taught us about new components: industrial precedent OR concrete user demand. "It would be nice" without either is not enough.

## Plan mode hardening — OpenClaude pattern adoption (May 2026)

End-to-end testing of plan mode revealed three gaps that allowed the model to bypass the contract: (1) the model could "forget" plan mode mid-session and try to modify files, (2) it would delegate modifications to a subagent expecting the subagent to be unrestricted, (3) `/build` left the agent idle until the user typed something else.

We investigated how Claude Code (via OpenClaude source) avoids these and adopted three of its mechanisms.

### Per-turn plan-mode runtime reminder

The system-prompt suffix alone is insufficient — even when placed near the top, frontier models give it less weight than fresh messages. OpenClaude injects a `plan_mode` attachment as a user-meta message **every turn the session is in plan mode**, with language that explicitly says *"This supersedes any other instructions you have received"*.

We replicate this via `plan_mode_runtime_lines(metadata)` in `durin/agent/agent_mode.py`, called from `ContextBuilder.build_messages` and folded into the runtime-context block alongside `goal_state_runtime_lines`. Same pattern, different signal. The reminder repeats every turn so the model sees it fresh near the current user message.

### Subagent inherits parent mode

The earlier implementation forced subagents into `EXPLORE_MODE` regardless of parent mode. That worked as a safety net but it was incoherent: the model would still try to `spawn` for modification work, then receive a denial chain via the subagent.

OpenClaude's pattern (see `agentToolUtils.ts:90`) is different: the subagent inherits the parent's permission mode. If parent is in plan, subagent is also in plan — both restricted to read-only + `exit_plan_mode`. The model understands delegation does not escape the mode and stops trying to work around it.

Implementation: `SubagentManager` accepts a `sessions: SessionManager` reference; the `mode_provider` passed to the subagent's `AgentRunSpec` reads the parent's `session.metadata["agent_mode"]` each iteration. Falls back to `EXPLORE_MODE` when no sessions handle is available (defensive).

### /build wakes the agent

`cmd_build` previously transitioned the mode and stashed the approved plan path, then returned an `OutboundMessage` informational reply. The bus saw no new inbound message, so the runner stayed idle until the user typed something — often the user had to send "y?" or "ok?" to wake it.

Fix: after approving the plan, `cmd_build` publishes a synthetic `InboundMessage` with `content="Proceed with the approved plan."` to the bus. The runner consumes it, sees `approved_plan_path` in the runtime context (one-shot reminder), reads the plan file, executes. Mirrors the `/plan <task>` re-publish pattern we already had.

### What this did NOT fix

The CLI streaming output is still mixed visually with the `You:` prompt (prompt_toolkit + Rich coordination issue). Mitigated partially with `run_in_terminal`, but a full fix requires suspending the input prompt during agent turns — a CLI refactor outside this pass. WebUI is unaffected.

### Lesson

For mechanisms that depend on the model respecting a contract, **per-turn reinforcement beats one-time prompt placement**. System-prompt suffixes get buried by skills lists / memory / history. Attachment-style reminders next to the current user message stay visible. OpenClaude's "supersedes any other instructions" phrasing is also load-bearing — frontier models otherwise weight earlier prompt content as authoritative even when a later instruction contradicts.

Pattern more broadly: when adding a behavior contract to the loop, ask whether the model will see it fresh on every turn it matters, or whether it has to "remember" something said earlier in a long context. The first is reliable; the second is wishful.

## CLI background-work indicator — `_block_input_until_response` (May 2026)

### Symptom

After `/build` (and `/plan <task>`), the CLI returned to the user input prompt while the agent was still processing the synthetic trigger that the slash command had published to the bus. The user saw the slash command's confirmation message, then a blinking prompt, with no signal that work was in flight. Streamed output eventually appeared but collided visually with the input line.

### Fix (minimal indicator pass)

`cmd_plan` and `cmd_build` now set `metadata["_block_input_until_response"] = True` on their `OutboundMessage` whenever they publish a synthetic follow-up to the bus. The interactive CLI loop honors this flag: after printing the slash response (pausing the spinner briefly via `renderer.pause()`), it clears `turn_done` and waits again, instead of returning to the input prompt. The renderer's `ThinkingSpinner` keeps running across that boundary, so the user sees `durin is thinking…` until the follow-up stream begins. Stream deltas land on the existing renderer and replace the spinner as usual.

The flag is one-shot, opt-in metadata — slash commands that don't schedule follow-up work (mode echo, plain `/plan`) leave it unset and the CLI behaves exactly as before.

### What this did NOT fix

The deeper prompt_toolkit + Rich coordination bug is still here: while the renderer's `Live` is updating, the input line is technically still active beneath it. The visible mess (raw ANSI when pasted, occasional overlap) requires actually suspending the input prompt for the duration of the agent turn. Slated for the next pass.

### Lesson

CLI UX gaps caused by **scheduled-but-uncommunicated background work** are best fixed at the *contract boundary* (the slash command's output metadata), not by guessing inside the CLI consumer. The flag travels with the message that scheduled the work, so it's local to the cause. Avoids the alternative of polling/inferring "is there pending work?" from outside, which never resolves cleanly with a streaming bus.

## Plan re-display contract (May 2026)

### Symptom

After the model called `exit_plan_mode` with a full plan, the user only saw a one-line teaser (e.g. *"Sección de noticias con 5 items. Ejecutá /build."*) and never the actual plan content. They had to push back ("pero nunca me mostraste el plan") before the model would re-display it. Approving `/build` blind on a one-line summary is unsafe.

### Fix

Both the PLAN_MODE prompt suffix and the `exit_plan_mode` tool result now explicitly tell the model: *"The user has NOT seen the plan yet — saving to disk is internal bookkeeping. Your next assistant message MUST present the full plan content."* The tool result wraps the plan in a `--- Plan content (present this in your reply) ---` block so it's right there in the model's context when it composes the next message.

Claude Code's UX cheats here: when ExitPlanMode is called, the *system* surfaces the plan in a dialog regardless of what the model says. Durin doesn't have that channel-side rendering yet (the CLI doesn't peek into tool results), so the contract has to be enforced via prompt. If the model regresses we'll need to teach the CLI to detect `exit_plan_mode` tool results and render the plan content itself — at which point the prompt instruction becomes belt + suspenders.

### Lesson

Tool results that include user-visible content need an explicit "render this to the user" instruction. Otherwise, models default to summarizing the tool call ("I saved the plan") rather than relaying its content. The same pattern likely applies to any tool that produces an artifact the user must see to approve — `present_artifact(content, then_ask=...)` is more honest than relying on "the user will see it" implicit semantics.

## CLI streaming: static indicator instead of animated spinner (May 2026)

### Symptom

During streamed responses (especially the `/build` follow-up turn), spinner frames appeared as interleaved scrollback lines instead of refreshing in place:

```
?[2K?[32m⠴?[0m ?[2mdurin is thinking…?[0m
?[2K?[32m⠦?[0m ?[2mdurin is thinking…?[0m
?[2K?[32m⠹?[0m ?[2mdurin is thinking…?[0mY: ¡
?[2K?[32m⠼?[0m ?[2mdurin is thinking…?[0mn de noticias…
```

The escape sequences `\x1b[2K` (clear line) followed by `\x1b[32m⠼\x1b[0m` (green braille dot) are the spinner. They were leaking into the conversation as literal text mixed with the stream chunks and the `You:` input prompt.

### Root cause

Rich's `Console.status` runs its braille animation on a background **refresh thread** that wakes every ~80ms and prints the next frame. The renderer's `_stop_spinner` path was already cleaning up around stream deltas and reasoning lines, but the animation thread could still emit one or two frames between the `stop()` signal and the actual thread shutdown. Those frames landed *after* a print of the streaming text (which had appended a `\n`), so the in-place semantics (`\r` overwrite) were lost — the frames stuck as historical lines.

This was a race between the spinner thread and the synchronous print path. It got dramatically worse the more chatty the model was on a streamed turn (lots of reasoning chunks + lots of stream deltas → lots of small write windows → lots of stray frames).

### Fix

Replace the animated spinner with a **synchronous static indicator** — same class name (`ThinkingSpinner`) for API compatibility, but the internals just write a dim line on `__enter__`, clear it with `\r\x1b[2K` on `__exit__`, and `pause()` is now a clear + restore context manager. No background thread, no animation, no race. Trade-off: the indicator no longer visibly "ticks" (you see `⏳ durin is thinking…` instead of a spinning braille dot). The daily UX is noticeably calmer.

If we ever want the animation back, the right approach is a single Rich `Live` group with `auto_refresh=False` that re-renders the spinner + streamed buffer in lockstep on every delta — one render pipeline, no competing writers. Not worth the complexity right now.

### Lesson

In TTY UIs that mix synchronous prints with animated indicators, **two render pipelines** (a streaming buffer + a spinner thread) always race for the cursor. The race is unfixable as long as both pipelines write directly to stdout — you can stagger them, you can paper over it with `stop()`/`start()`, but you cannot make it correct. Either unify them (Live group) or drop animation (static indicator). Doing neither produces exactly the kind of visible-escape-codes-in-the-transcript bug we just removed.

## TodoWrite tool — item #1 of the tools roadmap (May 2026)

### What shipped

`todo_write` tool registered as a core tool ([durin/agent/tools/todos.py](../durin/agent/tools/todos.py)). Backed by session metadata helpers in [durin/session/todo_state.py](../durin/session/todo_state.py). Wired into the runtime-context block of [durin/agent/context.py](../durin/agent/context.py) so the current checklist survives compaction and is visible to the model on every turn.

Schema is the flat list pattern adopted by all four reference agents (OpenHands, Hermes, OpenCode, OpenClaude): each item is `{content, status, activeForm}` where status is `pending` / `in_progress` / `completed`. Each tool call REPLACES the entire list — there is no add/update/delete triplet. The tool result echoes the rendered markdown checklist so the model has the text to paste back at the user in its next assistant message.

The tool is allowed in plan mode (storage write is in-memory metadata, not workspace state) so the model can maintain a checklist while investigating.

### Why one tool, not three

Three CRUD tools (`TodoAdd` / `TodoUpdate` / `TodoComplete`) create three classes of bugs that the single-replacement tool does not:

1. **Stale partial updates**: model issues `TodoComplete(id=3)` but item 3 was renumbered when a prior `TodoAdd` happened mid-turn. The single tool has no IDs and no off-by-one risk.
2. **Half-applied transitions**: model marks an item complete but forgets to mark the next one in_progress. The single tool requires sending the entire list, which forces "what is everything supposed to look like right now" thinking rather than diff thinking.
3. **Runtime-context drift**: each CRUD verb needs its own echo logic; one replacement tool means one place to render state.

Trade-off: marginally more tokens per call (the model retypes completed items). With items capped at 50 and content/activeForm at 400 chars each, that is bounded — and it's the same cost the four reference agents pay.

### Soft contract in code, hard contract in prompt

Two pieces of the contract are enforced server-side:

- Items missing `content` or with invalid `status` are silently dropped during `parse_todos`. The model that produces garbage still gets a useful tool result back (just truncated to the valid items).
- If multiple items are marked `in_progress`, the first keeps the status and the rest are demoted to `pending`. The tool flags this in its result so the model notices. Rejection would have been simpler but a 12B-class model that screwed this up once would likely keep screwing it up — coercion + visible nudge is the more cooperative path.

Everything else (use exactly one in_progress at a time, mark complete the moment work finishes, skip the tool for one-step asks) lives in the tool description and is the model's responsibility.

### Lesson

Echoing tool state back into the runtime-context block on every turn is what makes session metadata feel like "memory" instead of "stale write". The state is one place (metadata), the renderer is one function (`*_runtime_lines`), the model sees a fresh restatement each turn. Same pattern as `goal_state_runtime_lines` and `plan_mode_runtime_lines` — generalize this to every state-tracking tool we add.

## Sleep tool — item #2 of the tools roadmap (May 2026)

### What shipped

`sleep` tool ([durin/agent/tools/sleep.py](../durin/agent/tools/sleep.py)). Single parameter `seconds: number` with optional `reason: string`. Blocks the current turn via `asyncio.sleep`, bounded between 0 and 300 seconds. Verified end-to-end against the live agent: requested 2.0s, actual elapsed 2.001s, telemetry emitted both `sleep.start` and `sleep.end` events.

### Design decisions

1. **Cap at 300s, not 60s**: real polling use cases (waiting for a build, a deploy, a remote queue to drain) want minutes, not seconds. 60s would force the model into a sleep-loop pattern that triples LLM call count for no benefit.
2. **Cap at 300s, not unbounded**: a longer wait belongs in `cron` (schedule a future re-invocation) rather than blocking now. Blocking the turn holds the LLM streaming connection open and consumes the per-turn wall-clock budget. The cap also prevents prompt-injection style misuse where a hostile tool output could persuade the agent to sleep indefinitely.
3. **Clamp over-asks instead of erroring**: if the model asks for 600s, the tool clamps to 300s and reports `(Requested 600s, clamped to the 300s ceiling — use cron for longer waits.)`. Erroring would push the model into a retry loop; clamping plus a clear nudge teaches it to switch to `cron` next time.
4. **Allowed in every mode** (plan, explore, build): sleep does not touch the workspace or any session state beyond emitting telemetry. Plan-mode read-only invariants still hold.
5. **No "reason required"**: making `reason` mandatory inflates token usage on every call for a field that is only ever read in post-hoc telemetry analysis. Optional + capped at 200 chars is the right balance.

### Telemetry

Two events per call:

```json
{"type":"sleep.start","data":{"requested_s":600,"actual_s":300,"clamped":true,"reason":"polling build"}}
{"type":"sleep.end","data":{"elapsed_s":300.01,"reason":"polling build"}}
```

A third event — `sleep.cancelled` — fires if the turn is interrupted mid-sleep (KeyboardInterrupt or cancellation propagation). This lets us tell "sleep finished normally" from "user killed the agent" in retrospect.

### Lesson

For tools that have a "do nothing for N seconds" semantic, the surface area is genuinely tiny — one parameter, one bound, one optional reason — but the temptation is to over-engineer (priority levels, conditional waits, jitter, "wake on event"). Resist. If the use case is "wait then check again", that's `sleep` + `cron`. If it's "wait for a specific event", that's a different tool (`monitor`, `subagent_wait`, etc.). Keep `sleep` boring.

## Tool-call meta events — pointers, not duplicates (May 2026)

### Symptom that triggered the work

Only 6 of 20 tools emitted any telemetry (`read_file`, `edit_file`, `grep`, `repo_overview`, `shell`, `plan_mode`). Tools like `web_search`, `spawn`, `cron`, `todo_write`, `long_task` were dark — you could not answer "how many times was each tool used this session?" from telemetry. The session meta file ([durin/session/session_meta.py](../durin/session/session_meta.py)) already had `msg_index` semantics for plan-event transitions, but tool calls were not represented at all.

### What shipped

A new `type=tool_call` event in the session meta timeline. One event per tool invocation, written when the assistant message that emitted the call is persisted by `_save_turn`. Schema:

```json
{
  "type": "tool_call",
  "id": "call_abc123",          // LLM-assigned tool_call_id, primary key
  "name": "read_file",
  "outcome": "ok" | "error",
  "msg_index": 17,              // index into session.messages
  "duration_ms": 142.3,         // wall-clock spent inside tool.execute
  "error": "<200 char excerpt>",// present only when outcome=error
  "recorded_at": "2026-05-19T..."  // auto-added
}
```

Implementation:

1. [durin/session/session_meta.py](../durin/session/session_meta.py) — added `make_tool_call_event(...)` and `append_events_batch(...)` (one read-modify-write for N events to keep parallel tool calls cheap).
2. [durin/agent/runner.py](../durin/agent/runner.py) — new `_run_tool_timed` wrapper measures wall time around each tool call and stamps `tool_call_id` + `duration_ms` onto the existing `tool_events` list. The legacy `{name, status, detail}` fields stay intact for backwards compatibility.
3. [durin/agent/loop.py](../durin/agent/loop.py) — `_run_agent_loop` now returns `tool_events` as a sixth tuple element; `_save_turn` accepts it, indexes by `tool_call_id`, and writes one meta event per persisted assistant-message tool call. Best-effort: wrapped in `suppress(Exception)` so a meta-file write failure never breaks the agent.

### Why this design

Three principles we kept reaffirming during implementation:

1. **Pointers, not duplicates.** The full args + result of a tool call already live in `session.messages`. The meta event just records "name + outcome + where to look" so the memory subsystem can walk a timeline without parsing the full message log. Cheap, lossy, and indexed by `msg_index` for correlation.

2. **The contract boundary is `_save_turn`, not the tool.** Earlier sketches had each tool emit its own meta event. That would have meant: (a) duplicating session-key resolution in 20 tools, (b) chicken-and-egg problem with `msg_index` (the assistant message hasn't been persisted yet at the moment the tool runs), (c) silent drift between tools that remembered to emit and those that didn't. Centralizing in `_save_turn` makes coverage automatic — every tool gets a meta event without modifying the tool.

3. **Parallel calls share msg_index, differ by id.** When an assistant message issues two tool calls (e.g. `read_file` and `grep` concurrently), the runner returns two events with the same eventual `msg_index` but distinct `tool_call_id`s. The schema reflects this: `id` is the primary key, `msg_index` is the timeline pointer.

### What it unlocks

- "How many times did `read_file` get called this session?" → `jq '[.events[] | select(.type == "tool_call" and .name == "read_file")] | length' meta.json`
- "Which tool calls failed?" → `jq '.events[] | select(.outcome == "error")' meta.json`
- "Show me the assistant message that triggered tool call X" → look up `id == X`, jump to `session.messages[msg_index]`.
- Foundation for Phase 2 memory: the meta file is the durable, compaction-safe timeline of significant actions in a session. Plan events and tool-call events are now first-class citizens of that timeline; future event types (review, deliberation, etc.) drop into the same schema.

### E2E verification

Ran a live agent turn with `sleep` (0.5s) followed by `todo_write`. The resulting meta file had two events, one per tool, pointing to `msg_index=1` and `msg_index=3` respectively — both verified to be the assistant messages that emitted the calls. `duration_ms` for `sleep` was 501.7 (matching the requested 500ms).

### Lesson

When you have a persistence layer dedicated to "significant lifecycle events" (the meta file) and you start asking "should this go to telemetry or to meta?", the dividing line is: **telemetry is for analytics across sessions; meta is for the timeline of a single session**. The same data can live in both (cheap), but the meta-side is what lets memory walk one session deeply. Tool calls deserve to be in meta even when they look like "just a metric" — because the metric only gets interesting once you can pivot from "X happened" to "X happened *at this point* in the conversation".

## AskUserQuestion — item #3 of the tools roadmap (May 2026)

### What shipped

`ask_user_question` tool ([durin/agent/tools/ask_user.py](../durin/agent/tools/ask_user.py)). Parameters: `question: string` (required), `options: list[string]` (optional, 2-6 items). Records the question on `session.metadata["pending_question"]` with a fresh `question_id` + the option list, emits an `ask_user.question_asked` telemetry event, and returns a tool result that explicitly tells the model to "YIELD TO USER — present this question as your next assistant message and stop, do not call more tools".

Allowed in every agent mode (plan, explore, build) — the tool only touches session metadata, never the workspace.

### Why V1 yields instead of blocking

Two implementation strategies were on the table:

1. **Synchronous in-turn pause** — tool publishes the question to the bus, registers an `asyncio.Future`, awaits it; the bus intercepts the next inbound message for that session and completes the Future, the tool returns the user's text as its tool result. Same turn continues.
2. **Yield-and-resume** (V1) — tool returns immediately telling the model to present the question and stop. The model's assistant message contains the question, the turn ends, the user's next message naturally becomes the answer in a new turn, the model sees the full context (assistant asked → user replied) and continues.

V1 ships zero new bus plumbing. The cost: a turn boundary between "agent asks" and "agent receives". For a frontier model this is invisible — the new turn sees the prior tool call and the new user message and reasons over both. For UI it's identical (the user sees a question, types an answer, the agent continues).

The synchronous version remains a viable upgrade path that does not change the tool's public schema: same `question` + `options`, same `pending_question` metadata key. If we hit a use case where the turn boundary actually matters (multi-question chains where the agent wants to bundle multiple clarifications in one turn), we can swap the implementation without touching the calling model's prompts.

### The "yield" instruction is doing real work

We deliberated whether the tool offers value over "just have the model type the question". The verdict: yes, because of the **explicit yield**. Without the tool, the model often keeps guessing parameters, calls more tools speculatively, or even fabricates answers it can't have. The tool result is an unambiguous "stop here" signal — same lever we used in `exit_plan_mode`. Plus:

- `pending_question` on session metadata is a hook for UI affordances. CLI ignores it today; a future WebUI render path can show a clickable option list when present.
- The `ask_user.question_asked` telemetry event marks moments where the agent couldn't proceed without input — useful signal when tuning prompts.

### E2E verification

Ran an agent turn that called `ask_user_question("Which framework?", options=["React","Vue","Svelte"])`. Confirmed:

- The tool result told the model to yield; the model presented the question + options as bullet list and stopped (no further tool calls).
- `session.metadata["pending_question"]` got the full payload (question, options, question_id).
- The auto-generated meta event recorded the call at the correct `msg_index`.

### Lesson

When a tool's value is mostly semantic (forcing a control-flow shift rather than computing a result), the schema can stay small but the result text must do the heavy lifting. "Return data" tools can be terse; "yield" tools need to spell out the contract — what the model should write next, what it should NOT do, where the user's reply will arrive. We saw the same pattern with `exit_plan_mode` and the plan-display contract — making the model's next move explicit in the tool result avoids drifting into hallucinated continuations.

## session_search — item #4 of the tools roadmap (May 2026)

### What shipped

`session_search` tool ([durin/agent/tools/session_search.py](../durin/agent/tools/session_search.py)). Searches the current session's in-memory `session.messages` list for a keyword or regex and returns matches as `[msg_index] role: snippet`. Parameters:

- `query: string` (required) — substring (default) or regex
- `regex: bool` — opt into regex; invalid patterns surface as a clear tool error
- `case_sensitive: bool` — default false (case-insensitive)
- `role: "user" | "assistant" | "tool" | "system"` — restrict to one role
- `max_results: int 1..100` — default 20, returns the **last** N matches when there are more (recency bias matches the most common "what did I see recently?" use case)
- `snippet_chars: int 50..500` — width of the context window around each match, default 200

Allowed in every agent mode (read-only).

### Why this exists

Long sessions accumulate hundreds of messages. The model's working context (LLM history window) only carries the most recent slice; everything else is on disk in the jsonl. Without a search affordance, the model has only two options when it needs to recall something specific from earlier:

1. Call `read_file` on the session jsonl — works, but the file is large and the model burns tokens scanning it
2. Hallucinate / re-ask the user — bad UX

`session_search` is the obvious middle path: keyword/regex lookup against the same in-memory list the LLM history is built from, returning compact `[msg_index] role: snippet` results that fit cheaply in the next tool result. The model can chain a follow-up read against the meta timeline if it needs the full message at `msg_index`.

### Design notes worth keeping

- **Live messages, not disk**. The tool reads `session.messages` (the in-memory list), not `<session>.jsonl`. The in-memory list is the source of truth that the LLM history-builder also uses, so search results stay consistent with what the model has seen. Reading from disk could lag by a turn and is unnecessary indirection.
- **Tail bias on overflow**. When matches exceed `max_results`, we keep the **last** N chronologically. The earlier-in-session matches are usually less relevant ("what was the original ask?") than recent ones ("what error did I just see?"). If the model needs older matches it can narrow the query.
- **Snippet via single-pass regex windowing**. A `re.search` per message + a `_make_snippet` helper that centers the window on the match position, collapses whitespace, and ellipses both ends. No fancy tokenizer; the goal is readable single-line excerpts that fit alongside the msg_index.
- **Structured content handled**. Tool messages with `content` as a list of blocks (text, image_url, etc.) get their `text` fields concatenated before searching. The model would have seen this content rendered the same way during the original turn, so search consistency holds.
- **Read-only with no new exposure surface**. The tool only returns content the model has already produced or seen. No new data-leak vector vs. the existing history-rendering path.

### E2E verification

Seeded a session with "my favorite color is electric blue, and the project codename is Mongoose-7" in turn 1. In turn 2, asked the agent to use `session_search` to recall the codename. The agent invoked the tool with `query="codename"`, got back a match at `msg_index=0` (the seed user message), and reported "Your project codename is Mongoose-7" without re-reading the whole session.

### Lesson

For tools that operate on session-local state, the right substrate is almost always the in-memory representation, not the persisted file. The in-memory copy is the canonical source the LLM consumes; reading the file introduces consistency lag and indirection without any benefit for tools that are scoped to the current turn. Save the on-disk path for tools that operate across sessions (memory subsystem, cross-conversation indexing) — there it's the right answer.

## Subagent lifecycle tools — item #5 of the tools roadmap (May 2026)

### What shipped

Four new tools in [durin/agent/tools/subagent_lifecycle.py](../durin/agent/tools/subagent_lifecycle.py):

- `subagent_list` — task_id, label, state, iteration, age, tool-call count for every subagent the current session has spawned (running + retained history)
- `subagent_status(task_id)` — detailed snapshot for one subagent (phase, iteration, recent tool calls, usage, error)
- `subagent_stop(task_id)` — best-effort cancel; returns `stopped` / `not_running` / `unknown`
- `subagent_output(task_id)` — final or partial output of a subagent (long results truncated at 4000 chars with a pointer back to the announce)

All allowed in plan mode — they only touch the manager's in-memory state, not the workspace.

Companion changes in [durin/agent/subagent.py](../durin/agent/subagent.py):

- `SubagentStatus` gained `session_key`, `final_content`, `ended_at`
- Done-callback no longer pops `_task_statuses` or `_session_tasks`; instead `_remember_finished` LRU-trims at `_max_status_history` (default 100). Statuses stick around so `subagent_output` can serve completed tasks turns later.
- New public methods on `SubagentManager`: `list_for_session`, `get_status_for`, `stop_task`, `get_output_for`. All accept `session_key` for ownership checks.

### Session-scope security boundary

Every lifecycle method takes `(task_id, session_key)`. Cross-session lookups return the same `"unknown"` response as a genuinely nonexistent id — we never confirm whether a task exists in another session. A model that guesses or fishes for ids cannot leak across conversations. The check happens inside `SubagentManager`, not in each tool, so wrappers can't accidentally bypass it.

### LRU retention is the load-bearing design choice

The original `_cleanup` popped both `_task_statuses` and `_session_tasks` on completion. That left `subagent_output` with nothing to serve once the asyncio.Task finished — and the asyncio.Task often finishes during a different tool call than the one that next wants to read the result. By retaining status entries (capped at 100) and only trimming when over the cap, we get a usable "ask later" affordance without unbounded memory growth.

The trim order is dict-insertion order — oldest first — which matches FIFO for our purposes (a session that spawns 200 subagents drops the earliest 100). The session index (`_session_tasks`) is updated in lockstep so `list_for_session` stays consistent.

### MRO gotcha (caught during E2E)

First implementation put `create` and `enabled` on a `_SubagentToolBase` mixin and declared each concrete tool as `class XxxTool(Tool, _SubagentToolBase)`. The MRO walks `Tool` first, so `Tool.create` (the default `cls()` constructor) ran instead of the mixin's overload — and crashed with "missing 1 required positional argument: manager". The unit tests passed because they instantiated tools directly with `Tool(manager=...)`, bypassing `create`. The bug only surfaced under the live agent loader path.

Fix: define `create` and `enabled` on each concrete class. Same pattern as `LongTaskTool`/`CompleteGoalTool`. The base class still owns shared state and helpers (`set_context`, `_session_key`), just not the constructor hooks.

Lesson recorded as a comment at the top of `_SubagentToolBase` so the next person doesn't repeat the mistake.

### E2E verification (the kind of observability the user asked for)

Single-turn live-agent test:

1. Spawn subagent: "Read NOTES.md and return its markdown headings as a bullet list"
2. `sleep(8)` (parallel with spawn — model chose to dispatch both in one assistant message)
3. `subagent_list`
4. `subagent_output(task_id)`
5. Report the headings

The meta timeline recorded all four tool calls with the right msg_index pointers — `spawn` and `sleep` shared `msg_index=1` (parallel calls), `subagent_list` at `msg_index=4`, `subagent_output` at `msg_index=6`. Each with its `tool_call_id` and `duration_ms`. The agent returned the three correct headings from the workspace file.

The parallel-call case is the one that matters operationally: the model wanted to start the subagent and the sleep simultaneously because the sleep doesn't depend on the spawn result. The auto-meta-event recording handled this correctly without special-casing parallel calls.

### Lesson

For background-task lifecycle, the most valuable affordance is **retention with a bounded window**, not "kill everything that finished". Statuses cost ~200 bytes each; 100 of them is 20KB per session. That memory buys the model the ability to ask "what did task X return?" a turn or three after the announce, which is the common UX. Aggressive cleanup looked cleaner in V1 but broke the natural "fire and check later" flow that makes background work feel native.

## Subagent monitor + cron update — items #6 and #7 (May 2026)

### #6 subagent_monitor

Fifth lifecycle tool in [durin/agent/tools/subagent_lifecycle.py](../durin/agent/tools/subagent_lifecycle.py). Whereas `subagent_status` returns a full snapshot, `subagent_monitor` returns a **cursor-based diff**: the caller passes `after_event=N` (typically the `next_cursor` from the previous monitor call) and receives only the events the manager has accumulated since index N. The response also includes `next_cursor`, `phase`, `iteration`, `is_running`, and — when the task has finished in the meantime — the final output / error / stop_reason.

The natural usage pattern is **poll-sleep-poll**:

```
monitor(task_id, after_event=0)  → cursor=4
sleep(5)
monitor(task_id, after_event=4)  → cursor=7  (+ final output if finished)
```

This is cheaper than re-fetching the whole event list on every poll, and lets the model build a running narrative of the subagent's progress without paying the same tokens twice. The "finished output bundled into the monitor response" detail removes the extra `subagent_output` round-trip when the task ends between polls.

#### Manager-side support

Added `SubagentManager.monitor_since(task_id, session_key, after_event)` returning the same dict shape the tool renders. Same session-scope ownership check as the other lifecycle methods. Clamps an out-of-range `after_event` to `len(events)` so the model can pass a stale cursor without getting an error.

#### E2E verification

Spawned a subagent that read DATA.md and returned its 3 markdown headings. Single-turn flow: `spawn → monitor(after_event=0) → sleep(8) → monitor(after_event=<cursor>)` → report headings. The meta timeline recorded all four tool calls; the model correctly threaded the cursor through both monitor invocations and reported the right headings.

### #7 cron `update` action

Added `action="update"` to the existing `cron` tool ([durin/agent/tools/cron.py](../durin/agent/tools/cron.py)). The underlying `CronService.update_job` already supported mutation; this commit just exposes it to the model with the same per-action parameter conventions as the rest of the tool (job_id + any of name / message / schedule / deliver).

Validation rules:

- `job_id` required; unknown id returns `"not found"` rather than silently inventing a job
- At most one of `every_seconds` / `cron_expr` / `at` per call (mirrors `add`)
- `tz` only valid alongside `cron_expr`
- ≥1 actual change required — a `cron(action="update", job_id="X")` call with no other fields errors with a clear hint instead of being a silent no-op
- System jobs (e.g. `dream`) remain protected: update returns the same "protected" message as remove

#### Why no separate `cron_update` tool?

The roadmap entry framed it as "list/delete/edit". List + delete already existed as actions on the single `cron` tool; adding a separate `cron_update` tool would have fragmented the surface that the model already understands. The action-enum dispatch keeps the tool count stable while extending capability. Same pattern Hermes uses.

#### E2E verification

`add` → `update` (rename + swap from `every_seconds=3600` to `cron_expr="0 9 * * *"` with `tz="America/Vancouver"`) → `list`. The list confirmed both the rename and the timezone-aware cron expression. The agent reported the final state in one line: "renamed-standup — cron: 0 9 * * * (America/Vancouver)".

### Lesson

When a tool has multiple actions on the same resource (cron jobs), keep them under one tool with an action enum rather than splitting into N micro-tools. The model treats a single tool as one mental object — "I know cron has these operations on it" — and the enum gives it perfect discoverability without inflating the tool list visible to the LLM. The penalty is per-action validation lives in the tool itself rather than the schema layer, but that's a small fixed cost.

For polling-style observability (Monitor), cursor-diff is dramatically cheaper than snapshot-everything-each-time. Even a 5-event subagent on a 10-poll loop saves 45 redundant event renderings. The cursor convention also forces the model to think incrementally rather than re-summarizing the full history each turn, which is a behavior win on top of the token win.

---

## Tier 1 + Tier 2 harness hardening — OpenClaw + Hermes-inspired (May 2026)

### Context

After doc 07 (external-agent review) and doc 08 (Phase 2 memory synthesis), reviewing the OpenClaw and Hermes codebases surfaced a long list of **harness improvements unrelated to memory or skills**. These target the boundary between the model and the environment — the same family as Phase 1 hardening (`1A`, `1B`, `2B` above) — but covering failure modes Phase 1 didn't reach.

The pattern across both source projects: defensive instrumentation in the runner / consolidator / provider layer that doesn't try to teach the model anything. It just catches predictable failure modes the model can't escape on its own and either repairs them silently or terminates the turn with a clear `stop_reason` so the caller can decide.

15 items shipped in May 2026 across 15 independent commits. Each commit is a single concern with tests; each is auditable in isolation in `git log`. Final state: **1793 tests passing**.

### Tier 1 — Operational resilience (7 items, all OpenClaw or Hermes)

These are the low-blast-radius items: cheap defensive checks at the runner / provider layer.

| # | Component | stop_reason / signal | Telemetry event | Env knob |
|---|---|---|---|---|
| 2C | Idle-timeout circuit breaker | `circuit_breaker_idle_timeout` | `circuit_breaker.idle_timeout` | `DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS=1` |
| 2D | Per-block tool-result validation | (transparent repair) | (none — caps before aggregate path) | (no knob — 100 KB text / 5 MB image / 10 MB audio) |
| 2E | Re-sanitize after `context_transform` | (transparent repair) | (none) | (no knob) |
| 2F | Compaction grace window | (deadline extension) | `compaction.grace_extended` | `DURIN_COMPACTION_GRACE_S=30` |
| 2G | Per-model `parallel_tool_calls` gating | (transparent inject) | (none) | `agents.defaults.parallelToolCalls` config dict |
| 2H | Per-turn tool-result budget | (transparent spillover) | `turn_budget.enforced` | `DURIN_TURN_BUDGET_CHARS=200000` |
| 2I | Heartbeat isolated sessions | (per-tick fresh session) | (none) | `heartbeat.isolatedSessions=false` |

**Patterns that recur across Tier 1**:

- **Circuit breakers with thresholds**: idle-timeout (2C) and post-compaction (Tier 2 C2) follow the same shape — counter increments on failure signal, resets on forward progress, opens after threshold with distinct `stop_reason` and telemetry event. Adopted because it's the cheapest way to bound the cost of a stuck model without trying to diagnose *why* it's stuck.
- **Defensive validation at the boundary**: per-block validation (2D), tool-arg repair (Tier 2 B1) — assume the model emits garbage and fix it at the receive point rather than letting downstream layers explode.
- **Grace windows for known-slow operations**: compaction grace (2F) extends the outer timeout once when consolidation is detected in flight. Wraps `asyncio.wait({task}, timeout=...)` (which doesn't cancel) instead of `asyncio.wait_for` so the same task can be probed.

### Tier 2 — Resilience + reliability + context engineering (8 items)

Grouped into three blocks of independent concerns. The user explicitly approved doing C-block (context engineering) now despite its overlap with Phase 2 memory, on the reasoning that the *organization* (3-tier cache-friendly layout) is reusable regardless of what memory adds.

| Block | # | Component | Telemetry / signal |
|---|---|---|---|
| A — Resilience | 3A | Pre-emptive compaction trigger | `compaction.preemptive_trigger` |
| A — Resilience | 3B | Mid-turn precheck signal | `mid_turn_precheck.overflow` |
| A — Resilience | 3C | Compaction lock aggregate timeout | `compaction.lock_timeout` |
| B — Tool reliability | 3D | Tool-call argument repair | `tool_call.argument_repair` |
| B — Tool reliability | 3E | Unknown-tool loop guard | `unknown_tool.loop_guard` |
| B — Tool reliability | 3F | History image / audio prune | (transparent prune) |
| C — Context engineering | 3G | 3-tier system prompt for cache stability | (no event — organizational) |
| C — Context engineering | 3H | Post-compaction loop guard | `post_compaction_loop.tripped` |

### Key design decisions (and where to revisit them)

1. **A1 + A2 + 3H together cover the "stuck after compaction" failure mode**. A1 compacts at 50% of window (was: ~93%) → more frequent, smaller compactions. A2 catches the case where post-sanitize prompt is still oversized → distinct stop reason instead of waiting for the provider 400. 3H detects when compaction *happened* but didn't break the loop (same tool/args/result triple repeating) → abort with distinct stop reason.

2. **`consolidation_ratio` semantic changed**. Previously "fraction of input budget to retain after compaction" (60K from 119K = 50%). Now "fraction of trigger threshold to retain" (32K from 64K = 50%). The change keeps the user-visible 0.5 default meaningful under the new lower trigger — without rebasing, target ≈ trigger and each compaction round would do almost no work. Two existing tests that verified the loop mechanics are pinned to `preemptive_compact_ratio=1.0` (legacy trigger) so they keep verifying what they were designed to verify.

3. **Per-model `preemptive_compact_ratio` lives on `ModelPresetConfig`, not as a separate dict**. User explicitly rejected a dual ratio + max_tokens design — "lo importante es que se pueda configurar por modelo". A 1M-window model with `preemptiveCompactRatio: 0.15` compacts at 150K (sensible) instead of 500K (paying per token shipped). Falls back to `AgentDefaults.preemptive_compact_ratio` when the preset doesn't override.

4. **B2 unknown-tool guard is per-tool-name, not per-(name, args)**. 1A blocks exact `(tool_name, args)` repeats; B2 fires when the same hallucinated NAME is called repeatedly even with varying args. The model is experimenting with the wrong name — once it's done that 3 times, the third try wastes tokens.

5. **B3 image prune is read-time, not write-time**. Tier 1 2D caps individual oversized blocks at write time. B3 handles the orthogonal problem: a 5 MB image attached in turn 1 still rides along on turn 10 even though it's already in the model's KV cache. Replaces with `[image data removed - already processed by model]` after `preserve_turns=3` completed turns. Idempotent — re-running over already-pruned history is a no-op.

6. **C1 moved the agent-mode suffix from "near top of prompt" to "between stable and volatile"**. The old test `test_plan_suffix_appears_near_top_of_prompt` was replaced with `test_plan_suffix_precedes_volatile_blocks` — same intent (mode suffix should outrank dynamic content) reformulated for the layered design. The stable prefix (identity + bootstrap + skills catalog) becomes byte-identical across all turns of one session, which is exactly what provider prompt caches need.

7. **C2 (post-compaction loop guard) uses `should_abort is True` strict identity check**. Real `Verdict.should_abort` is a bool; MagicMock-wrapped guards in unrelated tests return truthy mock attributes that would have falsely tripped the abort path. The strict check makes the integration robust to test-suite shapes we don't control.

### What we deliberately did NOT do in Tier 2

| Item | Why skipped |
|---|---|
| **Compaction continuation retry attempts** (OpenClaw `compactionContinuationRetryAttempts`) | Only applies when heartbeat performs a compaction mid-turn. Durin's heartbeats are one-shot — irrelevant. |
| **Session takeover error** (OpenClaw `EmbeddedAttemptSessionTakeoverError`) | Multi-process / multi-tab. Durin is single-process. |
| **Assistant failover** (OpenClaw `assistant-failover.ts`) | `fallback_models` config covers our case; OpenClaw's is more sophisticated but overkill. |
| **LanceDB + dreaming + 6-factor scoring** (OpenClaw memory subsystem) | That's Phase 2 memory — not Tier 2 harness. Doc 08 is the discussion. |
| **HuggingFace embedded GGUF** | Doc 08 alternative; same scope deferral. |
| **Steering queue** (pi) | Specific to pi's UX architecture; durin's CLI works differently. |

### Lessons

- **Defensive layers compose**: 2C (idle-timeout) + 3B (mid-turn precheck) + 3H (post-compaction loop guard) form a stack — each catches a different "stuck" mode. Together they bound the cost of an unrecoverable state without trying to teach the model anything. None of them is sufficient alone; all together are.
- **Per-block + aggregate validation are both necessary**. 2D (per-block) handles a single huge image; 2H (per-turn aggregate) handles many medium results that sum to overflow. Neither subsumes the other. Both feed into the existing per-tool `max_tool_result_chars` cap, which handles the third dimension (per-tool char limit). Three layers, each catching a distinct shape of failure.
- **Idempotency matters for transformations in the sanitize pipeline**. `prune_processed_history_images` returns the same object identity when nothing changed (3F); `validate_tool_result_blocks` does the same (2D). This avoids unnecessary allocation AND makes them safe to call twice in the pipeline (which we do for the orphan repair around `_snip_history`).
- **Telemetry-first instead of behavior-first**. Many Tier 2 items emit a structured event even when they take no action (e.g. `tool_call.argument_repair` with `parsed_ok` in the payload — fires even when the cleaning didn't fully fix the JSON). This means we can ship the breaker and the configurable threshold *together*, then tune the threshold from real production data. Without the event, we'd be tuning blind.
- **Configurable knobs default to OpenClaw / Hermes values**. Where the source project documented a default (`MAX_CONSECUTIVE_IDLE_TIMEOUTS_BEFORE_OUTPUT=1`, `PRESERVE_RECENT_COMPLETED_TURNS=3`, etc.) we adopted it unchanged. The two projects already tuned these on their own evals; we don't have better numbers yet, and divergence for divergence's sake is a maintenance liability.
- **Test pins are sometimes the right answer**. A1's semantic change broke two tests that legitimately verified the loop mechanics. Rather than rewriting them (and losing the existing coverage), we pinned them to `preemptive_compact_ratio=1.0` and called out the pin in the docstring. New A1-specific tests cover the new behavior. This is the cheaper path to keep both invariants.

## Secrets subsystem — Phase 1 + 2 (2026-05-22)

API keys used to live as plaintext inline in `config.json.d/providers.json`.
Built a secrets subsystem (full design: `docs/archive/11_secrets_design.md`).

### What shipped

- `durin/security/secrets.py` — `SecretStore` over `~/.durin/secrets.json`
  (mode 0600, outside the config tree). Entries carry two orthogonal axes:
  `service` (classification, non-unique — many secrets may share one) and
  `scope` (consumer authorization: `exec`, `skill:*`, `provider:<n>`, …).
- `${secret:NAME}` reference grammar — whole-field only, no partial
  interpolation. Config holds the reference; `resolve_secret()` resolves
  lazily at the point of use so plaintext never re-enters the `Config`
  object, logs, or telemetry.
- `durin secret set/list/show/rm/grant/revoke/migrate`.
- Resolution wired into `Config.get_api_key()` + the provider factory.
- `migrate_plaintext_provider_keys` — explicit (`durin secret migrate`),
  idempotent, backs up config first.
- Onboard wizard writes provider keys to the store as references.
- Phase 2: `SecretRedactor` — the agent runner redacts stored secret
  values out of every tool result before it reaches the model;
  `ExecTool` injects `exec`-scoped secrets into the subprocess env.

### Decisions

- **Isolation > encryption.** A 0600 file is barely more secure than the
  old config — both are user-readable plaintext, and the agent process
  needs the value in clear anyway. The real wins are: value never in
  config / never in model context / never in chat, and only reaching the
  exec that needs it. Encryption-at-rest was explicitly de-scoped.
- **`service` is not a unique key.** Two Atlassian tokens (work/personal),
  several keys per provider — all valid. `name` is the unique id;
  `service` classifies; `account` distinguishes within a service.
- **Migration is explicit, not auto-on-load.** Silently rewriting config
  under tooling and tests is worse than an opt-in `durin secret migrate`.
- **Config reference = authorization.** `resolve()` does not check `scope`
  for a config-field reference — writing the reference into config is the
  grant. `scope` gates auto-injection (`exec`) and the future agent flow.
- Validated against hermes (`.env` + static curated registry +
  `redact_secrets`), openclaw (`SecretRef` indirection, no semantic
  purpose), pi-agent (`!command` inline resolution, no store). None did
  cross-skill reuse or per-agent scoping; the two-axis model is durin's.

### Not done (follow-ups, see docs/11 §13)

- Resolution + migration for `tools.web.search` keys and channel tokens —
  Phase 1 landed provider-first.
- `skill:<name>`-scoped exec injection (needs skill context in `ExecTool`).
- Multi-agent `agent:` scope enforcement (data model only).
- `durin doctor` dangling-reference check; log-sink redaction.
- Phase 3: `need_secret` / `request_secret` agent tools.

### Secrets — follow-up same day (2026-05-22)

After the provider-first Phase 1+2 landed, extended in the same session:

- Resolution wired for **web-search keys and channel tokens** too —
  `WebSearchTool._resolved_api_key()` and a single `${secret:}`-resolving
  chokepoint in `ChannelManager` before each channel is constructed. The
  wizard writes those credentials as references as well. Secrets now work
  in every place config expects a credential, not just providers.
- **Agent tools** `list_secrets` + `request_secret` (`agent/tools/secrets.py`).
  `request_secret` reuses the `ask_user_question` yield pattern: rather
  than prompting inside the agent loop (fragile across CLI/gateway/
  channels), it returns the exact `durin secret set` command for the user
  to run — the value goes to the store via the CLI, never through the
  agent. With Phase-2 exec injection this closes the loop: discover →
  request → use as `$NAME` in shell commands, agent never sees a value.
- `durin doctor` gained a `secret refs` check (dangling `${secret:}`).

Decision recorded: `request_secret` yields a command instead of an
interactive prompt. An in-loop hidden prompt would be more magical but
fragile (prompt_toolkit terminal state; no TTY under gateway/channels).
The yield form is bulletproof and keeps the value maximally isolated —
the user types it straight into the CLI store, it never reaches the
agent process at all.

### Secrets — install walkthrough findings (2026-05-22)

Did a full uninstall → clean pipx install → reconfigure walkthrough.
Verified end to end: agent CLI (`durin agent -m`), and a real web
session (HTTP bootstrap → ws token → streaming reply). Seven UX/bug
fixes came out of it:

1. `config set` now bootstraps a default config instead of demanding
   `onboard` first.
2. `config show` shows `${secret:}` references verbatim — only literal
   secrets are masked (a reference is not a secret).
3. `durin uninstall` stops the gateway daemon first (was orphaning it).
4. `onboard --no-wizard` next-steps no longer points at the wrong file.
5. New `durin config import <path>` — replicate a setup without the
   wizard; migrates plaintext keys on the way in.
6. `durin doctor` warns on multiple `durin` on PATH (venv + pipx).
7. **Critical**: `config show/get/set/edit` were broken on the split
   layout — `load_raw_config` read the `{"_layout":"split"}` marker, so
   `config set` validated that to an all-defaults Config and saved it,
   silently wiping every other value. `load_raw_config` now delegates
   to the layout-aware `read_persisted_config`.

`durin --help`'s command-group epilog is regenerated from the live
registry so it can't go stale.

## Post-a7: secrets Phase 3, web parity, interactive renders, design system (2026-05-22)

After v0.1.0a7, a run of delivery plus a few design decisions worth recording.

### Interactive `request_secret` — the two-channel rule

The agent needs to *ask* for a credential, but the secret value must never
enter the agent's context. The decision: **two separate transport channels**.
The agent channel carries the `request_secret` tool call and, later, a
metadata-only "stored" note — never the value. The secret channel carries
only the value: a masked input → client code → `SecretStore`. The widget is
co-located with the chat, but its submit calls a different pipe.

Concretely the value rides the **websocket** as a dedicated frame, not the
old `/api/secrets/set` GET — which put the value in a URL query, a real leak
vector (browser history, proxies, logs). The TUI uses a masked modal straight
to `SecretStore`. The LLM is told only `name` / `service` / `scope`.

### Why a design system, and its scope

durin had three visual surfaces that had drifted apart — the web had a
deliberate palette, the TUI used stock Textual themes, the wizard had almost
no colour. Rather than patch each, we established one source of truth:
`design/DESIGN.md` + `design/tokens.css`, mirrored into `durin/cli/theme.py`
for the non-CSS surfaces, with a test pinning the mirror to the CSS.

Three palettes (Ithildin default, Forge, Mithril) × light/dark. Reference
study: Pi (single-source theming, done well) and Hermes (three surfaces, no
shared source → visible drift — the cautionary case the anti-drift test
guards against).

Deliberate **scope** decision: the one-shot CLI commands (`status`, `doctor`,
`config`) are *not* themed — they're tools, not spaces, and use semantic
colour only. The design system governs the surfaces you inhabit (web, TUI)
and the guided flow (wizard); nothing else.

### Web config parity

The dashboard can now configure all of durin without the CLI: a generic
`/api/config` plus a schema-driven "All settings" view, a Secrets section,
and a refined Settings IA. The principle: anything in the config schema is
reachable from the web; curated sections sit on top for the common paths.

## Memory audit gap D — what the vector embeds (2026-05-23)

While auditing the memory subsystem (Phase 1 + 2 landed; Phase 3 not yet
started) we surfaced ten gaps. The most clear-cut one — gap D — was that
`VectorIndex._embed_text` only fed the embedder `summary > headline > body`
as a fallback chain. For a corpus entry written via `memory_store(content=...)`
with no explicit summary, the embedder only saw the auto-generated
~10-word headline. That makes vector recall over class D (queryable
corpus) approximately useless for any query that doesn't match the
headline literally — the body content carrying the actual information
never enters the embedding.

Fix: compose `headline → summary → entities → body` in that order, into a
1500-char budget (≈ 375 tokens, well under e5-small's 512 max). Order
chosen so the embedder sees the most distilled signal first (headline,
summary), the typed entities next (a high-signal-per-char block once gap
A lands), and the body last as the longest, most truncatable piece.

The change is one method on `VectorIndex` plus its tests. No migration
needed — entries already in the LanceDB index keep working; new writes
and any `rebuild_from_workspace()` call pick up the new composition.

**Lesson** worth stating: when you introduce optional fields to a schema
(summary on `MemoryEntry`) the behavioural assumption that "the caller
will fill them" doesn't hold, especially for an LLM-driven write path
where the model defaults to minimal-cost arguments. Either make the
field mandatory at the construction site, or make the consumer (here:
the embedder) tolerant of the field being empty. The previous fallback
chain was the second strategy but it stopped too early — fell back to
headline, never reached body — so the consumer wasn't tolerant enough.

## Memory embedding hardening — F1+F2+F3 (2026-05-23)

The vector-retrieval pillar of Phase 2 had ten gaps that surfaced in a
direct audit. The most consequential ones grouped into three families
("catalog lies", "indexing incomplete", "load cost at the wrong time")
were closed in one pass and end-to-end-verified against real fastembed
+ LanceDB.

### Family 1 — fastembed catalog lies

The config schema defaulted to `intfloat/multilingual-e5-small`, the
`MemoryEmbeddingConfig` listed `BAAI/bge-m3` as the heavy alternative,
and `_FASTEMBED_DIMS` carried seven model identifiers — but fastembed
0.6+ silently retired both. Any installation with `memory.enabled=true`
crashed on the first `memory_store` with `ValueError: Model not supported`.

The fix dropped the static `_FASTEMBED_DIMS` dict and consults
`TextEmbedding.list_supported_models()` at construction time — the
fastembed catalog is build-time-constant, so caching it in
`durin/memory/embedding.py::_CATALOG_CACHE` after the first access is
safe and cheap. `FastembedProvider.__init__` now raises `ValueError`
with an actionable message listing the available models when given an
unknown id — surfaces the problem at the config boundary, not three
layers deep in a tool call.

Defaults revised after auditing the 30 models the current fastembed
ships:

- `paraphrase-multilingual-MiniLM-L12-v2` (220 MB, 384-dim) — default.
- `intfloat/multilingual-e5-large` (2.24 GB, 1024-dim) — recommended
  for Chinese/Japanese/Korean.
- `all-MiniLM-L6-v2` (90 MB, 384-dim) — English-only minimal.

The fastembed pin tightened from `>=0.4.0,<1.0.0` to `>=0.7,<0.9` so
the catalog stays inside one release window. A new test
(`tests/memory/test_embedding_catalog.py`) pins every wizard option +
the schema default against the live catalog so a future fastembed bump
fails CI before it ships to users.

`durin doctor` got a dedicated `check_embedding_model` row that prints
the model + dim when memory is on, "vector memory off" when not, and a
clear failure with `fix:` text when the configured model isn't in the
catalog. `durin status` was extended to show `vector on (<model>)` or
`vector off` so the user can confirm at a glance what they're paying
the ~220 MB / ~2.24 GB for.

### Family 2 — indexing incomplete

Three structural holes:

1. **`memory_ingest` never touched the vector index**. The user uploads
   a doc, it sits in `ingested/<id>/source.*`, and vector search can't
   see it until a hypothetical dream runs. `MemoryIngestTool.execute`
   now derives a `memory/corpus/<id>.md` entry pointing back to the
   ingested source via `source_refs`, with the first 1500 chars of the
   body in the entry. The same upsert path as `memory_store` indexes
   it immediately. Full content stays in `ingested/<id>/source.*` for
   `memory_drill`.
2. **Manual edits to memory entries left vectors stale**. There was no
   way to tell the index "this file changed". A new
   `/memory reindex` subcommand calls
   `VectorIndex.rebuild_from_workspace()` — full re-embed in ~1.3 s
   per 1000 entries with MiniLM-L12. Same code path covers users who
   change `memory.embedding.model` and end up with a dim-mismatched
   table.
3. **No dim-mismatch detection**. `VectorIndex.upsert` and `.search`
   now compare the on-disk vector column's dim against the current
   provider's `.dimensions` and raise `VectorIndexDimensionMismatch`
   with an actionable message naming `/memory reindex` as the fix.

### Family 3 — load cost at the wrong time

The first call to `FastembedProvider.embed()` paid ~18 s for the model
download. By default, that first call landed in the middle of a real
turn the user was waiting on. Two cheap fixes:

1. `FastembedProvider.warmup(model)` classmethod — loads the model and
   returns. The onboard wizard calls it transparently after the user
   picks a model (when fastembed is installed in the current venv —
   re-onboard flows), so the download happens while the user is
   actively waiting in the wizard, not mid-conversation.
2. `AgentLoop._warmup_memory_embedding` is scheduled in the background
   from `AgentLoop.run` when `memory.enabled` — covers the boot of a
   fresh process where the user just enabled memory and is about to
   start chatting. Uses `asyncio.to_thread` so the fastembed sync
   download doesn't block the event loop. Failures are logged but
   never surface to the user; the next `memory_store`/`memory_search`
   will retry the load.

### Verification

Real end-to-end against fastembed + LanceDB in a temp workspace,
covering 10 cases: unknown-model rejection, default-model load,
`memory_store` + vector upsert, `memory_search` via vector,
`memory_ingest` derivation + vector indexing, vector-recallability of
ingested docs, manual-edit + rebuild flow, dim-mismatch detection +
rebuild fix, hot-layer read, and `doctor`'s check. All ten passed.
CLI surfaces (`durin status`, `durin doctor`, `durin config set`)
verified by direct invocation.

### Pre-existing bug noted (not fixed in this pass)

`durin config get memory.embedding.*` returns `No such key` for
deeply-nested paths inside `MemoryEmbeddingConfig`, while `set` of the
same paths works. Confirmed pre-existing (reproduces before this
change) — it's a split-layout config code defect, not embedding-related.
Filed for a follow-up pass.

### Follow-up — fixed in same pass

The `durin config get` defect noted above turned out to be one line.
`cmd_get` read `load_raw_config(path)` (the as-on-disk dict, which
omits anything the user never wrote). Switched to `load_config(path)`
+ `model_dump(by_alias=False, mode="json")` so schema defaults
populate paths the user never touched. Verified:

```
$ durin config get memory.embedding.model
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
$ durin config get memory.embedding
{ "api_key": null, "base_url": null, "lazy_eviction": false,
  "model": "...", "provider": "fastembed" }
```

`No such key` still fires for genuinely-absent paths (`config get
nonexistent.key`). The fallback to `load_raw_config` is kept for the
case where the on-disk config fails schema validation — better to show
a value than refuse all queries while the user fixes the typo.

## Context composition telemetry (2026-05-23)

Phase 2 of the memory work added embedding telemetry (load duration,
embed duration, recall latency) but not visibility into how the
context budget is spent across tiers. Without that we can't answer
"how much of my 200 K context is just the system prompt today?" or
"of the cached tokens this provider reports, which component is
actually getting cached?".

The fix is a new `context.composition` event emitted from
`ContextBuilder.build_messages` once per turn-build, carrying a
tiktoken-estimated breakdown by tier:

- **stable** — identity / bootstrap / skills_active / skills_catalog
  / memory_hot. The cache-friendly prefix that providers like
  Anthropic and OpenAI's prefix cache reuse across turns.
- **context** — mode suffix (PLAN / BUILD / EXPLORE), if active.
- **volatile** — memory_long_term (MEMORY.md) / recent_history
  (history.jsonl) / session_summary. The per-turn block that breaks
  the cache.
- **history_msg_tokens** — prior turns in the message list.
- **current_msg_tokens** — the user message + runtime context for
  this turn.
- **tools_tokens** — JSON of every tool definition sent to the LLM.
- **estimated_total** — the sum, what we expect provider's
  `prompt_tokens` to approximate modulo tokenizer differences.

Sanity check from a real workspace probe:

```
  stable (cache-friendly)        2916    63.3%
     · skills_active              953    20.7%
     · bootstrap                  921    20.0%
     · skills_catalog             550    11.9%
     · identity                   492    10.7%
  tool definitions               1221    26.5%
  history (prior turns)           418     9.1%
  current message                  55     1.2%
  ──────────────────────────── ──────
  estimated total                4610
```

This is exactly what we wanted: in this turn the stable prefix is 63%
of the input, so when `cache.usage.cache_ratio_pct` is around that
number we know caching is working as expected on the stable layer. If
it's much lower, something in stable rotated (skills loaded, bootstrap
edited, hot layer flipped under dream). If higher, tools are getting
cached too — depends on the provider's reach.

**Implementation cost**: ~150 lines on top of `ContextBuilder` (three
layer methods now also populate a sibling dict with sub-blocks per
component, so `_emit_composition_event` can measure each sub-block
without re-rendering). New helper `estimate_text_tokens(str)` wraps
tiktoken on a string for these per-component counts.
`TypedDict ContextCompositionEvent` registered in
`durin/telemetry/schema.py`.

**Reflected in TUI / web**: deferred. Once we have a few sessions of
telemetry data, we'll add a composition panel that shows the current
breakdown and a sparkline of `cache_ratio_pct` over recent turns.
Measuring comes first; visualisation follows the data.

## Entity-centric memory shipped + wired (T1 closure, 2026-05-23)

**Context**: doc 03 Phase 2 (flat skill docs) was the original memory
horizon. After reviewing Hermes Agent, OpenClaw, OpenClaude, Cognee,
Graphiti, Mem0, MemPalace, HippoRAG and A-Mem (archived doc 22), we
pivoted to an **entity-centric** model: episodic entries tag entities
as `type:value`; a periodic LLM "dream" pass consolidates them into
`memory/entities/<type>/<slug>.md` pages backed by git for history.

The plan went through several iterations as it survived contact with
real implementations:

- **Doc 08** (Phase 2 proposal, three synthesis paths) — archived.
- **Doc 14** (typed entities `type:value` proposal A) — implemented;
  the closed 9-type list it suggested was superseded by doc 18 §4's
  open vocabulary (8 broad cross-profession types, suggestion not
  enforced). Archived.
- **Doc 18** (entity-centric plan, vigente) — principles, schema,
  storage, retrieval design.
- **Doc 19** (implementation plan, executed Phases 0-6) — vigente.
- **Doc 21** (integration plan) — superseded by 23+24. Archived.
- **Doc 22** (critiques validated against 9 reference systems) —
  fed doc 23's consolidated T1.x list. Archived.
- **Doc 23** (T1 implementation by risk clusters A-D, executed) —
  archived after closure.
- **Doc 24** (T1 wiring gaps W1-W4 + E2E test plan, executed) —
  archived after closure.
- **Doc 25** (post-T1 state + T2 horizon, no commitment) — vigente.

**What was built**:

| Layer | Module | File |
|---|---|---|
| Entry typing | `MemoryEntry.entities: list[str]` validated as `<type>:<value>` | `durin/memory/schema.py` |
| Git substrate | `GitRepo` over `memory/.git/` via dulwich | `durin/utils/git_repo.py` |
| Entity pages | Markdown + open-vocab frontmatter parser | `durin/memory/entity_page.py` |
| Aliases | `AliasIndex` (rebuild-only on first use, sub-second) | `durin/memory/aliases_index.py` |
| Dream | `DreamConsolidator` with pydantic + retry + context budget | `durin/memory/dream.py` |
| Retrieval | RRF-based entity-aware reranker | `durin/memory/entity_ranker.py` |
| Vector index | `VectorIndex` (LanceDB) with `entities` field + entity_page rows | `durin/memory/vector_index.py` |
| Absorption | `EntityAbsorption` (merge + archive + deindex) | `durin/memory/absorption.py` |
| CLI | `durin memory dream/history/show/diff/revert/expand/absorb/absorb-suggest` | `durin/cli/memory_cmd.py` |

**What was wired at runtime (T1, W1-W4)**:

- W1: `memory_search` invokes the entity-aware ranker on raw LanceDB
  rows when the query mentions a known entity, separating `ranking`
  from `strategy` in the response.
- W2: AliasIndex builds lazily on first `memory_search` call (no
  persistent sidecar per doc 23 T1.4).
- W3: `durin memory dream` constructs a `VectorIndex` and passes it
  to the consolidator so the resulting entity page lands in LanceDB.
- W4: `EntityAbsorption` exposed via `durin memory absorb` +
  `absorb-suggest`. Vector index deletes the absorbed row; alias
  index drops the absorbed ref.

**What was explicitly NOT built / deferred to T2** (doc 25):

- Auto-trigger of `DreamConsolidator` (today: manual `durin memory
  dream`). Auto-trigger needs LLM-judge + confirmation design.
- Identifier-based extraction in queries (today: alias-only).
- Shared `AliasIndex` via `ctx` (today: lazy per-tool instance).
- Auto-absorb post-dream (today: manual `durin memory absorb`).

**Verification**:

- 4365 tests pass, 16 skipped.
- 6 outcome tests in `tests/integration/test_phase6_outcomes.py`
  cover the doc 19 §8 operational outcomes (decisions consolidated,
  aliases unified, recurring incident, drill-down, anti-fragility).
- 4 hermetic E2E tests in `tests/memory/test_t1_wiring_e2e.py`
  exercise the full wired path (W1+W2+W3+W4).
- Live verification with real glm-5.1 + fastembed during cluster C/D
  of the original doc 23 execution.

**Bug caught by live verification, not by tests**: `_workspace_root`
in `durin/cli/memory_cmd.py` called the `workspace_path` property as
a method (`cfg.workspace_path()`). Every `durin memory <cmd>` crashed
in production. Tests didn't catch it because they patched
`_workspace_root` directly — illustrating the `feedback-verify-live`
principle: green unit tests are not feature verification.

## Shared AliasIndex via process cache — §2.C (2026-05-24)

**Context**: After T1 shipped (entity-centric memory + 4 wiring W1-W4),
three runtime consumers each built their own `AliasIndex` on first use:

| Consumer | Where |
|---|---|
| `MemorySearchTool` | `agent/tools/memory_search.py` |
| `DreamConsolidator` | `memory/dream.py` |
| `EntityAbsorption` | `memory/absorption.py` |

Each rebuild parses every `memory/entities/<type>/*.md` page. Sub-second
per build but redundant when more than one consumer hits the workspace
in the same process. Archived doc 24 W2 flagged this for T2 ("upgrade
to shared via ctx is T2 if perf needs"); doc 25 §2.C captured the
re-scoped item (3 builders, not 2 as the original archived doc
incorrectly assumed).

**Decision: process-wide cache, not `ToolContext`-scoped**. `ToolContext`
is per-tool-call inside the agent loop, but `DreamConsolidator` and
`EntityAbsorption` are invoked from `cli/memory_cmd.py` (CLI subcommands)
where there is no `ToolContext`. A module-level cache keyed by
`memory_root` covers all three call sites uniformly.

**Module**: `durin/memory/aliases_cache.py`.

- `get_shared_alias_index(memory_root) -> AliasIndex`: lazy build with
  double-checked locking, returns an empty index for cold workspaces
  (callers check `size() == 0` if they need that signal).
- `invalidate_alias_index(memory_root)`: defensive drop for out-of-band
  edits and tests. Not called from production code paths in v1 because
  the existing `refresh_for` / `remove` API mutates the shared instance
  in place — writes by dream or absorb become visible to search
  immediately without explicit invalidation.
- `_clear_all()` / `_cache_size()`: test helpers (production never
  calls these).

**Refactor surface**: each `_get_alias_index()` in the three consumers
now falls through to `get_shared_alias_index()` unless `alias_index=...`
was injected via constructor (tests). The per-instance `_alias_index`
flag in `MemorySearchTool` is gone — superseded by the shared cache.

**Tests** (15 new in `tests/memory/test_aliases_cache.py`):
- Basic sharing: same root → same instance, different roots → independent.
- Cold workspace → empty index (no exception).
- Refresh + remove propagate across consumers without invalidation.
- Concurrent first-call: 8 threads race, exactly one build runs.
- Each of the 3 real consumers resolves to the shared cache.
- Injected `alias_index` bypasses the shared cache (test escape hatch).

The pre-existing E2E test `test_e2e3_memory_search_rebuilds_alias_index_lazily`
was updated: it asserted on the removed per-instance flags
(`tool._alias_index`, `tool._alias_index_attempted`); now it asserts
on `aliases_cache._cache_size()` directly.

**Suite**: 4396 passing, 16 skipped (+15 vs pre-§2.C). Live verify
against the real binary still works (`durin memory absorb-suggest` on a
real workspace returns clean output).

**LOC actual vs estimated**: ~110 LOC source (cache module) + ~250 LOC
tests + 3 small consumer refactors. Estimate in doc 25 was "~40 LOC +
invalidation hooks" — underestimated because the test coverage was
non-trivial (concurrency, per-consumer wiring, injection escape
hatch). Source itself was roughly on target.

## Fragment/canonical retrieval contract — §2.H (2026-05-24)

**Context**: doc 18 §6 (vigente) promised that "la página consolidada
y los entries post-cursor coexisten en los resultados de retrieval;
el LLM reconcilia en read-time con timestamps y contexto". Verifying
that promise against code on 2026-05-23 showed it was broken at the
delivery boundary on **both** retrieval paths:

| Path | Gap |
|---|---|
| Lazy (`memory_search` tool result) | `Result.to_dict()` dropped `valid_from` / `entities` / `class_name`. No textual marker. The LLM had to infer canonical-vs-fragment from the URI prefix, which is brittle. |
| Eager (`hot_layer` in system prompt) | Read from the 4 legacy classes (`memory/<class>/*.md`), not from `memory/entities/<type>/<slug>.md` — the new canonical pages never made it into the prompt at all. |

Captured as §2.H in doc 25 and tagged complete-T1 (closes a wiring gap,
not a new feature). Marker convention follows the existing compaction
pattern (`=== ARCHIVED SUMMARY ===`, bitácora 2026-05-19): an explicit
ASCII delimiter the LLM can pattern-match without relying on field
structure.

**Lazy path — `Result` + tool boundary**:

- `Result` dataclass extended with `class_name`, `valid_from`,
  `entities` (all default-empty so existing callers keep working).
- `Result.kind` computes one of `canonical | fragment | session |
  ingested` — `"canonical"` iff `class_name == "entity_page"`, else
  follows `source`. Normalises `"sessions"` → `"session"` so the LLM
  sees singular kind labels everywhere.
- `Result.render_block()` produces
  `=== KIND: <uri> (ts: ...) === ... === END KIND ===` per result.
  Header carries ts for fragments and `(canonical entity page)` for
  canonical (which have no single valid_from — temporal claims live
  in prose per doc 18 §6 protocol α).
- `to_dict()` adds `kind` always and the new fields when non-empty.
- `MemorySearchTool.execute()` augments each result with a `rendered`
  field carrying the marker block. Raw fields stay alongside for
  callers that prefer structured access.
- `search_dreamed` now also walks `memory/entities/<type>/*.md`
  (excluding `<slug>/archive/` — those are absorbed records reachable
  only via `durin memory expand`). Previously canonical pages were
  vector-only; the grep fallback missed them entirely.
- `_vector_row_to_result` preserves `valid_from` / `entities` from
  LanceDB rows (previously dropped — confirmed earlier in B1 doc 24).

**Eager path — `hot_layer`**:

- New "Canonical pages" section renders top N entity pages as
  `=== CANONICAL ===` blocks. Sorted by `extra.updated_at` desc with
  mtime fallback. Caps per-page body at 600 chars so a single huge
  page can't consume the whole canonical budget.
- New "Recent fragments (post-cursor)" section renders top N
  episodic entries that tag at least one entity and whose
  `valid_from` is strictly newer than every relevant entity page's
  `dream_processed_through` cursor. Wrapped as
  `=== FRAGMENT ===`. Pre-cursor entries are filtered — they're
  already absorbed, surfacing them again defeats the consolidation.
- Section order: identity → canonical → fragments → legacy headlines
  → entity list. Total budget grew from ~1000 to ~1900 tokens —
  still cache-friendly between dreams.
- Identifiers from page frontmatter (`extra.identifiers.{email,
  slack, github, ...}`) flatten into a single "Identifiers — ..."
  line per canonical block.

**Tests**: 21 new in `tests/memory/test_fragment_canonical_contract.py`
covering Result.kind (4 kinds), to_dict() with/without new fields,
render_block header+footer for canonical+fragment, search_dreamed
surfacing both canonical and fragment, archive exclusion in grep and
hot_layer, tool boundary `rendered` field, hot_layer canonical/
fragment sections with pre-cursor filtering, cold-workspace graceful
fallback (no entities dir → no canonical/fragment sections, identity
+ legacy still work).

**Suite**: 4417 passing (+21 vs §2.H pre-state). Live verify:
`durin memory absorb-suggest` + `durin memory dream --dry-run` both
clean on the real workspace.

**LOC actual vs estimated**: ~250 LOC source (search.py + hot_layer.py
+ memory_search.py) + ~330 LOC tests + ~80 LOC docs. Estimate in
doc 25 was "~300 LOC source + ~120 LOC tests" — source was on
target; tests overshot because the contract has many edge cases
(archive exclusion in two surfaces, pre-cursor filter, cold
workspace, 4 kinds × 2 surfaces).

**Why this matters**: after §2.H, the rest of the deferred items
(§2.A.1 auto-trigger, §2.D auto-absorb, §2.F eager-inject-fetch)
build on a correct delivery contract. Without §2.H, automating the
dream would have produced output the LLM couldn't use — silent
quality loss compounding with every auto-dream tick.

## §2.A.1 dispatcher β.1 — cron daily auto-trigger (2026-05-24)

**Context**: doc 18 §6 listed four scenarios for triggering the
entity-centric dream (cron, post-compaction, session-close, threshold
per-entity). T1 shipped manual-only via `durin memory dream`. The user
asked specifically for the daily cron as non-optional plus the other
three as "scenarios we had planned" — implementing them as one
coherent dispatcher.

Decided to split β into two commits to keep each reviewable:

- **β.1** (this commit): `MemoryDreamConfig` + `DreamRunner` + lock
  + cron diario + telemetry events.
- **β.2** (next): post-compaction hook, session-close hook,
  threshold-per-entity hook in the `memory_store` write path.

**β.1 shipped**:

- `MemoryDreamConfig` (`config/schema.py`): seven knobs covering all
  four triggers + throttle + master switch. Defaults: enabled True,
  cron `0 3 * * *` (3am local — chosen to avoid the legacy `dream`
  cron's every-2h schedule), threshold 5 entries, post-compaction
  + session-close on, throttle 300s. Extended `MemoryConfig` with
  `dream: MemoryDreamConfig`.
- `DreamRunner` (`durin/memory/dream_runner.py`): synchronous runner
  wrapping `DreamConsolidator` with three production concerns:
  - **Atomic lock**: `memory/.dream.lock` via `os.open(O_CREAT|O_EXCL)`.
    Lock file carries `{pid, started_at, trigger}` JSON for diagnostics.
    Stale recovery: locks older than 10min are treated as crashed and
    removed.
  - **Throttle**: `memory/.dream.last_run` mtime gates re-runs within
    `min_seconds_between_runs`. Cheap path: stat one file, no parse.
  - **Telemetry**: three new events
    (`memory.dream.start` / `.end` / `.skipped`) with `trigger` label
    so the §2.E aggregator can split usage by source.
  Returns `DreamRunResult(ran, reason, entities_consolidated,
  entities_failed, duration_s)` so callers (CLI, future hooks, tests)
  see the same shape.
- Cron registration in `cli/commands.py` startup mirrors the legacy
  `dream` job pattern — registers `memory_dream` system job with
  the configured cron expression. `on_cron_job` dispatch routes
  `name == "memory_dream"` to `asyncio.to_thread(runner.run, trigger="cron_daily")`
  so the cron loop stays responsive during the LLM calls.

**Tests** (14 new in `tests/memory/test_dream_runner.py`):
- Cold workspace shortcuts before lock acquisition.
- Untagged-only entries → no_pending.
- Full successful pass: returns ok, page lands on disk, lock released.
- Existing fresh lock blocks; pre-existing lock is left intact (we
  don't own it).
- Stale lock (>10min) is removed and the new run proceeds.
- Throttle blocks immediate rerun; throttle=0 never blocks.
- entity_filter narrows the pass to one entity ref.
- Telemetry emits start+end on success, skipped on throttle/
  concurrent_lock, and NEVER start/end when there's no pending work
  (only skipped(no_pending)).

**Suite**: 4431 passing (+14 vs §2.A.1 β.1 pre-state). Live verify:
`durin status` shows config loads cleanly; `cfg.memory.dream.*`
reflects all new fields with the documented defaults.

**LOC actual**: ~280 source (dream_runner.py + schema additions +
cron wire) + ~290 tests + ~60 telemetry schema. Total ~630.
Estimate in doc 25 was ~420 for the full β (all 4 triggers); β.1
alone is roughly half because the post-compaction / session-close /
threshold wiring is the remaining work — pure plumbing into existing
hooks.

**Why split β**: the dispatcher + cron is self-contained and gives
the user the explicitly-requested "always on" daily pass. The other
3 triggers all reuse `DreamRunner.run()` — adding them is plumbing,
not new mechanism — so they can ship in β.2 without re-opening the
lock/throttle/telemetry design.

**Trigger label vocabulary** (for §2.E aggregator parsing):
- `cron_daily` — fired by the system cron job.
- `post_compaction` — fired after `Consolidator` archives a chunk.
- `session_close` — fired on `/quit` or idle timeout.
- `threshold` — fired by memory_store when per-entity count crosses
  `threshold_entries`.
- `manual` — fired by `durin memory dream` CLI (β.2 will refactor
  the CLI to route through `DreamRunner` so manual runs also pick up
  the lock/throttle).

## §2.A.1 dispatcher β.2 — 3 sub-daily triggers + manual route refactor (2026-05-24)

**Context**: β.1 shipped the runner + cron daily. β.2 wires the
remaining three triggers from doc 18 §6 plus refactors the manual
CLI command to go through `DreamRunner` so all five entry points
share the same lock / throttle / telemetry surface.

**Manual route refactor — `cli/memory_cmd.py:cmd_dream`**:
The dry-run preview stays in the CLI (cosmetic). The real path
constructs `DreamRunner(min_seconds_between_runs=0)` so the user's
explicit invocation skips the throttle, and calls `runner.run(
trigger="manual", entity_filter=entity, on_progress=...)`. Progress
callback feeds rich-printed lines per entity. Concurrent-lock case
prints a friendly notice instead of silently no-op-ing.

**Threshold trigger — `MemoryStoreTool._maybe_dispatch_threshold_dream`**:
After every successful write that tagged at least one entity, the
tool:
1. Reads `dream_config` (injected via `create(ctx)` from
   `ctx.config.memory.dream`).
2. Returns early if config is missing / disabled / threshold ≤ 0.
3. Calls `_discover_pending_consolidations(memory_root, entity_filter=None)`
   once to count post-cursor entries per entity.
4. For each entity that crossed `threshold_entries`, spawns a daemon
   thread named `dream-threshold-<ref>` that calls
   `DreamRunner.run(trigger="threshold", entity_filter=ref)`.

Daemon threads (not asyncio tasks) keep the implementation simple —
`memory_store.execute` is already async, but the DreamRunner is sync
and we'd need `to_thread` anyway. Threads daemon so process shutdown
doesn't wait on them. The runner's own lock + throttle absorbs bursts
when many threshold-crossings happen in quick succession.

**Post-compaction hook — `Consolidator.on_post_compaction`**:
New attribute on `Consolidator` (default `None`). Fires inside the
`maybe_consolidate_by_tokens` finally block, right after
`post_compaction_guard.arm(...)`, when `last_summary` is truthy.
Wrapped in try/except so a buggy hook never breaks consolidation —
markdown is the source of truth.

Wired in `cli/commands.py` startup: when `memory.dream.post_compaction`
is True, set `agent.consolidator.on_post_compaction` to a thunk that
spawns a daemon thread (same pattern as the threshold trigger)
calling `DreamRunner.run(trigger="post_compaction")`.

**Session-close hook — `AgentLoop.on_session_close` + `cmd_new`**:
New attribute on `AgentLoop` (default `None`). `cmd_new` calls it
after archiving the prior session, with the same try/except defense.
`getattr(loop, "on_session_close", None)` keeps test scaffolds
(SimpleNamespace loops) working without the attribute.

Wired similarly: when `memory.dream.on_session_close` is True, set
`agent.on_session_close` to a thunk spawning a daemon thread with
`trigger="session_close"`. Independent of `post_compaction` — a user
may want one on and the other off.

**`hasattr(agent, ...)` defense in wiring**: the cli/commands.py
startup also handles fake `_FakeAgentLoop` test scaffolds that don't
have `consolidator` / `on_session_close` attributes. Both wiring
branches gate on `hasattr` before assigning.

**Tests** (11 new in `tests/memory/test_dream_triggers_beta2.py`):
- Consolidator source declares `on_post_compaction` + call site
  exists (source-level pin to avoid spinning up the full deps tree).
- Hook delivery semantics: callback invoked with session key.
- `cmd_new` invokes `loop.on_session_close` when set, handles missing
  attribute, swallows hook exceptions.
- `AgentLoop` source declares `on_session_close` attribute.
- Threshold: does not dispatch below count, dispatches when crossed,
  disabled when config None, disabled when threshold=0, dispatches
  per-entity when multiple cross.

Plus pre-existing fixes:
- `cmd_new` now uses `getattr(loop, "on_session_close", None)` so the
  tests in `tests/agent/test_unified_session.py` (which build
  SimpleNamespace loops without the attribute) keep passing.

**Suite**: 4442 passing (+11 vs β.1 close). Live verify: `durin status`
+ `durin memory dream --dry-run` both clean on real workspace.
Smoke e2e exercised all 4 triggers end-to-end (cron_daily via real
runner, threshold via real memory_store hook, post_compaction +
session_close via direct hook invocation pattern).

**LOC actual**: ~150 source (runner refactor + 3 hook integrations
+ wiring) + ~370 tests. Estimate in doc 25 β was "alto" for D
(post-compaction) and the user said "los 4 tiene sentido" — split
β into β.1 + β.2 to keep each commit reviewable kept the change set
manageable (β.1 ~990 LOC commit; β.2 ~520 LOC).

**Trigger label vocabulary in production** (`telemetry.MemoryDreamStartEvent.trigger`):
`cron_daily` | `post_compaction` | `session_close` | `threshold` | `manual`.

## §2.D auto-absorb post-dream — LLM-judge + threshold + git-audit (2026-05-24)

**Context**: after §2.A.1 shipped (4 dream triggers automated), the
loop between dream and absorption was still manual: `find_candidates`
existed and `absorb()` worked, but nothing wired them together. User
had to remember to run `durin memory absorb-suggest`. Doc 25 §2.D
captured the gap.

**Research before deciding** (3 reference systems compared):

| System | Auto-merge? | Detection | Trust |
|---|---|---|---|
| OpenClaude | NO | LLM-driven prevention at write-time | model self-judgement |
| OpenClaw | NO | exact ID lint error | forces manual review |
| Hermes curator | YES | LLM content-semantic, NO embeddings, NO threshold | trust LLM declaration + rich audit |

2/3 systems explicitly REFUSE auto-merge (silent false positive risk).
Hermes does it but trusts the LLM completely with per-run REPORT.md.
Our design ended up a hybrid neither system uses: threshold-gated LLM
judge + per-dream-pass trigger + reuses our existing git substrate for
audit + 24h quarantine to block premature consolidation. Defensible
because the user has explicit threshold preference + opt-in default OFF
mitigates blast radius.

**glm-4.6 peer review** (same pattern as T1) found 1 BUG + 5 substantive
critiques. All 6 adopted:

- **C4 (BUG)**: `absorb()` deleted absorbed entity from vector index
  but never re-upserted the canonical after the body merge. Pre-
  existing bug — manual path also affected. **Fixed**: after the git
  commit, re-call `vector_index.upsert_entity_page` with merged body
  so semantic search picks up the new content.
- **C3 (risk grave)**: premature consolidation loop where the dream
  pass that just wrote two near-identical pages immediately judges
  them as "same" (same-model bias). **Mitigation**: 24h quarantine
  gate — auto-absorb only considers pages where both file mtimes are
  older than `min_age_hours`. Forces decisions on stable, observed
  state rather than fresh dream output.
- **C2 (bias)**: judge prompt is blind to temporal context — can't
  distinguish "Marcelo 2022" from "Marcelo 2026". **Fix**: prompt now
  includes file mtime + `dream_processed_through` cursor + identifiers
  per page so judge can reason about temporal separation.
- **C5 (telemetry)**: need a "regret" signal for §2.E tuning.
  **Added**: `memory.absorb.reverted` event, emitted from `cmd_revert`
  when target commit has `Reason: auto` trailer.
- **D6 (slug-picker frágil)**: "more git commits wins" rewarded old
  zombies over fresh canonical pages. **Replaced** with: newer
  `dream_processed_through` cursor wins → tiebreak by episodic
  entries referencing the ref (centrality light) → alfa final.
  Content from BOTH pages is preserved via existing `_merge_pages()`
  regardless of which slug wins — the picker only decides which URL
  is the merged page's home.
- **C6 (threshold)**: kept at 95 per user preference. glm argued
  85-90 because LLMs commonly assign 80-90 for "obviously same";
  user explicitly said 95 is fine. Tunable from config without code
  change as data arrives via `memory.absorb.judged`.

**Defer with rationale** (glm C7-A, C7-B, C7-C):
- Re-synthesis body merge instead of append (avoids zombie growth):
  real improvement, also applies to manual path; not blocking MVP,
  ship if telemetry shows body bloat hurting LLM signal quality.
- Type hierarchy (`person` ↔ `agent` could be the same): no schema
  for inheritance today; over-design preventive.
- Judge sees relational context (A→ProjectX vs B→ProjectY = different):
  requires building entity graph formally; T3+.

**Components shipped** (~485 LOC source + ~480 LOC tests):

| Layer | File | Function |
|---|---|---|
| Judge LLM | `durin/memory/absorb_judge.py` | `judge_pair()` + `JudgeResult` + adversarial prompt + markdown-marker parser with retry |
| Prompt | `durin/templates/dream/absorb_judge.md` | "alias overlap necessary but NOT sufficient", default to "different" on weak evidence |
| Dispatcher | `dream_runner.py::_maybe_auto_absorb` | post-`_consolidate` (only if `consolidated > 0`): cross-type filter → 24h quarantine → judge → threshold gate → picker → absorb |
| Picker | `dream_runner.py::_pick_canonical` | recency-first per D6 (revised) |
| C4 fix | `absorption.py::absorb()` | re-upsertea canonical post-merge |
| Audit enrichment | `absorption.py::absorb()` | new kwargs `judge_reasoning` + `judge_confidence` → commit body + `Judge-Confidence` trailer |
| Revert (real) | `cli/memory_cmd.py::cmd_revert` | implementación via `subprocess.run(["git", "revert", "--no-edit", sha])` — antes era stub print |
| Config | `config/schema.py::AutoAbsorbConfig` | enabled / confidence_threshold / min_age_hours / judge_model |
| Telemetry | `telemetry/schema.py` | 4 new events: `judged` / `auto_merged` / `skipped` / `reverted` |
| Wiring | `cli/commands.py` + `cli/memory_cmd.py` + `memory_store.py` | 3 DreamRunner construction sites pass the 4 `auto_absorb_*` kwargs |

**Tests** (~480 LOC, 31 new across 2 files):
- `tests/memory/test_absorb_judge.py` (18): template placeholders,
  parser happy path + every malformed shape, prompt rendering with
  identifiers + timestamps, judge_pair retry + LLM exception wrapping.
- `tests/memory/test_auto_absorb_dispatcher.py` (13): cross-type
  filter, 24h quarantine + zero-disables, threshold gating, verdict
  gating (different/unclear), judge failure handling, happy-path
  absorb with metadata, picker (cursor / centrality / alfa), C4 fix
  (vector re-upsert verified), commit trailer enrichment.

Pre-existing test fix: `test_revert_with_yes_prints_guidance` renamed
to `_runs_git_revert` and updated for the new actual-revert behaviour
(skips when `git` binary missing).

**Suite**: 4457 passing (+31 vs §2.A.1 β.2 close), 16 skipped + 1
deselected (pre-existing env failure in
`test_check_executable_returns_ok_for_known_binary` that fails on main
without these changes — `python` not in PATH issue unrelated to §2.D).

**Live verify**: `durin memory absorb-suggest` + `durin memory dream
--dry-run` both clean. Smoke e2e: dream pass with stub LLM
(consolidator + judge) end-to-end produces auto-merge commit with
`Judge-Confidence: 97` trailer + full reasoning in body + page
archived correctly. Quarantine gate verified by observing that the
same smoke with `min_age_hours=24` blocks the merge (intentional —
dream just wrote both pages, mtime=now).

**Activación en producción**: `durin config set
memory.dream.auto_absorb.enabled true`. Default OFF — user explicitly
must opt in. Threshold 95 / quarantine 24h / model=dream model.

**Event vocabulary for §2.E aggregator**:
- `memory.absorb.judged`: every LLM call (verdict + confidence + duration).
- `memory.absorb.auto_merged`: actual merge (canonical/absorbed/sha).
- `memory.absorb.skipped`: every non-merge decision with reason.
- `memory.absorb.reverted`: user undid an auto-merge (regret signal).

## Architecture decision — MEMORY.md / SOUL.md / USER.md stay outside entity-centric (2026-05-31)

**Context**: audit of the two `dream` / `memory_dream` crons (see
the doc fix in `docs/architecture/memory/` landed the same day)
surfaced that durin runs two parallel consolidation pipelines. The
legacy `dream` (every 2h) writes `MEMORY.md` / `SOUL.md` / `USER.md`
plus creates skills under `skills/`; the entity-centric
`memory_dream` (daily 3am) writes `memory/entities/<type>/<slug>.md`.
Both are load-bearing.

**Question raised**: should `MEMORY.md` / `SOUL.md` / `USER.md`
eventually migrate to the entity-centric model so durin runs a
single track?

**Decision (Marcelo, 2026-05-31): NO — keep the two tracks
permanently separate.**

**Reasons**:

1. **These three files are the *core* of the agent's identity and
   working memory**, not knowledge graph nodes. They sit in a
   different conceptual layer than the entities the agent acquires
   about its world.
2. **No reason to share with the rest of the entity model.** A core
   like SOUL.md isn't usefully related to `person:marcelo` or any
   other graph node. Mixing them dilutes both layers.
3. **They should NOT be findable by `memory_search`.** Search is for
   facts the agent retrieves contextually; the cores are loaded
   unconditionally into every system prompt. If a search could turn
   them up, the agent might surface them as evidence in a reply,
   confusing identity with knowledge.

**Implication for the two `dream` jobs**: the two-track design is
not technical debt. The legacy `dream` is the maintainer of the
core layer (identity, working memory, escalated skills); the
entity-centric `memory_dream` is the maintainer of the knowledge
graph. Both stay.

**Implication for naming (deferred)**: the "legacy" label on
`dream` is misleading — it implies it should be deprecated. Better
names would be `reflect` (hourly active reflection over current
sessions) and `dream` (nightly deep consolidation of the knowledge
graph). The rename of code / config / cron IDs is a separate item
for when we're more mature; for now only the display labels in the
UI get clarified.

**Lesson — layer boundaries are deliberate**: when a system shows
two parallel pipelines, the first instinct is "consolidate into
one." But sometimes the parallelism reflects a real semantic
boundary that the unified design would erase. Before merging
parallel systems, name the boundary and check whether it should
survive — here, "identity + working memory" vs "knowledge graph
about the world" is a boundary we want.

---

## Web log viewer — alternatives discarded (2026-06-03)

When building the dashboard log viewer (Settings → Logs, two read-only
tabs over the gateway JSONL log and the existing telemetry JSONL), several
tempting designs were considered and rejected. The shipped design is
documented in `superpowers/specs/2026-06-03-logs-viewer-lifecycle-design.md`
and `superpowers/plans/2026-06-03-logs-viewer-lifecycle.md`; this records
the *roads not taken*.

- **Exposing telemetry retention as web config.** *Discarded.* Telemetry
  has its own deliberate lifecycle (`durin/telemetry/retention.py`: compress
  at 30d, delete at 90d) and the self-managing agents consume it to make
  decisions. The viewer reads it but never mutates it; its knobs are not
  surfaced. **Lesson:** a data store that feeds autonomous decisions is not
  a user-tunable surface — read it, don't expose its dials.

- **A single unified log+telemetry stream.** *Discarded.* The two sources
  share only a timestamp; merging them into one chronological stream forces
  a lowest-common-denominator (timestamp + text) that erases telemetry's
  typed structure and breaks the category filter. Kept as two tabs over one
  shared read primitive. **Lesson:** don't unify streams whose only common
  axis is time; you pay structure to buy a correlation that isn't there.

- **An FTS/SQLite index over logs for search.** *Discarded.* Logs are
  append-only and time-ordered, so the filesystem layout *is* the time
  index. Newest-first reads + a `before_ts` cursor + grep-before-parse +
  a bounded scan window give interactive search whose cost scales with the
  *page*, not the *corpus* — at zero index infrastructure. **Lesson:** for
  recent-biased, time-ordered data, the filesystem is the index; an FTS
  layer is write-time cost and staleness for value you don't need yet.

- **Daily compression trigger (in addition to size).** *Discarded.* loguru
  takes a single rotation trigger; size-based rotation (5 MB → gz) already
  bounds segments. A separate daily pass is a second mechanism for the same
  end and needs custom rotation code. **Lesson:** one trigger that already
  bounds the thing doesn't get a second trigger "to be safe."

The one factual correction worth recording: an early exploration claimed
telemetry "accumulates forever, never cleaned." That was **wrong** —
`retention.py` has compressed+deleted on the health-check tick since P7.2.
The design decision above (don't touch telemetry) rests on the corrected
fact. **Lesson:** verify a subsystem's actual lifecycle in code before
designing a feature that assumes its absence.


---

## Post-migration audit: legacy dream cluster removed, two-track model settled (June 2026)

The entity-centric migration deleted the old consolidation stack; a meticulous
post-migration audit (A1-A5, B1-B4, N1-N9, C1-C3 — record in
`docs/qa/post_migration_audit_2026-06.md`) then closed the gaps and locked the
decisions below. **Read before re-proposing any of these.**

### Discarded (removed, with rationale)
- **The DreamConsolidator / DreamRunner cluster + the JSON-Patch apply pipeline +
  `threshold_trigger.py`.** Replaced by four passes of the single `memory_dream`
  cron (extract / refine / skill / always_on) writing through `memory_writer`'s
  git-CAS field patches. *Why threshold died:* 800-doc bench bursts made per-write
  LLM dispatch unviable (see `project_ingest_dormant_rationale`); any re-enable
  needs an explicit throttle.
- **The per-entity `dream_processed_through` cursor** (N3). The earlier model
  folded an entity's fragments into its page and used the cursor to "graduate"
  consolidated fragments out of the hot layer. *Why removed:* the redesign is a
  **two-track model** — entity pages (consolidated, built from SESSIONS + agent
  authoring) and raw fragments (`/remember` facts + session summaries) that are
  NEVER folded into pages. Nothing consolidates fragments, so nothing advances a
  per-entity cursor. Don't reintroduce it without first reintroducing fragment→page
  consolidation.
- **`existing_uris` anti-dup in the extract prompt** (A5). The extract dream
  enriches entities BY the agent's explicit `memory_upsert_entity` ref (it never
  creates from scratch), so it can't introduce the duplicate `existing_uris` would
  prevent; dedup is the agent's search-first instruction + the refine merge.
- **The legacy working-memory Dream (`MEMORY.md` / `USER.md` injection + the 2h
  `memory.py::Dream` phase-1/phase-2 over `history.jsonl`).** The agent's identity
  now lives in the static `SOUL.md` bootstrap + the pinned principal entity;
  standing behavioural guidance is the `always_on` pin (A4), curated by the dream
  within a token budget. There is now exactly ONE dream cron (`memory_dream`).

### Kept / decided
- **Relation cap is alert-only** (A3): crossing soft/hard emits telemetry + logs
  but never blocks a write or drops a relation. Enforcing the hard reject is a
  one-line flip if mega-hubs ever prove real.
- **No fragment auto-archive** (N4): episodic is the user's raw track
  (`/remember`); auto-archiving would destroy explicit user memory. Deletion is
  manual (`memory_forget`, reversible to `memory/archive/`). A size/age cap — not
  auto-archive — is the lever if volume ever matters.
- **Lesson (N1/N2/N8):** the live end-to-end + headless-webui checks caught three
  real gaps the unit suite + doc-rewrite missed (hard-reset clobbering hand edits;
  nothing embedding entity pages reactively; `rebuild_from_workspace` skipping
  references). Verify the *whole path live* before declaring a subsystem done.

## Memory-graph clustering / communities (Fase 3) — discarded as superseded (2026-06-07)

- **What it was**: the planned Fase 3 of the memory-graph webui redesign (backlog P8): GraphRAG-style community detection (Leiden) over the global graph + LLM map-reduce community summaries + collapsible super-nodes, to keep the force-directed view navigable as the graph grows past a few hundred nodes (the "hairball").
- **Why it was tried (planned)**: when scoped, the only ways to read the graph were the global overview (a hairball at scale) + type filters. Clustering looked necessary to tame density.
- **What was learned**: the same redesign shipped an **ego-graph focus mode** (`build_entity_subgraph`, uncapped, centred on the clicked node — Obsidian's local graph) plus **search→click brings any node (even capped) into focus**, **type-filter chips**, and the **500-node weight cap**. Together these cover the actual need — "explore the graph without drowning in the hairball" — by making the global view a launch point you immediately focus *out of*, not a surface you must comprehend whole.
- **Why it was discarded**: the navigability value clustering would add is already delivered by the ego-graph; the remaining delta (community boundaries + LLM summaries) is disproportionate — a community-detection algorithm + recurring LLM summarisation cost + cluster-rendering UI — for marginal benefit, and untestable/irrelevant at durin's current scale. Per the backlog convention, an item whose use case is covered by existing surfaces is **discarded with rationale**, not deferred.
- **Lesson**: when a later-shipped feature (ego-graph focus) absorbs the premise of a planned one, re-evaluate instead of building on inertia. The hairball is a *landing-view* problem; "focus on demand" beats "summarise everything up front." Re-open only on a concrete failure — e.g., the landing overview itself becomes unusable at scale *with* focus available.

## Last updated: 2026-06-07 (memory-graph P8: Fase 3 clustering discarded as superseded by ego-graph focus)
