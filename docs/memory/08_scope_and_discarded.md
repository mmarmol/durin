---
title: Scope, non-goals, and discarded approaches
version: 0.1-draft
status: under construction
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md
related: all other 0X docs
---

# Scope, non-goals, and discarded approaches

This document records what the memory system **does not** do (and why), what we **tried and abandoned**, and what we explicitly **deferred to backlog**. It exists so future maintainers (humans or LLMs) don't re-attempt patterns we've already evaluated and rejected, and so we have a written rationale when someone asks "why isn't durin doing X?".

**Principle:** every "no" is a decision. Decisions deserve rationale. Without this doc, the corpus reads like a wish-list of what we built; with it, the boundary between MVP and not-MVP is clear.

---

## 1. Non-goals (recap from overview)

Reproduced from `00_overview.md` §2 with one-line rationale per item:

| # | Non-goal | Rationale |
|---|---|---|
| 1 | Not a classical knowledge graph (RDF/SPARQL) | Academic, rigid, hostile to LLMs. We use markdown + indices instead. |
| 2 | Not a reasoning system | Retrieval and structure are our scope; reasoning is the final LLM's job. |
| 3 | Not multi-tenant | Single-workspace per installation. Multiple users interact but memory is shared. |
| 4 | No LLM in hot path | Hot path is deterministic. LLMs only on cold path (Dream, ingestion). |
| 5 | Not a replacement for the context window | We provide material; LLM synthesizes. |
| 6 | No history rewriting | Sessions are immutable; synthesis goes on top. |

---

## 2. Discarded experiments

These were tried (in code or as proposals), evaluated against reality, and removed or rejected.

### 2.1 G3.b — LLM query rewriter on hot path

**What we tried:** an LLM call before every `memory_search` that generated 5 paraphrases, extracted entities and predicates, and merged results via RRF (`durin/memory/query_rewriter.py`, preserved as library, not active).

**Why it failed:**
- Saturated rate limits. Each agent turn → N searches → N LLM calls just to rewrite.
- The hot path is the wrong place for an LLM operation. It's like "writing to the DB on every SELECT" (user's articulation 2026-05-26).
- Smoke test showed 8/10 initial but bench-scale runs broke z.ai with empty responses + timeouts.

**Lesson:** when considering adding LLM in any frequent path, ask first what upstream weakness the LLM is compensating for. The rewriter compensated for: (a) frontmatter not entering the embedding, (b) 1500-char body truncation, (c) MiniLM-L12 being a small model, (d) no aliases in entries, (e) cross-lingual limits of the embedding model. The right fixes are all upstream (entered MVP via §6.6 doc 02 + alias expansion); the rewriter was a downstream patch.

**Status:** discarded 2026-05-26. Module preserved as library for opt-in cold-path use (e.g., write-time extraction, async curation) if those use cases arise.

**Maintenance plan (decided 2026-05-27):** the module as a whole has no active caller and no concrete future plan. Two small pieces inside it ARE reusable for the upcoming Dream JSON Patch apply (H1 / `05_dream_cold_path.md` §6):

- `_lenient_json_loads` — a `json_repair`-tolerant JSON parser (~30 LOC) for LLM outputs with trailing commas / unquoted keys / comments.
- Code fence stripping regex — handles ``` / ```json / ~~~ wrappers (~10 LOC).

When the Dream apply v2 commit is implemented, these two pieces should be **extracted into a shared utility module** (suggested: `durin/memory/_llm_parsing.py`) and the rest of `query_rewriter.py` deleted in the same commit. Total work ~10 minutes, embedded in the apply implementation task — not a standalone refactor. Other pieces deleted with the module: CJK normalization helpers, `build_memory_llm_invoke`, the `QueryRewrite` dataclass, and the **intent classification** field (`factual_lookup | list | temporal | comparison | open_ended`) — none of these have a planned caller. The intent classification specifically: the intent router in `03_search_pipeline.md` §3 routes by lexical patterns (regex, CJK detection), not LLM classification; if LLM-based intent classification surfaces as a future need, it is ~30 LOC to reimplement freshly rather than carry the dead one forward.

Until that cleanup happens, the module sits unused. Importing from it is discouraged — anyone needing those utilities should trigger the cleanup commit rather than perpetuating the dead module as a dependency.

### 2.2 Closed predicate catalog for attributes

**What we tried (proposal stage):** define a fixed catalog of attribute keys (email, phone, lives_in, spouse, works_at, etc.) and force Dream to extract only into this catalog.

**Why it failed:**
- CRM-only worldview. Useless for coders ("file:lives_in" is silly), salespeople ("deal:stage"), support ("ticket:resolution"), students ("class:notes"), makers ("project:materials").
- User feedback 2026-05-26: "necesito pensar esto como una solucion generalista, para estudiantes, coders, makererts, vendedores, soporte, asistente personal, GENERAL."

**Decision:** open attributes + relations (`01_data_and_entities.md` §4). Drift control via existing_schema in Dream prompt (per-entity, not workspace-wide catalog).

### 2.3 Body in LanceDB rows

**What we considered:** storing the full markdown body inside each LanceDB row to avoid disk reads in cold-tier retrieval.

**Why rejected:**
- ~2x storage cost (10MB → 30MB typical for medium workspace).
- Disk reads for the body are <5ms; not the bottleneck.
- User feedback 2026-05-27: confirmed disk reads are acceptable for an assistant.

**Decision:** body NOT in LanceDB. Cold tier reads `.md` on demand (`02_indexing.md` §3.1, §10 #3).

### 2.4 MMR (Maximal Marginal Relevance) for top-K diversity

**What we proposed:** a step in the search pipeline that re-selects top-K balancing relevance and diversity, to prevent top-10 having 5 hits that say the same thing.

**Why deferred:**
- Archive of consolidated episodic (§3.6 doc 01) eliminates the primary source of duplication. Post-archive, the typical pattern is `entity (canonical) + 0-3 fragments + 1 session` — that's triangulation, not redundancy.
- Mainstream systems (mem0, graphiti, hermes, letta, cognee) don't implement MMR.
- Costs: ~50 LOC + tuning λ + test surface.
- Corpus chunks from the same source are addressed differently via per-source cap (§12.4 doc 03).

**Status:** not in MVP. Standalone algorithm, can be added later if bench shows residual duplication.

### 2.5 SQLite structural / analytical index

**What we proposed:** a SQLite table with parsed `attributes` and `relations` columns for analytical queries (COUNT, JOIN, GROUP BY).

**Why deferred:**
- For N entities < 500 (MVP scale), grep + parse on-the-fly is fast enough.
- FTS5 over rendered frontmatter (from doc 02) covers "find entities with attribute X" queries.
- Adds: another derived index to maintain, schema migration, sync coordination.
- Revisit when: N entities grows beyond ~500 OR agent issues frequent counting / grouping queries.

**Status:** not in MVP. Cross-corpus decision #1.

### 2.6 Pin-by-modality (exact-match hits guaranteed visibility)

**What we proposed:** a mechanism where exact-match hits from grep are pinned to top of their section regardless of RRF score.

**Why rejected:**
- Requires measuring keyword specificity to avoid pinning common-word matches.
- The `keywords` + dynamic RRF boost mechanism (§7.2 doc 03) covers the same case more elegantly: when the LLM signals "I care about this literal", `w_lexical` boost elevates the match naturally.
- No mainstream system implements pin-by-modality.

**Status:** not in MVP. Reconsider if bench shows that `keywords` is under-used and exact matches still get lost.

### 2.7 Cross-encoder ON by default

**What we initially proposed:** cross-encoder reranker enabled by default in MVP.

**Why changed to opt-in:**
- All multilingual cross-encoder models add 300-1500ms latency on CPU. Default-on breaks the search budget.
- Comparable systems (mem0 opt-in, graphiti opt-in) ship the same way.
- The default RRF + entity-aware rerank already produces useful retrieval.

**Status:** in MVP as opt-in, OFF by default. Default model when enabled: `jinaai/jina-reranker-v2-base-multilingual`.

---

## 3. Operational risks (from doc 18 §10)

The entity-centric memory design carries known operational risks. They were enumerated in `docs/18_entity_centric_plan.md` §10 before the corpus was written. This section maps each risk to its status in v2 and identifies what (if anything) the corpus does to mitigate it.

| # | Risk | Status in v2 | Mitigation reference |
|---|---|---|---|
| **R1** | HyperMem (SOTA LoCoMo) achieves 92.73% without entity nodes; entity-centric design may not pay off on bench accuracy alone | **Accepted.** The promise of durin's memory is **operational coherence across sessions** + human-editable corpus + cross-system identity persistence — axes LoCoMo doesn't test. Bench is secondary; we use it for retrieval-quality regression detection, not as the primary success metric. | `09_implementation_roadmap.md` §11 (validation); doc 18 §11 outcomes |
| **R2** | Mega-hub: `person:user` and `project:durin` will accumulate hundreds-to-thousands of claims over months | **Partially mitigated.** Per-entity relation cap (soft 50 / hard 200 reject — doc 01 §4.4); archive of consolidated episodic (those entries don't count toward claims since they're moved out). **Sub-paging by scope is NOT implemented** — deferred to backlog (§5). Trigger: telemetry sees any entity with claim count > N. | doc 01 §4.4, doc 01 §3.6 archive, §5 backlog (sub-paging) |
| **R3** | Dream cost is unmeasured at scale | **Mitigated.** Doc 05 §13 provides an estimated range ($0.25-$1/day at typical pass rates with glm-5.1). Doc 07 telemetry captures `llm_input_tokens_total` and `llm_output_tokens_total` per pass. Operator alarm at `dream_llm_cost_per_day_usd > $5/day`. | doc 05 §13, doc 07 §6.2, §11 |
| **R4** | Cross-system identity (email vs git author vs conversational nickname) has no universal solution | **Accepted.** Aliases are declared manually (via Dream extraction from observations, or via human edit of the entity page). The system does not auto-resolve identity across external systems. | doc 01 §4.5 (slug normalization), doc 05 §5 (existing schema includes existing aliases) |
| **R5** | LLM-driven entity resolution can mis-merge | **Mitigated.** Absorb-judge defaults: OFF (master switch), 95/100 confidence threshold, 24h quarantine, recovery via `git revert`. The cascade is: deterministic match first (slug + aliases exact), LLM only in the gray zone — and only when operator opts in. | doc 05 §8 (all subsections) |
| **R6** | Alias collision: common names (`marcelo`, `María`, `juan`) become ambiguous as the workspace grows; alias_index assumes one-to-one | **Partially mitigated.** Numeric suffix in slugs prevents file-system collision (doc 01 §4.5). Absorb-judge handles cases where two pages should actually be merged. **BUT one-to-many alias resolution (`alias → N candidate entities` with disambiguation at read-time) is NOT implemented** — deferred to backlog (§5). Trigger: telemetry detects ≥2 entities sharing an alias in their `aliases:` field. | doc 01 §4.5, doc 05 §8, §5 backlog (alias one-to-many) |

**Reading guidance:** "accepted" means we know the limit and the system documents it; "mitigated" means active code reduces the risk; "partially mitigated" means there is a structural gap left for backlog.

---

## 4. Mechanisms in other systems NOT adopted

We surveyed mem0, Letta/MemGPT, Zep, Graphiti, Cognee, Hermes-Agent, OpenClaude, OpenClaw, OpenHands, GAAMA. These mechanisms exist in those systems but we explicitly did NOT adopt them.

### 3.1 HyDE (Hypothetical Document Embeddings)

**What it does:** LLM imagines a hypothetical document that would answer the query, embeds THAT, searches with the imagined-document embedding (often closer semantically to the actual answer than the literal query).

**Why not adopted:** still LLM-in-hot-path — same problem as G3.b. We're avoiding LLM in retrieval. Cold-path query enrichment (Dream-side) is a possible future direction but not MVP.

### 3.2 Reflection / pattern detection (GAAMA, Zep)

**What it does:** beyond fact consolidation, a periodic process detects recurring patterns ("X tends to postpone PRs in long sprints") and emits "reflection" nodes.

**Why not adopted in MVP:** valuable for generalist agents but adds significant Dream complexity. Cost-benefit not yet justified. Backlog.

### 3.3 Concepts as first-class entities (GAAMA hypergraph)

**What it does:** abstract concepts (e.g., `durin`, `rlhf`, `agile`) are first-class graph nodes that mediate retrieval via Personalized PageRank.

**Why not adopted:** durin has a `topic` entity type but doesn't propagate retrieval through it. Adopting hypergraph mediation requires changing the retrieval algorithm to graph traversal. Different paradigm. Backlog.

### 3.4 Multiple memory tools per modality (e.g., semantic_search vs keyword_search)

**What others do:** some designs separate tools by retrieval modality. We considered this in `03_search_pipeline.md` §1.

**Why not adopted:** mainstream pattern is single tool with internal routing (mem0, hermes, openclaw, cognee — 4 of 5 systems). LLMs don't reliably choose between specialized search tools. We use one `memory_search` tool with optional `keywords` for explicit literal signaling.

### 3.5 LLM exposed weights / fusion params

**What others do:** none we surveyed expose RRF/BM25 weights to the LLM (verified in repos).

**Why not adopted:** LLMs don't have intuition for numeric weights. Pass burden to operator config + onboarding wizard / dashboard.

### 3.6 Mode/type enum in search tool (cognee `search_type`)

**What it does:** cognee exposes `search_type: GRAPH_COMPLETION | RAG_COMPLETION | CODE | CHUNKS | FEELING_LUCKY`, agent picks the retrieval mode.

**Why not adopted:** adds complexity to tool description. Test results suggest LLMs pick the wrong mode often. Auto-routing by query pattern (intent_router) achieves similar effect without burdening the LLM.

### 3.7 Embedding hybrid (SPLADE / ColBERT)

**What it does:** sparse + dense hybrid embeddings (SPLADE) or multi-vector with late interaction (ColBERT). Outperforms bi-encoder + BM25 in IR benchmarks.

**Why not adopted:** requires new models, breaks current LanceDB + FTS5 setup. Migration cost high. Backlog.

### 3.8 Versioning as a separate tool

**What others might do:** dedicated `memory_history` MCP tool for git log queries.

**Why not adopted:** git history is exposed to Dream internally (its prompt includes `recent_history`) and to the human via any git CLI. No dedicated agent-facing tool. Cross-corpus decision #4.

### 3.9 Active forgetting policies (delete or compress old entries)

**What others do:** mem0 has lifecycle policies (delete after N days for low-importance memories). Letta has explicit memory management tools.

**Why not adopted:** archive of consolidated episodic (§3.6 doc 01) handles the primary case. Deeper forgetting (compress 100 old episodic into 1 summary) is destructive; requires explicit policies for safety. Backlog.

### 3.10 Trust scoring per source

**What others might do:** user-provided memories rank above LLM-inferred memories.

**Why not adopted:** durin's classes (stable vs episodic) already encode this implicitly — stable means "user/agent explicitly marked durable". Not enough distinct trust tiers to justify a separate scoring system in MVP.

### 3.11 Tool call history as structured memory

**What others might do:** structure agent's own tool-call history as a queryable layer.

**Why not adopted:** sessions already contain tool calls. Grep over `sessions/<id>.jsonl` covers ad-hoc retrieval. No dedicated structured layer in MVP.

---

## 5. Features explicitly deferred to backlog

These are NOT discarded; they're queued for post-MVP. When and how they enter depends on observed need.

| Feature | Trigger to revisit | Likely doc to update |
|---|---|---|
| **§2.F eager pre-fetch** (query-specific memory injection into user message before LLM call, hermes/openclaw pattern) | Telemetry shows `memory.silent_retrieval_miss > 5%` of turns over 1 week, OR users report frequent "the agent didn't remember X" complaints | `06_prompts_and_instructions.md` new §9, `04_agent_tools.md` §6 |
| Cross-encoder reranker default ON | Bench shows opt-in OFF is significantly worse | `03_search_pipeline.md` §9, `06_prompts_and_instructions.md` §6.2 |
| MMR | Bench / user reports show duplication in top-K post-archive | `03_search_pipeline.md` §11 |
| Temporal decay enabled for more classes | Workspace > 1 year old shows obsolete-info regressions | `03_search_pipeline.md` §10 |
| SQLite structural index | N entities > 500 OR analytical queries become frequent | `02_indexing.md` new section |
| Active forgetting (compression, deletion) | Workspace > 2 years; storage / index size becomes burden | `05_dream_cold_path.md` new section |
| Reflection / pattern detection (Dream tier 2) | Generalist use cases show pattern queries fail | `05_dream_cold_path.md` new section |
| Cross-entity consistency checks | Drift between entities observed | `05_dream_cold_path.md` new section |
| Concepts as mediators (GAAMA-style) | Concept-level queries fail consistently | architectural rework |
| Embedding hybrid (SPLADE/ColBERT) | Recall@10 plateau with current models | `02_indexing.md` model selection |
| Dedicated archive index | Frequent archive queries from operators | `01_data_and_entities.md` §10 #4 |
| Pin-by-modality | Exact-match queries fail despite `keywords` mechanism | `03_search_pipeline.md` §7.3 |
| HyDE on cold path | Need for cold-path query enrichment surfaces | `03_search_pipeline.md` new section |
| **Sub-paging by scope (R2 mitigation)** | Any single entity exceeds N=200 claims (verified via telemetry counting attribute keys + relation count + episodic provenance entries) | `01_data_and_entities.md` §3.5 schema extension; `05_dream_cold_path.md` new section on partition triggers |
| **Alias one-to-many resolution (R6 mitigation)** | Telemetry detects ≥2 entities sharing an alias in their `aliases:` field, OR Dream encounters write-time collision on alias intended for a single entity | `01_data_and_entities.md` §4.5 (alias index becomes one-to-many); `03_search_pipeline.md` §3.2 (entity extraction returns candidate list); `05_dream_cold_path.md` §8 (write-time tagger) |
| **Memory export / import (formal)** — structured dump filterable by entity/scope/date, cross-system migration from competing systems (mem0, letta), encrypted format option | Pre-condition for sharing durin with real external users (any first beta or limited-release). Specific triggers: (1) first user request for export OR (2) immediately before any breaking schema change (operators need to export before migrating). Until then, `cp -r ~/.durin/workspace/` is the informal portability mechanism between same-version installations. | New design doc when triggered |
| **Data deletion (GDPR-like cascading delete)** — "forget everything about `person:X`" with cascading delete across entity page + archive + episodic mentions + provenance refs in other entities + LanceDB rows + FTS5 entries; honest handling of git history | Pre-condition for exposing durin to external users via channels (Telegram/Slack/etc. bots). Specific triggers: (1) first external user interacts with durin via any channel, (2) operator opens durin to users in a jurisdiction with explicit right-to-be-forgotten laws (EU GDPR, California CCPA, etc.), or (3) any public/beta release announcement. Until then, the operator removes data manually (delete files + git commits) with no formal flow. | New design doc when triggered |
| **Auto-backup of memory workspace** — push `memory/.git/` to a remote, OR encrypted snapshot to cloud, OR scheduled local backup directory | Trigger: operator enables `memory.backup.enabled = true` in workspace config (currently not implemented). Until then, the operator can manually `git remote add` and `git push` `memory/.git/` to any git remote (the workspace folder is a normal git repo). For non-git backup, `rsync` or `tar` of `~/.durin/workspace/` works. | `02_indexing.md` and/or a dedicated `backup.md` doc when triggered |

### 4.1 §2.F eager pre-fetch — detailed rationale

This is one of the deferred items above. Worth detailing because the deferral is data-driven, not arbitrary.

**Mechanism (Hermes + OpenClaw pattern, verified in `hermes-agent/agent/memory_manager.py:227` and `conversation_loop.py:754`):**

```
Before each agent turn:
  1. raw_context = memory_search(query=user_message)
  2. block = "<memory-context>[system note...]\n{raw_context}\n</memory-context>"
  3. user_message_for_api = original_user_message + "\n\n" + block
  4. LLM sees the message with memory already injected; may respond without
     invoking memory_search tool at all
```

**Why deferred (not rejected):**

- **HotLayer already covers 70%.** The always-on canonical+fragment injection (doc 06 §8) handles the most-frequent queries without a per-turn extra search.
- **Multi-query identity.md pattern shipped +3.9pp** in LoCoMo v2 by teaching the agent to invoke memory_search itself when needed. Adding eager pre-fetch on top is uncertain incremental value.
- **Cost per turn**: +50-130ms latency + cache miss in upstream prompt cache (variable payload in user message). Real cost; only worth paying if the value is observable.

**Trigger to revisit (telemetry-driven):**

1. Doc 07 §6.4 adds a new event `memory.silent_retrieval_miss` emitted when:
   - The agent answered without invoking `memory_search` in this turn, AND
   - The next user turn looks like a re-ask or correction (heuristic: `is_re_question(prev, curr) || contains_negation_correction(curr)`).

2. Operator monitors weekly rate.

3. If `silent_retrieval_miss_rate > 5%` consistently over a week, OR users report "the agent didn't remember X" >3 times in a week, the deferred §2.F becomes active.

**When activated, the spec to write:** doc 06 new §9 "Eager pre-fetch (§2.F)" with: trigger condition (every turn), search builder (uses user_message as query, scope=all, level=warm), wrapper format (`<memory-context>` block with system note), insertion point (append to user message ephemerally — do NOT persist to session), failure behavior (omit silently on memory_search error), telemetry (`memory.eager_prefetch_invoked` + duration).

---

## 6. Decisions where we explicitly chose against the mainstream

Cases where mem0/graphiti/etc. do X, but we chose NOT X for explicit reasons. Useful when someone says "why doesn't durin do X like everyone else?"

| Topic | Mainstream | Durin choice | Rationale |
|---|---|---|---|
| Default ON for cross-encoder | mostly opt-in OFF (same) | Opt-in OFF | We agree with mainstream here. |
| MMR | rarely implemented | Not in MVP | Same as mainstream. |
| Versioning as a tool | not standard | git history exposed via Dream prompt + CLI | Reuse what exists. |
| LLM in hot path | most avoid; cognee uses LLM in classifier | We strictly avoid | Cost + latency. |
| Multi-vector per facet | rare (some research) | Single vector per doc | Simplicity. |
| Closed catalog | mem0 has implicit catalog via LLM tendencies | Open, with drift control via existing_schema | Generalist use cases. |
| Tool sectioning markers | rare (hermes uses `<memory-context>`) | We use them (CANONICAL/FRAGMENT/SESSION/INGESTED) | Validates +3.9pp in our v2 prompts. |
| Cold-path consolidation | mem0 sync at write; we batch | Async batched Dream | User experience (no write latency) + cost. |

---

## 7. Lessons learned (general)

Distilled from the design process documented in `docs/29_exploracion_datos_y_relaciones.md` and prior iterations:

### Lesson 1 — Tool description is a weak signal

Imperative instructions in tool descriptions ("USE BEFORE answering", "trust this") tend NOT to change LLM behavior reliably. Structural patterns (markers in results, distinct tool names with specific purpose) work better.

**Evidence:** D1 / D3 prompts tested 2026-05 lost 20pp. The v2 prompts (declarative + specific) gained 3.9pp.

**Implication:** prefer structural communication. When you must use text, make it declarative ("issue 2-3 searches for compound questions") not imperative ("ALWAYS use multi-query").

### Lesson 2 — Fix causes, not symptoms

When retrieval fails, the temptation is to add a downstream patch (rewriter, pin, special mode). The right approach is to ask: what UPSTREAM weakness is causing the failure?

**Evidence:** G3.b query rewriter compensated for 5 upstream issues. Fixing those (frontmatter rendering, summary for entity pages, aliases in entries, FTS5 for cross-lingual lexical, BM25 over rendered frontmatter) makes the rewriter unnecessary.

**Implication:** before adding a new component, list the upstream causes the new component would compensate for. Fix one of those instead.

### Lesson 3 — Archive over delete

Recoverability is cheap when designed in; expensive when bolted on.

**Evidence:** archive of consolidated episodic preserves provenance and enables recovery if Dream consolidates wrong. Bench shows this also eliminates the main duplication problem in retrieval.

**Implication:** when removing data from active state, move it (archive) rather than delete. Disk is cheap; bad consolidations are expensive.

### Lesson 4 — Markdown as source of truth

When index and SoT diverge, SoT must win. This requires the index to be a derivative reconstructible from SoT.

**Evidence:** every index in this corpus (LanceDB, FTS5, eventual structural SQLite) is reconstructible from `.md` files. `durin reindex` is always available.

**Implication:** never store data in an index that doesn't also exist in markdown. The index is acceleration; markdown is truth.

### Lesson 5 — Single tool with internal routing > multiple specialized tools

Multi-tool agents struggle to choose between similar-purpose tools (`feedback_tool_description_weak_signal.md`).

**Evidence:** mem0, hermes, openclaw, cognee all use single search tool. Cognee tried mode enum and added `FEELING_LUCKY` because the agent picked wrong modes.

**Implication:** if you can route by query pattern (CJK, keyword shape, etc.) internally, do that. Don't make the LLM pick.

### Lesson 6 — Cold-path investment pays compound returns

Building Dream right (consolidation, archive, dedup, drift control) eliminates many downstream problems (duplication, drift, retrieval noise).

**Evidence:** archive + consolidation makes MMR unnecessary, makes pin-by-modality unnecessary, makes drift control structural rather than per-query.

**Implication:** invest in cold path early. Hot-path patches stack up as tech debt.

---

## 8. Cross-references

- Architectural decisions per module: each module's §10/§14/§16 (decisions tables).
- Cross-corpus decisions: `00_overview.md` §10.
- Prior exploration (Spanish, longer-form): `docs/29_exploracion_datos_y_relaciones.md`.
- Mem files documenting past failures: `~/.claude/projects/.../memory/feedback_*.md`, `project_g3b_query_rewriting_plan.md`.
