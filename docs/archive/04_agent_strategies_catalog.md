# Agent Strategies Catalog

> Reference catalog of strategies used by leading production and research agents, organized for future evaluation against Durin.

This document consolidates findings from the agent landscape research (May 2026). Each strategy is categorized, attributed, and tagged with applicability/cost estimates for Durin.

---

## How to use this document

When evaluating a future improvement to Durin:
1. Check the relevant **Category** section for prior art
2. Note which agents have validated the approach in production
3. Cross-reference with Durin's current state (column **Status in Durin**)
4. Use the **Cost/Value** column as a rough prioritization signal

---

## Category 1: Tool Interface Design (ACI)

The interface between agent and environment. SWE-agent's key finding: interface design matters more than loop architecture.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **Windowed file viewer** | SWE-agent | Read files in 100-line windows with line numbers, navigate via commands | ✅ Implemented (offset+limit, default 2000 lines) — **no telemetry yet** | Low / Medium |
| **Capped search results** | SWE-agent | Max 50 results, structured summary instead of dump | ✅ Implemented (head_limit, default 250) — **no telemetry yet** | Low / Medium |
| **Edit by line range** | SWE-agent | Edits target explicit line ranges with validation | Edit whole-file or by string match | Medium / High |
| **Linter gate (hard reject)** | SWE-agent | Syntactically invalid edits rejected with error feedback before commit | Soft (verify runs after) | Medium / High |
| **Model-specific edit formats** | Aider | 13 different edit formats, one per model family | Single format | High / Low (model-dependent) |
| **PageRank repo map** | Aider | Tree-sitter parse + dependency graph + symbol ranking + token-budget selection | None | High / Very High |
| **Shadow workspace** | Cursor | Apply edits to hidden VS Code window, run lint, report without touching user files | None | High / High |
| **Speculative edits** | Cursor | Use existing code as draft tokens for speculative decoding (13x speedup) | None | Very High / Medium (latency only) |

---

## Category 2: Verification Architecture

How agents confirm work is correct before declaring done.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **Forced verification gate** | Durin, SWE-agent, Cursor | `complete_goal` blocked until verify passes | ✅ Implemented | — |
| **Generate-test-repair loop** | Aider | Auto-run tests after edits, feed errors back automatically | Manual (agent must call) | Low / High |
| **Dedicated Critic model (multi-model)** | Devin | Separate model reviews before execution; binary pass/reject | None — single model | Medium / Unproven (V3 test inconclusive) |
| **Critic with explicit criteria** | (Hypothetical combo) | Critic receives acceptance criteria, not just code; checks each one | None | Low / Likely high |
| **Shadow verification** | Cursor | Run lint/tests in temp workspace, report diagnostics | None | High / High |
| **Multi-agent debate / Discriminator** | Moatless | Multiple agents debate, Discriminator selects | None — single-call deliberation | Very High / Low |
| **MCTS reward backpropagation** | Moatless | Branch + score + backprop; -100 to +100 rewards | None | Very High / Medium |

---

## Category 3: Context Engineering

How agents get the right information into the LLM's window.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **Repo map (PageRank)** | Aider | Importance-weighted symbol selection within token budget | None | High / Very High |
| **Codebase indexing (incremental)** | Cursor, Windsurf | Hash-based incremental file indexing | Basic | Medium / High |
| **Fast Context retrieval (SWE-grep)** | Windsurf | 10x faster context retrieval, proprietary | None | Very High / Medium |
| **Codemaps (AI-annotated)** | Windsurf | AI-annotated visual code navigation | None | High / Medium |
| **Encrypted compaction** | Codex CLI | `/responses/compact` returns encrypted summary preserving model's latent state | Basic compaction | Very High / Low (privacy-driven) |
| **Prompt caching (prefix optimization)** | Codex CLI, Anthropic | Static content before variable content for cache hits | Partial | Low / Medium |
| **Skill documents (self-generated)** | Hermes | Solve task → write reusable skill doc → retrieve on similar task | None | Medium / Very High (40% speedup reported) |
| **Memory as graph with role-typed nodes** | Durin (designed), Graphiti | Session/goal/pending/step/milestone nodes; dynamic projection | ❌ Designed in Doc 03, not built | Very High / Very High |
| **Recent-steps FIFO queue** | Many | Last N steps kept in full detail | Partial (basic history) | Low / Medium |
| **Importance-based milestone promotion** | Durin (designed) | Important steps promoted to milestones; rest archived | ❌ Not built | Medium / High |

---

## Category 4: Multi-Model Orchestration

Using multiple models with specialized roles.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **Planner / Coder / Critic split** | Devin | DAG planning, code generation, adversarial review — separate models | None — single model | Very High / Likely high |
| **Architect mode (plan with one, code with another)** | Aider | `/architect` uses two models: planner + implementer | None | Medium / Medium |
| **Read-only Plan agent + full-access Build agent** | OpenCode | Separate agents for exploration vs execution | Partial (phases, not separate agents) | Medium / Medium |
| **Sub-agent delegation** | OpenHands, Cursor | Hierarchical decomposition; parallel sub-agents | None | High / Medium |
| **Specialized SLMs as generators** | Durin (designed) | Pragmatic / Explorer / Critic SLMs propose; heavy LLM synthesizes | Partial (single-call role-play, not separate SLMs) | High / Unproven |
| **Browser model (separate)** | Devin | Dedicated model for browser interaction | None (Durin not a browser agent) | — |

---

## Category 5: Exploration Strategies

How agents decide what to look at.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **MCTS tree search** | Moatless | Full Monte Carlo Tree Search with reward backpropagation | None | Very High / Medium |
| **Multi-attempt retry** | Most | If fails, retry differently | ✅ Cycle restart | — |
| **Read-only plan agent** | OpenCode | Dedicated exploration phase before edits | Partial (INVESTIGATE phase) | Low / Medium |
| **Hierarchical structure-then-deps** | RepoMaster | Walk structure first, then dependencies | None | Medium / Medium |
| **Embedding-based code search** | Moatless | FAISS vector store as callable tool | None | Medium / Medium |
| **Self-questioning challenge prompt** | Durin (V3 test) | "What haven't you checked? What assumptions?" — produces gap list | Tested, mixed results | Low / Conditional |

---

## Category 6: Memory & Learning Across Sessions

How agents retain and reuse knowledge.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **Closed learning loop (skill docs)** | Hermes | Solve → document skill → store → retrieve on similar task | None | High / Very High |
| **Persistent memory directory** | Hermes, Goose | `~/.hermes/` or similar, flat markdown files | Basic (session metadata) | Low / Medium |
| **Auto-generated skills from experience** | Hermes | Agent writes new skill docs based on what it learned | None | High / High |
| **Community-shared skills** | Hermes | `agentskills.io` standard for sharing | None | Medium / Medium |
| **Per-task code snippets** | Aider | Caches repo maps and tags between runs | Basic | Low / Medium |
| **Event-sourced state with replay** | OpenHands | Append-only immutable event log | None | High / Medium |
| **Episodic failure memory** | (Reflexion paper) | Remember what failed before, avoid repeats | None | Medium / High |

---

## Category 7: Behavioral Modulation

How agents adapt their behavior to context.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **Posture vector (dynamic weights)** | Durin (unique) | 5-axis vector modulates prompts, temperatures, deliberation; updated by events | ✅ Implemented but V6 = 0pp delta | — / Unproven |
| **RL-based offline self-improvement** | Hermes (Atropos) | RL loop optimizes agent behaviors against benchmarks; not real-time | None | Very High / High (but offline) |
| **Phase-aware temperature** | Durin (essentially unique) | Different temps for INVESTIGATE / PLAN / EXECUTE / VERIFY | ✅ Implemented | — / Unvalidated |
| **Per-mode temperature** | OpenCode (partial) | Different temps per agent mode | Closest production analog to Durin's phase temps | — |
| **Posture phrase injection** | Durin (unique) | Translates vector to short prompt phrase | ✅ Implemented | — / Unproven |

**Finding: this category is largely empty in production. No agent ships dynamic behavioral weights.** This is either a real opportunity or a dead end.

---

## Category 8: Deliberation / Planning

How agents reason before acting.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **ReAct loop (default)** | Most agents | Thought → Action → Observation cycle | ✅ Implemented (loop) | — |
| **DAG-based planning** | Devin | Plans as directed acyclic graph, not linear list | List | High / Medium |
| **Dynamic re-planning on failure** | Devin | Plan adjusts when tests/critic flags issues | ✅ Cycle restart with new investigation | — |
| **Single-call multi-perspective (role-play)** | Durin V3 | One LLM call with Critic/Explorer/Pragmatic sections | ✅ Implemented | — / V3 test inconclusive |
| **Multi-call multi-model deliberation** | Devin, Moatless | Different models per perspective | None | Very High / Medium |
| **Plan agent + Build agent split** | OpenCode | Dedicated planner before executor | Partial | Medium / Medium |
| **Reasoning tokens before code gen** | Bolt.new | Reasoning section in single-pass prompt | Partial (thinking blocks) | Low / Low |

---

## Category 9: Sandboxing / Safety

Execution isolation strategies.

| Strategy | Who does it | Description | Status in Durin | Cost/Value |
|---|---|---|---|---|
| **Bubblewrap (bwrap)** | Durin | Linux namespace-based sandbox | ✅ Implemented | — |
| **Docker sandbox** | OpenHands, Devin | Full container isolation | Partial (testbed Docker) | Medium / Medium |
| **Seatbelt (macOS) / Landlock (Linux)** | Codex CLI | Platform-native sandboxing | None | Medium / Medium |
| **Browser WebAssembly sandbox** | Bolt.new | StackBlitz WebContainers | None (not browser-based) | — |
| **Three autonomy modes** | Codex CLI | Suggest / auto-edit / full-auto, user-selectable | Permission system | Low / Medium |

---

## Cross-Cutting Patterns

### What every successful production agent has in common:
1. **Tools quality > loop quality** — SWE-agent's central insight
2. **Verification is non-negotiable** — every agent has some form
3. **Context engineering matters more than model choice** — Aider repo map, Cursor indexing
4. **MCP support** — increasingly the standard for extensibility (Goose, OpenCode, OpenHands, Codex)

### What no production agent does (open design space):
1. **Dynamic behavioral weights** — only Durin
2. **Phase-aware temperature** — only Durin (ThinkCoder is the academic precedent)
3. **Posture-modulated context projection** — Doc 03 design, not built anywhere
4. **Real-time multi-perspective deliberation in single model** — Durin V3 (Devin does it with separate models)

### What only research agents do (high-risk/high-reward):
1. **MCTS tree search** — Moatless 23% improvement on SWE-bench
2. **Multi-agent debate** — Moatless, academic papers
3. **RL-based self-improvement** — Hermes (offline)

---

## Prioritization Framework

For each potential improvement, evaluate:

1. **Production validation**: Has it been proven to work in shipped agents?
2. **Implementation cost**: Days, weeks, or months?
3. **Maps to Durin's gaps?**: Does it address a known weakness (memory, exploration, verification, etc.)?
4. **Compatible with Durin's design philosophy**: Aligns with Doc 01-04 architecture?

### Quick-win candidates (low cost, validated, fills gap)
- **Generate-test-repair loop** (Aider): automatic test execution after edits — fills the gap in current "agent must manually call exec"
- **Capped search results with summaries** (SWE-agent): improves search tool — low cost refactor
- **Critic with explicit criteria** (combo): tested as V4 in `06_log_experiments.md` — empirically refuted (-1.16pts vs baseline), kept here as documented dead-end

### Medium-term investigations
- **Linter gate (hard reject syntactically invalid edits)** (SWE-agent): integrate with existing edit tools
- **Edit by line range with validation** (SWE-agent): refactor edit tools
- **Skill documents (Hermes pattern)**: small step toward Doc 03 memory system
- **Episodic failure memory** (Reflexion): track failure patterns across sessions

### Large investments (multi-week)
- **Memory graph (Doc 03)**: the biggest known opportunity
- **PageRank repo map** (Aider): context engineering upgrade
- **Shadow workspace** (Cursor): speculative verification

### Probationary (no production validation, unproven in our tests)
- **Posture vector**: V6 = 0pp; needs new evidence or removal
- **Single-call multi-perspective deliberation**: V3 test inconclusive; needs better test or simplification
- **Phase-aware temperature**: never validated; needs dedicated experiment

---

## Sources

Original research synthesized from:
- Aider docs and source code
- SWE-agent (Princeton) NeurIPS 2024 paper and config files
- Cursor blog (shadow workspace, speculative edits)
- Devin (Cognition) public blog posts
- Goose docs (Block/AAIF)
- OpenHands (formerly OpenDevin) site and SDK paper
- OpenCode source and docs
- Codex CLI documentation
- Hermes Agent site and self-evolution repo (NousResearch)
- Moatless Tools (SWE-Search paper)
- Continue.dev docs
- Cline / Roo Code docs and issues
- arxiv 2604.03515 (agent loop taxonomy)
- arxiv 2410.20285 (Moatless SWE-Search)
- arxiv 2506.07295 (Hot or Cold? coding temperature)
- ACL 2025 (ThinkCoder)
- Anthropic and OpenAI provider documentation
- Evidently AI blog on agent benchmarks

---

## Last updated: 2026-05-19

---

## May 2026 addendum — observations from the windowed/capped review

Re-reading this catalog with a filter for "language-agnostic, production-validated, small scope" surfaced that the two SWE-agent quick-wins (windowed file viewer, capped search) **are already implemented in Durin's `read_file` and `grep` tools**, just at more permissive defaults than SWE-agent's originals (2000 vs 100 lines; 250 vs 50 results). What we don't have is **telemetry to know if those defaults are correctly sized for 1M-context frontier models** — see `bitacora.md` for full rationale.

Three candidates per-language that we explicitly **rejected from the immediate plan** (May 2026):
- Generate-test-repair loop (Aider) — requires per-language test runner integration
- Linter gate hard-reject (SWE-agent) — requires per-language AST/linter
- Edit-by-line-range with validation — validation step is per-language

These remain valid quick-wins but turn into multi-language maintenance burden as Durin's tooling broadens.
