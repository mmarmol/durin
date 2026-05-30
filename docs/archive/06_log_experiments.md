# Investigation Cycle Experiment Log

> Testing whether a structured "challenge" step improves agent investigation quality.

---

## Hypothesis

Adding a questioning/challenge step between investigation and planning would:
- H1: Improve exploration coverage (agent reads more relevant files)
- H2: Improve fix quality (agent produces better solutions)

This was motivated by the daily experience: the agent declares "done" with insufficient exploration and shallow acceptance criteria.

---

## Experiment Design

Two scenarios with known bugs requiring multi-file understanding:

**Scenario 1 — Notification cache bug**: Users don't receive emails after updating their address. Root cause is in `user_service.py` (missing cache invalidation), but the obvious file to check is `sender.py`. Requires reading `preferences.py` (caching layer) and `user_service.py` (profile updates) to find the real fix.

**Scenario 2 — Invoice tax integration**: `generate_invoice()` uses hardcoded 10% tax. Complete fix requires: regional tax rates from `tax_rules.py`, tax exemptions via `is_tax_exempt()`, AND discount processing from `discounts.py` (discounts apply BEFORE tax). A fix that only swaps the tax rate is incomplete.

Three conditions tested:
- **Baseline**: Bug report + one obvious file → propose fix
- **Challenge (multi-turn)**: Same start → challenge prompt ("what haven't you checked?") → agent identifies gaps → show additional files → fix
- **Integrated (single-call)**: Bug report + file + challenge instructions in same prompt → fix
- **Full context**: Bug report + ALL files → fix (upper bound)

Model: glm-5.1 (754B MoE) via Z.ai API.

---

## Results

### V1: Multi-turn challenge (3 sequential LLM calls)

| Scenario | Baseline | Challenge | Delta |
|---|---|---|---|
| notification_cache | 5/5 | 5/5 | 0 |
| invoice_tax | 3/5 | 1/5 (empty response) | -2 |

Challenge correctly identified ALL hidden files in both scenarios. But the multi-turn conversation degraded output quality — scenario 2 produced an empty final response after 3 rounds of analysis.

### V2: Single-call integrated challenge + full context upper bound

| Scenario | Baseline | Integrated | Full Context |
|---|---|---|---|
| notification_cache | 5/5 | 3/5 | 5/5 |
| invoice_tax | 3/5 | 2/5 | 2/5 |

---

## Proven

1. **Challenge identifies exploration gaps (H1 supported)**. In 100% of cases across both experiments, the challenge step correctly identified all hidden files and raised specific, actionable concerns about unverified assumptions. In scenario 2, the challenge always detected the discount issue that baseline missed.

2. **Challenge competes for tokens with the fix**. When analysis and fix share one response, the model spends tokens on analysis and the fix comes out incomplete (scenario 1: 5→3, scenario 2: 3→2). The challenge works but cannibalizes the output.

3. **More context does NOT automatically improve fix quality**. Full context in scenario 2 scored 2/5 — the LLM saw `discounts.py` entirely and still didn't integrate discount processing. This is the same finding as SWE-bench V6: the bottleneck is synthesis/comprehension, not exploration.

## Disproven

1. **A single "look at more files" cycle does NOT improve one-shot fixes (H2 not supported)**. The model can identify what it's missing, but seeing more code doesn't mean it integrates that code into the solution. This was consistent across both experiments and all delivery formats.

---

## Analysis: Why More Context Doesn't Help

The model's failure mode is NOT "I didn't see the relevant code." It's "I saw the code but didn't synthesize a multi-file solution." This is a fundamental limitation of one-shot fix proposals:

- The model reads `discounts.py`, sees `apply_discount()`, understands it conceptually
- But when generating the fix for `invoice.py`, it focuses on the primary ask (tax rates) and doesn't integrate the secondary concern (discounts before tax)
- This is cognitive load / attention allocation, not information deficit

This mirrors the SWE-bench V6 conclusion: 6/9 failures were comprehension problems, not exploration problems. The agent layer (posture, planning, deliberation) cannot fix what the model fundamentally doesn't synthesize.

---

## Implications for Durin

### What works
- **Structured questioning as exploration guide**. The challenge prompt is effective at producing a list of "things to investigate." This is useful for directing the INVESTIGATE phase.
- **The current fast-path (EXECUTE → VERIFY → loop)**. Iterative execution with verification IS the right architecture. The model performs better making one change, verifying, then making another — not planning everything upfront.

### What doesn't work
- **Pre-planning cycles**. Adding investigate → question → investigate → criteria → plan adds latency without improving outcomes. The model doesn't use extra context effectively in one-shot mode.
- **Challenge-in-same-call**. Cannibalizes response tokens.

### Where the real value is
The challenge would be most useful in **the retry path** — when VERIFY fails and the agent needs to re-investigate. The current `_RETRY_SELF_EVAL` prompt is generic ("Do you have a genuinely DIFFERENT approach?"). Replacing it with a structured challenge ("What files didn't you check? What assumptions were wrong?") would direct the re-investigation more effectively.

The fundamental problem (shallow synthesis of multi-file solutions) requires **iterative execution**, not better pre-planning. The agent should:
1. Fix one thing → verify
2. If verify reveals another issue → fix that → verify
3. Repeat until all acceptance criteria pass

This is what the current cycle system already does. The improvement opportunity is in making EACH cycle more targeted, not adding pre-cycles.

---

## Files

- Test scenarios: `scripts/hypothesis_test/scenario_1/`, `scripts/hypothesis_test/scenario_2/`
- V1 experiment: `scripts/hypothesis_test/run_experiment.py`
- V2 experiment: `scripts/hypothesis_test/run_experiment_v2.py`
- Raw results: `scripts/hypothesis_test/results.json`, `scripts/hypothesis_test/results_v2.json`

---

## Cross-reference: What Production Agents Do in This Phase

After the experiment, we surveyed how leading production agents handle the execution/process phase (before memory). Findings:

| Agent | Innovation in execution phase | Category |
|---|---|---|
| **SWE-agent** (Princeton) | Agent-Computer Interface (ACI): windowed file viewer, capped search results, linter gate that rejects syntactically invalid edits | Tool interface |
| **Aider** | PageRank repo map (tree-sitter), 13 model-specific edit formats, generate-test-repair inner loop | Context + edit format |
| **Cursor** | Shadow workspace — applies edits in hidden VS Code window, runs lint, reports diagnostics without touching user files | Speculative verification |
| **Devin** (Cognition) | Multi-model architecture with dedicated Critic that reviews before execution. DAG planning with dynamic re-planning | Multi-model adversarial |
| **Moatless Tools** | Full MCTS with reward backpropagation, multi-agent debate (SWE-Agent + Value Agent + Discriminator) | Tree search exploration |
| **OpenCode** | Dedicated read-only `plan` agent vs full-access `build` agent | Phase-specific agents |
| **Codex CLI** (OpenAI) | Stateless requests + encrypted compaction for ZDR; prompt-caching architecture | Context efficiency |

Cross-cutting finding: **no production agent uses dynamic behavioral weights** (Durin's posture vector). Hermes Agent's RL-based self-improvement is the closest, but it operates offline. Devin's Critic is binary, not weighted. This is an open design space — but also unproven.

Production agents that ARE working in this phase focus on three categories:
1. **Interface design** (SWE-agent ACI, Aider edit formats)
2. **Verification architecture** (SWE-agent linter gate, Cursor shadow workspace, Devin Critic)
3. **Multi-model orchestration** (Devin, Moatless)

What Durin built that matches industry patterns: plan tiers, forced verification, fast-path. These have value.

What Durin built that is novel and unproven: posture vector (V6 = 0pp), single-call multi-perspective deliberation. These need either evidence or pruning.

---

## Component Value Assessment

| Component | Real value | Evidence |
|---|---|---|
| Plan tiers (DIRECT/PLAN) | High | Standard industry pattern |
| Forced verification (complete_goal blocked) | High | Matches SWE-agent linter gate, Cursor shadow, Devin Critic |
| Fast-path EXECUTE→VERIFY | High | Experiment confirmed correct architecture |
| Phase temperature | Moderate, unproven | Unique to Durin, no evidence either way |
| Posture vector | No evidence | V6 = 0pp delta. No production agent uses behavioral weights |
| Deliberation V3 (single-call multi-perspective) | No evidence | Challenge identifies gaps but multi-turn breaks output. Single-call cannibalizes fix tokens. Devin's multi-perspective works but uses DIFFERENT models, not one model role-playing |

The pattern: what has industry analogues has value. What's novel is unproven.

---

## Next Investigation Candidates (this phase, pre-memory)

Four candidates ordered by cost-benefit:

### Candidate 1 — Pre-completion adversarial review (Devin Critic pattern)
**What**: Before `complete_goal` succeeds, a separate LLM call reviews the full work against the original goal with clean context.
**Why**: Directly addresses the daily pain point: "you said done, but you missed X."
**Mechanism**: New hook intercepts `complete_goal`; builds context (goal + work done + verification results); asks "does this fully satisfy the original request? what's missing?"; if gaps identified, returns to investigation with structured feedback.
**Test**: Run on scenario 2 — does it catch missing discount integration?
**Cost**: 1 extra LLM call at completion. **Low.**

### Candidate 2 — Acceptance criteria generation pre-investigation
**What**: At goal start (PLAN tier), generate explicit acceptance criteria from the user's request, BEFORE investigation contaminates them.
**Why**: Current VERIFY only checks exit code 0. With explicit criteria, VERIFY has something concrete to compare against. Also captures implicit requirements early.
**Mechanism**: New goal start hook in PlanHook; one LLM call producing structured criteria; criteria become the contract VERIFY checks.
**Test**: Compare criteria depth/coverage with and without; check if Candidate 1 + Candidate 2 together catch scenario 2.
**Cost**: 1 extra LLM call at start. **Low.**

### Candidate 3 — Better tool interface (SWE-agent ACI)
**What**: Windowed file viewer (N lines at a time), search tool with capped results + structured summaries, edit by explicit line ranges with validation.
**Why**: SWE-agent's primary finding was that interface design matters more than loop architecture.
**Mechanism**: Refactor `read_file`, `grep`, `edit_file` tools.
**Test**: Compare agent performance on multi-file tasks; measure tool call efficiency.
**Cost**: Refactor multiple tools, integrate with file_state. **Medium.**

### Candidate 4 — Shadow verification (Cursor pattern)
**What**: Apply edits in a shadow workspace (via bwrap), run linter/tests there, report diagnostics without touching user files.
**Why**: Catches errors before the user sees them; enables speculative iteration.
**Mechanism**: Extend bwrap sandbox to support shadow workspaces with diff overlay; new `verify_shadow` tool.
**Test**: Measure error rate of committed changes; latency impact.
**Cost**: Sandbox state management, diff merging. **High.**

---

## Decisions

1. **Posture vector stays for now but is on probation**. No production precedent, no V6 benefit. If Candidate 1 or 2 require posture integration, it earns its keep. Otherwise it should be considered for removal in a future cycle.

2. **Deliberation V3 also on probation**. The challenge prompt has value as exploration guide (proven). The synthesis/merge does not. Consider stripping V3 back to just the challenge-as-exploration-guide pattern, applied in the retry path (replacing the generic `_RETRY_SELF_EVAL`).

3. **Plan + Forced Verify confirmed as core**. These have industry validation and experimental support.

4. **Recommendation: Test Candidate 1 first.** It's cheap, targets the user's actual daily pain, and is testable with the existing scenario 2. If it works, Candidate 2 stacks on top naturally. Candidates 3 and 4 are larger investments that should wait until the cheap experiments report.

5. **Memory graph (Doc 03) remains the largest open opportunity** — bigger than anything in this phase. But it's a multi-week build. In parallel, the cheap candidates here can be tested.

---

## Open Question

The experiment used SINGLE-SHOT fix proposals. A real multi-iteration agent would behave differently:
- Iteration 1: read invoice.py → propose fix
- If Candidate 1 catches "missing discounts" → iteration 2: read discounts.py → revise fix
- Verify → done

The challenge step that "failed" to improve one-shot fixes may succeed when there's room to iterate. Worth testing in the agent runtime, not just isolated LLM calls.

---

## Final Experiments: V3 / V4 / V5 / V6

After the initial exploratory tests, the hypothesis was tested in a proper multi-scenario, multi-trial framework. Conclusions are now data-backed.

### Methodology

- 3 scenarios with distinct failure modes:
  - `scenario_2`: multi-file integration (agent must read & use 3 helper modules)
  - `scenario_3`: wrong root cause (obvious fix is wrong; real cause is upstream)
  - `scenario_4`: implicit security requirement (auth/authz not in literal task)
- 2 trials per condition per scenario
- temperature=0 throughout (after research showed industry consensus is 0.0–0.3)
- Judge fixed to look at all edited files (V5 caught a hardcoded-`invoice.py` bug)
- glm-5.1 via Z.ai (one model — see Limitations below)

### Results (V5: A vs baseline; V6: B vs baseline)

```
Per-scenario average scores:

Scenario                       Baseline  V3 Critic  V4 Criteria  V6 Self-Review
scenario_2 multi-file           4.50      2.50        3.00         4.50
scenario_3 root cause           5.00      5.00        3.00         5.00
scenario_4 implicit security    5.00      5.00        5.00         5.00

Global avg                      4.83      4.17        3.67         4.83
Delta vs baseline               —         -0.66       -1.16        0.00
```

(V5 baseline different from V6 baseline due to API drift between runs; per-scenario deltas inside each experiment are the meaningful comparison)

### Critic activity

- V3 Critic rejections: 2 out of 12 trials (17%) — both in scenario_4, no measurable effect
- V4 Critic+Criteria rejections: 0 out of 12 trials (0%)
- V6 Self-review triggers: 12 out of 12 (100% fired) — 0 score improvements

### Refutations

**Candidate 1 (Generic Critic with clean context) — REFUTED**
- 17% rejection rate, no impact when it rejected
- Without criteria, the Critic doesn't know what to demand
- Same model = same blind spots; clean context doesn't fix it

**Candidate 2 (Critic + Acceptance Criteria) — REFUTED, ACTIVELY HARMFUL**
- 0% rejection rate
- Average score DROPS by 1.16 points vs baseline
- The narrow criteria constrain the agent's exploration: scenario_3 baseline fixes root cause (5/5); with_criteria fixes only the symptom because the criteria literally said "make get_price not return negatives" (3/5)
- The criteria generator has the same blind spots — it generates literal criteria from the task and misses implicit requirements

**Camino B (Self-Review Loop) — REFUTED**
- 100% trigger rate, 0% effect
- The agent walks through structured self-reflection questions and answers them, but the answers always confirm completion
- Adds 2-4 iterations of latency without quality change
- Confirms the academic finding: same-model self-verification doesn't catch own gaps

### What This Means

Three different ways of forcing the agent to verify itself before declaring completion all failed. The pattern is consistent: when the model CAN see the problem, it solves it without help (baseline 4.83). When it can't, no amount of process intervention reveals it. The constraint is model capability, not agent process.

Concrete corollary: **Durin's current Deliberation V3 (single-call multi-perspective) is structurally equivalent to V6 self-review.** Same model, structured prompt, asking itself to consider perspectives. The V6 result is direct evidence that this kind of intervention adds latency without measurable quality gain.

### Limitations of these experiments

- Single model (glm-5.1) — Claude or GPT may behave differently
- 2 trials per condition per scenario is exploratory, not confirmatory (N=6 per condition globally)
- 3 scenarios cover code refactoring tasks only — no non-code, no conversational
- Judge is also an LLM with its own variance
- API-side non-determinism even at temp=0 (between-day baseline averages ranged 3.67–4.83 for the same prompt)
- Scenarios were designed to test specific failure modes; results may not generalize

### Where investment should go instead

Based on this evidence + the production agent catalog (`04_agent_strategies_catalog.md`), the directions with actual track record:

1. **Memory system (Doc 03)** — Hermes's skill loop shows 40% speedup. Reflexion-style failure memory addresses recurring blind spots structurally.
2. **Tool interface (SWE-agent ACI pattern)** — windowed file views, capped search, edit-by-range. Concrete refactors of existing tools.
3. **Multi-MODEL verification (Devin pattern)** — a verification model that is GENUINELY different (different family, different training) from the executor. Our experiments all used same-model verification, which the data refutes.
4. **Generate-test-repair automation (Aider pattern)** — auto-run tests after edits; feed errors back. Already partially present in Durin's fast-path; just needs to be more automatic.

### Existing Durin components reassessed against this evidence

| Component | Reassessment after experiments |
|---|---|
| Plan tiers (DIRECT/PLAN) | Still validated — industry pattern, works |
| Forced verification (complete_goal blocked) | Still validated — Cursor / SWE-agent / Devin pattern |
| Fast-path EXECUTE→VERIFY | Strongly validated — agent baseline is solid when loop is clean |
| Posture vector | Still no evidence. V6 was a structurally similar test (same-model behavior modulation) and it failed. Should be considered for removal unless tied to memory in Doc 03 |
| Deliberation V3 (single-call multi-perspective) | Refuted by analogy. V6 is essentially the same intervention at completion time. If the deliberation V3 mechanism were valuable, V6 would have shown gains. It didn't. |
| Phase temperature | Still no direct test. Per industry research, single low temp (0.0–0.15) is the consensus. Phase variation has no production precedent and remains unvalidated |

---

## Last updated: 2026-05-18

---

## V9: Aider Exercism — first POSITIVE signal (2026-05-18)

After V3–V8 refuted every "smart layer" we tried, V9 is the first
experiment to validate a forward direction empirically. We tested whether
system-prompt specificity (SOUL.md content) affects model performance on
a standard coding benchmark — Aider's Exercism Python suite.

### Setup

- **Model**: glm-5.1 via Z.ai (temp=0)
- **Benchmark**: 30 exercises sampled from `exercism/python` `exercises/practice/` (seed=42), the same source Aider uses for its 133-exercise benchmark
- **Edit format**: whole-file (model returns complete file content in a fenced code block, filename on line before fence)
- **Evaluation**: copy exercise to temp dir (excluding `.meta/`), apply model's edits, run pytest with 60s timeout, exit code 0 = pass
- **Trials**: 1 trial per (exercise, condition) — single-shot, no retry. 90 total runs.
- **Harness**: `scripts/hypothesis_test/run_experiment_v9_soul.py` (standalone, not using Durin's AgentLoop — clean isolation of the system-prompt variable)

### Three conditions (only the system message varies)

```
A) none      — no system message at all
B) generic   — single sentence: "You are a careful Python engineer.
                Write clean, idiomatic Python that follows PEP 8."
C) specific  — five-rule block focused on correctness: read carefully,
                handle edge cases, follow spec literally, std-lib only,
                verify against examples
```

User prompt (identical across conditions) contains the problem
instructions, the stub file contents, and the output-format requirement.

### Results

| Condition | Pass rate | Passed | Errors (max-token hits) | Avg output tokens |
|---|---|---|---|---|
| none | 66.7% | 20/30 | 8 | 2607 |
| generic | 80.0% | 24/30 | 5 | 2195 |
| **specific** | **86.7%** | **26/30** | **1** | **1807** |

**Pass-rate deltas**:
- specific vs none: **+20 pp**
- specific vs generic: +6.7 pp
- generic vs none: +13.3 pp

The +20 pp delta is in the lower half of Aider's published GPT-4 range
(+33–41 pp), but the rank order (specific > generic > none) replicates
their finding cleanly on a different model (glm-5.1).

### Three independent signals support the same conclusion

| Dimension | none | specific | Direction |
|---|---|---|---|
| Pass rate | 66.7% | 86.7% | +20 pp |
| Avg output tokens | 2607 | 1807 | −31% (more concise) |
| Max-token errors | 8/30 | 1/30 | −87% (stops rambling) |

The specific-SOUL agent doesn't just pass more — it produces shorter
code and stops looping into the max-token cap. That's quality signal
that pass/fail alone would miss.

### Divergence pattern (9/30 exercises had different outcomes)

```
crypto-square     none=F  generic=F  specific=T
error-handling    none=F  generic=T  specific=T
isbn-verifier     none=F  generic=T  specific=T
luhn              none=T  generic=F  specific=T   <- non-monotone
linked-list       none=T  generic=T  specific=F   <- only case specific lost
perfect-numbers   none=F  generic=T  specific=T
say               none=F  generic=T  specific=T
word-count        none=F  generic=T  specific=T
zipper            none=F  generic=F  specific=T
```

Seven of nine divergences follow the monotone ordering. `luhn` and
`linked-list` are the cases to study.

### Limitations of this V9

- Single trial per cell — no within-condition variance estimate.
- 30 exercises subset of 140 — sample noise possible.
- The "specific" prompt was written by us, not by Aider. We have not yet
  tested whether Aider's own prompts produce the same gap on glm-5.1
  (that is V9b).
- Single model (glm-5.1) — Claude / GPT-4 sensitivity may differ.
- We don't capture the actual generated code in this run — only token
  counts and pytest output. Code-quality judgments require a follow-up
  with response capture (V9b).

### Why this matters

V9 is the first experiment in the V3–V9 series to produce a clear
positive signal. The direction is now backed by:

1. Our own data (+20 pp on glm-5.1, 30 exercises)
2. Aider's published data (+33–41 pp on GPT-4, 133 exercises)
3. PartialOrderEval academic paper (+58 pp on HumanEval, Qwen2.5)
4. Hermes Agent's skill-loop deployment (+40% speedup)
5. Industry convergence (Cursor `.cursorrules`, Claude Code `CLAUDE.md`)

This validates Horizon 1 of the roadmap (`roadmap.md`). The next
step is V9b: capture the actual code generated, compare with Aider's
own prompts as a literal-replication anchor, and assess code-quality
deltas beyond pass/fail.

### Files

- Script: `scripts/hypothesis_test/run_experiment_v9_soul.py`
- Results: `scripts/hypothesis_test/v9_runs/results_v9_seed42.jsonl`
- Aider's prompts (downloaded): `scripts/hypothesis_test/aider_prompts/`


## V9e — SOUL routing closure (2026-05-19)

### Goal

Close the SOUL routing hypothesis (Phase 1a of `roadmap.md`) with statistical evidence. V9d had revealed the V9 +20pp signal was a `max_tokens=4096` artifact. V9e ran the corrected protocol (`max_tokens=131072`) on the complement of 110 exercises (the 30 from V9d already done, the remaining 110 here) to push N high enough to detect any real differential effect across conditions.

### Setup

- Model: glm-5.1 via Z.ai API, temp=0, `max_tokens=131072`
- 3 conditions: `none` (empty system prompt), `specific` (the 5-rule correctness-focused prompt from V9), `generic_agent` (a short generic engineering posture)
- 110 exercises × 3 conditions = 330 trials planned
- Single LLM call per trial, whole-file edits, pytest exit code as ground truth
- Resume capability, `reasoning_content` captured per trial, per-call telemetry of model output

### Execution notes

V9e completed 321/330 trials (107 exercises with full 3-condition data) before being terminated to wrap up the analysis. Two operational issues surfaced and were fixed mid-run:
- Initial `subprocess.run(timeout=60)` for pytest did not kill runaway child processes when the LLM emitted infinite-loop code. One trial (rest-api / specific) hung for 6.5 hours before the SDK timeout cleared it. Fix: rewrote `run_pytest` in `scripts/hypothesis_test/run_experiment_v9_soul.py` to use `subprocess.Popen(start_new_session=True)` + `os.killpg(SIGTERM→SIGKILL)` on timeout.
- A 20-minute LLM-side stall (legitimate, in the realistic range given some trials show >25min legitimate `llm_duration` with the 131k budget) prompted the second restart.

### Results

**Pass rate (N=107 complete exercises)**:

| Condition | Passed | Rate |
|---|---|---|
| none | 74/107 | 69.2% |
| specific | 76/107 | 71.0% |
| generic_agent | 79/107 | 73.8% |

Gap: 4.6pp top-to-bottom. Standard error for proportions at N=107 is √(0.7·0.3/107) ≈ 4.4pp. **The spread fits within ~1 standard error — empirically indistinguishable from noise.**

**Outcome distribution across the 3 conditions per exercise**:

- All 3 pass: 64/107 (59.8%)
- All 3 fail: 18/107 (16.8%)
- Divergent (some pass, some fail): 25/107 (23.4%)

**Statistical tests on the 25 divergent exercises**:

- **Chi-square** against uniform distribution across the 6 possible patterns (T,F,F)…(F,T,T): χ² = 1.78, df = 5, critical value at p=0.05 is 11.07. **Cannot reject H0** — pattern distribution is compatible with random.
- **Per-condition sign tests** (wins vs losses in divergent cases):
  - none: 10W / 13L (binomial two-sided p = 0.68)
  - specific: 10W / 13L (p = 0.68)
  - generic_agent: 14W / 9L (p = 0.41)
  None significant.

**Error types** (failed trials only):

| Error type | none | specific | generic_agent |
|---|---|---|---|
| AssertionError | 28 | 30 | 25 |
| ERROR (setup) | 1 | 1 | 2 |

Conditions fail in the same way. No type-of-error differentiation.

**Fail-set Jaccard similarity**:

- none ∩ specific: 0.590
- none ∩ generic_agent: 0.568
- specific ∩ generic_agent: 0.611

~59% overlap of failures — most are shared difficulty.

**Asymmetric patterns** (informational, not robust):

- Exercises only `none` passes (4): beer-song, food-chain, kindergarten-garden, proverb — 3 of 4 are lyrics / exact-text-format problems
- Exercises only `specific` passes (3): book-store, camicia, robot-name
- Exercises only `generic_agent` passes (5): grade-school, meetup, paasio, pov, satellite — most are class/data-structure problems

N=4/3/5 is too small to claim a pattern. With Bernoulli noise across 23 divergent cases distributed in 6 buckets, expected counts per bucket are ~3.83 — observed values are 2-5, all within the random envelope.

**Efficiency (the robust signal)**:

| Condition | Avg tokens_out | Median tokens_out | Avg reasoning_chars |
|---|---|---|---|
| none | 4,959 | 2,285 | 17,659 |
| specific | 2,337 | 744 | 7,595 |
| generic_agent | 1,981 | 432 | 6,279 |

Median output tokens: `none` is **5.3× larger than `generic_agent`** for the same correctness. Reasoning chars (internal CoT): `none` uses **2.84× more** than `generic_agent`.

### Conclusion

The SOUL routing hypothesis (Phase 1a) is closed without a positive correctness signal:

1. Pass rates within noise across the 3 conditions
2. Divergent exercises distribute as random across the 6 patterns
3. Error types are identical
4. Most failures are shared difficulty
5. Anecdotal asymmetric patterns are statistically compatible with noise

What survives:

- Any non-empty SOUL reduces median output tokens 3-5× and reasoning chars 2.84× without correctness cost.
- This is enough justification to ship a single default SOUL — but **not** to build a router.

### Decision

- Phase 1a: **closed**.
- Set a single generic-engineering SOUL as Durin's `ContextBuilder` default.
- Forward focus: Phase 1c (tool I/O hygiene + telemetry) and Phase 2 (memory, informed by Hermes' production design).

### Files

- Script: `scripts/hypothesis_test/run_experiment_v9e_complement.py`
- Results: `scripts/hypothesis_test/v9_runs/results_v9e_seed42.jsonl` (321 trials)
- Roadmap closure: `roadmap.md` §Horizon 1a
- Bitácora entry: `bitacora.md` §Discarded: Role-based SOUL.md routing
