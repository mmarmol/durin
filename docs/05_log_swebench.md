# Benchmark Log

> Evaluation history and direction for Durin's agent systems.

---

## SWE-bench (Discontinued — 2026-05-18)

### What We Tested

SWE-bench Lite: real GitHub issues from open source repos. The agent receives a bug description and must produce a patch that passes the repo's test suite.

- **Model**: glm-5.1 (754B MoE, Z.ai)
- **Runs**: V5 (5 astropy, external mode), V5b (5 astropy, docker-internal), V6 (10 mixed repos, docker-internal)
- **Conditions**: Nanobot (base), Durin posture-only, Durin posture + deliberation V2, Durin fast-path + post-error deliberation

### Key Results

| Benchmark | Durin | Nanobot | Delta |
|---|---|---|---|
| V5 (5 astropy, external) | 4/5 (80%) posture-only | 2/5 (40%) | +40pp |
| V5b (5 astropy, docker-internal) | 5/5 patches (plan always) | 5/5 patches (no plan) | Identical patches, +45s overhead |
| **V6 (10 mixed, docker-internal)** | **3/9 (33%)** | **3/9 (33%)** | **0** |

V6 was the most rigorous test (10 instances across 6 repos, docker-internal mode, SWE-bench eval). Both agents resolved the exact same 3 instances (astropy-12907, astropy-14995, django-14999).

### Why We're Discontinuing SWE-bench

**SWE-bench measures model capability, not agent capability.**

1. **The bottleneck is comprehension, not process**: 6/9 failed cases are instances where the LLM doesn't understand the code semantics well enough to produce a correct fix. Posture, planning, and deliberation can't fix what the model fundamentally doesn't know. Example: astropy-6938 requires understanding numpy chararray view semantics — both agents produce the same wrong fix.

2. **Fast problems don't need agent infrastructure**: For problems the LLM can solve (3/9), it solves them in 3-17 iterations regardless of whether posture/plan is active. The agent layer is pure overhead on these cases.

3. **Slow problems don't benefit from agent infrastructure either**: The 6 failed cases produce patches that look correct to the agent (no verification failure triggers), so the plan system's escalation and deliberation never activate.

4. **The benchmark doesn't test what Durin adds**: Durin's value proposition is behavioral adaptation (posture), structured execution (plan with verification), and recovery from errors (deliberation). SWE-bench instances are mostly "fix this one function" — they don't require multi-step planning, error recovery, or behavioral adjustment.

5. **V5 vs V6 discrepancy**: The apparent posture value in V5 (40%→80%) disappeared in V6 (33%=33%) when we expanded to more repos. The V5 result was likely within noise for N=5.

### What We Learned (Still Valid)

These findings from SWE-bench remain valuable for Durin's architecture:

- **Fast-path design is correct**: Always-plan adds ~45s overhead with identical results. Execute→verify first, escalate on failure.
- **Preventive deliberation is harmful**: Speculating about risks before investigating causes wrong advice (astropy-6938 case study). Post-error only.
- **Forced verification is essential**: Without it, the agent declares "done" without testing. Even though it didn't differentiate in V6, it's architecturally sound.
- **Carry-posture has drift bugs**: Fixed, but the fix was discovered through benchmarking.

---

## Next: Agent-Specific Benchmarks

SWE-bench tests "can the model produce the right patch" — a model capability question. We need benchmarks that test "can the agent follow a process, recover from errors, and use tools effectively" — agent capability questions.

### τ-bench (Tau-bench) — Sierra AI

**What it measures**: Agent performance on realistic conversational tasks with tool use, policy adherence, and error recovery.

**Why it's relevant to Durin**:
- Tasks require following multi-step procedures (exactly what the plan system enforces)
- Agents must adhere to domain policies (maps to posture's discipline axis)
- Error recovery is explicitly tested (maps to plan escalation + post-error deliberation)
- Tool use quality matters, not just final output (maps to posture modulating tool selection)

**Domains**: Airline customer service, retail operations — structured tasks with clear policies.

**What to evaluate**:
- Does posture (cautela) improve policy adherence?
- Does the plan system's forced verification prevent premature task completion?
- Does post-error deliberation improve recovery from mistakes?

### GAIA — Meta AI

**What it measures**: General AI Assistant capabilities requiring multi-step reasoning with tools (web search, calculation, file manipulation).

**Why it's relevant to Durin**:
- Level 2-3 tasks require genuine planning — can't be solved in one step
- Agents must decide which tools to use and in what order
- Intermediate verification is essential (wrong intermediate result cascades)
- Tasks span multiple domains (web, math, files) — tests tool selection

**Levels**: 1 (simple, ~1 step), 2 (moderate, 3-5 steps), 3 (hard, 5-15 steps with dependencies)

**What to evaluate**:
- Does the fast-path vs full-plan distinction help? (Level 1 = fast path, Level 3 = full plan)
- Does posture (exploration axis) improve tool selection diversity?
- Does deliberation help on Level 3 tasks where the first approach often fails?

### Investigation Plan

1. **Research**: Review τ-bench and GAIA setup requirements, evaluation harness, scoring metrics
2. **Feasibility**: Determine if Durin can interface with these benchmarks (tool compatibility, environment requirements)
3. **Pilot**: Run 5-10 instances of each to assess signal quality
4. **Full evaluation**: If pilot shows signal, run full benchmark comparing Durin vs Nanobot

---

## Last updated: 2026-05-18
