# Roadmap

> Forward plan after empirical refutation of the previous "smart layer" direction. See `02_bitacora.md` for what was discarded and why.

---

## Current state (post-prune, 2026-05-18)

Durin is essentially a clean Nanobot baseline plus:
- Plumbing additions (`local_llama_provider`, multi-channel work, generic telemetry skeleton)
- Empirically validated execution: basic ReAct loop + tools + sandbox + sessions
- No active "smart layer" (posture, plan tiers, deliberation V3 all removed)

The codebase is ~2,500 lines lighter and 3,052 tests pass.

---

## Direction: two horizons backed by industry evidence

Both directions have strong empirical or industrial precedent, unlike what we built before.

### Horizon 1 — Task-aware context (dynamic SOUL.md / skill loading)

**Evidence base** (see `04_agent_strategies_catalog.md` and `06_log_experiments.md` for sources):
- Aider edit-format A/B: same model, different system-prompt wording → **+33-41 points** on 133 Exercism Python exercises
- PartialOrderEval (arxiv 2508.03678): same model, varying prompt specificity → **+58 points** on HumanEval (0.28 → 0.86)
- Hermes Agent: dynamic skill-document loading → **+40% speedup** on task completion
- Cursor's `.cursorrules` and Claude Code's `CLAUDE.md`: market convergence on rich system context

**Hypothesis to test**:
A **task-classified library of SOUL.md fragments** beats a **single static rich SOUL.md** for diverse tasks. The novelty of Durin's failed posture vector was *dynamic state-driven behavior modulation*; the novelty worth testing is *dynamic task-driven context selection*.

**What to build**:
1. A simple task classifier (LLM call at goal start: "what kind of task is this?")
2. A library of SOUL.md fragments per task category (e.g. "bug fix", "feature", "refactor", "review", "research")
3. Auto-injection of the relevant fragment into the system prompt

**How to evaluate**:
Cannot use scenarios where baseline already scores 5/5 — ceiling effect kills signal (see lessons in `02_bitacora.md`). Use Aider's published benchmark methodology (Exercism Python exercises) or build a benchmark with task variety where the baseline has measurable variance.

**Why this is different from what we tried**:
- Posture (refuted) was *thin abstract phrases* triggered by *runtime events*
- Dynamic SOUL.md is *rich concrete content* triggered by *task classification at start*
- Same general idea ("inject relevant context"), completely different implementation that maps to validated industrial pattern

---

### Horizon 2 — Memory system (graph-based, dynamic projection)

**Evidence base**:
- Hermes Agent skill loop (solve → document → reuse): **+40% speedup**, validated in production
- Aider's PageRank repo map: validated by adoption and benchmark results
- Reflexion (academic): episodic failure memory measurably improves recovery
- Doc 03 design predates our experiments; the design is internally consistent and aligns with these patterns

**Hypothesis to validate during construction**:
A persistent graph with role-typed nodes (session, goal, pending, step, milestone) + dynamic projection biased by relevance → better cross-task and cross-session performance than session-scoped context alone.

**What to build** (see `03_memory_design.md` for the full design):
- Five node types per Doc 03 §3.2
- Importance-based milestone promotion at step exit
- Dynamic context projection (which milestones enter the active window)
- Cross-session persistence with decay

**Refinements based on industry research**:
- Adopt Hermes's pattern of *agent-written skill documents* alongside agent-written milestones
- Consider Aider's PageRank-style relevance ranking for code-related milestones
- Reflexion-style explicit failure-pattern tracking

**Decision rule for memory features**:
Each subcomponent must demonstrate measurable lift before we layer more on top. We will NOT build the full graph and then check if it works — we'll build minimum-viable retrieval (Hermes-style flat skill docs), measure, then incrementally add structure.

---

## What we are explicitly NOT doing

These have been tested or have strong reasons against. See `02_bitacora.md` for full rationale.

- ❌ Posture vector (5-axis dynamic behavioral state)
- ❌ Plan tiers / phases / forced verification gate / cycle escalation
- ❌ Deliberation V3 (single-call multi-perspective in one model)
- ❌ Phase-aware temperatures
- ❌ Self-verification / self-review loops (same-model)
- ❌ Pre-completion Critic (without genuinely different model)

---

## Sequencing

**Phase 1 (Horizon 1 — Task-aware context)**:
Lower risk, lower cost, faster to test. Implement task classifier + SOUL.md library. Test against Aider-style benchmark. If +5pts measurable, ship. If not, learn why and pivot.

**Phase 2 (Horizon 2 — Memory)**:
Higher investment, higher potential differentiation. Start once Phase 1 has shipped or definitively concluded. Build incrementally: start with flat skill docs (Hermes-style), then add structure as evidence warrants.

These are independent — Phase 2 doesn't depend on Phase 1 succeeding. They can also run in parallel if resources allow.

---

## Decision rules (carried over from bitácora lessons)

1. **No component without empirical or industrial precedent.** "It seems like it should help" is not enough.
2. **Mechanisms must demonstrably activate in realistic tests.** If the main code path never runs, the component is overhead.
3. **Distrust same-model self-verification.** Need ground truth (tests) or different models.
4. **Specificity > abstraction.** "Be cautious" doesn't change behavior; concrete rules do.
5. **3+ trials minimum** for any quantitative claim.
6. **Test in regimes where baseline can fail.** Ceiling-effect scenarios prove nothing.

---

## Last updated: 2026-05-18
