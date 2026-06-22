# Memory: design rationale (non-goals, discarded approaches, lessons)

This document records what the memory system **does not** do (and why), what was **tried and abandoned**, and decisions where durin explicitly chose against the mainstream. It exists so future maintainers do not re-attempt patterns that have already been evaluated and rejected, and so there is a written rationale when someone asks "why isn't durin doing X?".

**Principle:** every "no" is a decision. Decisions deserve rationale.

---

## 1. Non-goals

| # | Non-goal | Rationale |
|---|---|---|
| 1 | Not a classical knowledge graph (RDF/SPARQL) | Academic, rigid, hostile to LLMs. We use markdown + indices instead. |
| 2 | Not a reasoning system | Retrieval and structure are our scope; reasoning is the final LLM's job. |
| 3 | Not multi-tenant | Single-workspace per installation. Multiple users interact but memory is shared. |
| 4 | No LLM in hot path | Hot path is deterministic. LLMs only on cold path (Dream, ingestion). |
| 5 | Not a replacement for the context window | We provide material; LLM synthesizes. |
| 6 | No history rewriting | Sessions are immutable; synthesis goes on top. |

---

## 2. Discarded approaches

These were tried (in code or as proposals), evaluated against reality, and removed or rejected.

### LLM query rewriter on the hot path

durin does not run an LLM before each `memory_search` call to generate paraphrases or extract entities. This was built and removed.

The rewriter saturated rate limits: each agent turn produced multiple searches, each requiring an LLM call just to rewrite the query. More fundamentally, placing an LLM on the hot path is the wrong layer. The rewriter was compensating for upstream weaknesses: frontmatter not entering the embedding, body truncation, the small embedding model, missing aliases, and cross-lingual limits. The right fixes are all upstream — and those were made. The rewriter was a downstream patch that solved the wrong problem at the wrong level.

**Lesson:** when considering an LLM in any frequent path, first list what upstream weakness it is compensating for. Fix one of those instead.

### Closed predicate catalog for attributes

A fixed catalog of attribute keys (email, phone, lives\_in, works\_at, etc.) that Dream would extract into was considered and rejected. The catalog reflects a CRM worldview useless for coders, makers, students, or support workflows. durin's use cases are generalist.

**Decision:** open attributes and relations, with drift control via the existing schema shown in Dream's prompt per entity.

### Body stored inside LanceDB rows

Storing the full markdown body in each LanceDB row (in addition to disk) was considered and rejected. The cost is roughly twice the index size with no meaningful latency benefit: disk reads for body content are a few milliseconds, and the next LLM call downstream takes seconds. More importantly, storing the body in LanceDB makes the cache authoritative alongside the markdown file — violating the principle that markdown is the single source of truth and LanceDB is a disposable derived index.

**Lesson:** an optimisation that violates an architectural principle must be justified by measurement, not intuition. The fix for a slow consumer is local to that consumer, not a schema change.

### Maximal Marginal Relevance (MMR) for result diversity

A step re-selecting top-K results to balance relevance and diversity was proposed. It was deferred pending evidence that duplication is a real problem in practice.

The archive of consolidated episodic eliminates the primary source of duplication: post-archive, the typical result set is one canonical entity page plus a small number of fragments — triangulation, not redundancy. The per-source cap in the search pipeline handles the secondary case. MMR remains a standalone algorithm that can be added later if bench shows residual duplication, but without evidence of the problem it adds complexity and regresses exact-match queries.

### SQLite structural / analytical index

A SQLite table with parsed `attributes` and `relations` columns for analytical queries (COUNT, JOIN, GROUP BY) was proposed and decided against.

For the workspace scales durin targets, grep and on-the-fly parsing are fast enough. FTS5 over rendered frontmatter covers attribute lookups. A structural index adds another derived table to maintain, schema migrations, sync coordination, and drift risk — for a role the LLM agent already fills: when analytical queries are needed, the agent writes the grep or parse it needs from the markdown files. Building a structural index replaces ad-hoc LLM-driven traversal with a separate query language the agent has to learn and the operator has to maintain. Mainstream LLM-in-the-loop systems ship without a structural layer.

### Pin-by-modality (pinning exact-match hits to top)

A mechanism to guarantee exact-match hits always appear at the top of results was proposed and rejected.

The auto-keyword detection path already handles the primary case: the search pipeline detects email addresses, URLs, UUIDs, and file paths in the query and boosts the lexical weight accordingly. The `keywords` parameter gives the agent explicit control for identifiers auto-detection cannot recognize. A pin mechanism would either bypass that explicit knob or duplicate it, and it would regress queries where the semantic match is actually better than the exact-match hit. Weight tuning of the existing boost knob is the correct response if exact-match misses occur.

### Cross-encoder reranker default ON

The cross-encoder reranker ships opt-in, OFF by default. Making it default ON was considered and rejected.

Multilingual cross-encoder models add 300–1500 ms latency on CPU and additional resident memory. Default ON would break the search latency budget for all installations without giving the operator a choice. The operator who values quality over latency enables it via workspace config, the onboarding wizard, or the web dashboard. Mainstream comparable systems ship reranking opt-in as well. The decision is structural — operator choice — not empirical.

### `memory_ingest` URL fetch and inline content branches

`memory_ingest` accepts only local file paths. URL and inline content variants were proposed and removed.

URL fetch would duplicate `web_fetch`, which already handles URL-to-markdown extraction with Jina Reader, a readability fallback, SSRF protection, content-type sniffing, and timeout handling. Reimplementing those policies inside `memory_ingest` would either duplicate the code and drift, or call `web_fetch` internally — at which point the parameter just hides a two-step workflow behind a flag. Inline content is `memory_store(class_name="corpus")`: when the agent already has text in context, persisting it goes through `memory_store` directly. The only `memory_ingest`-exclusive capability is preserving the original artifact on disk by content hash and chunking it — both meaningful only for local files.

The composition rule:

| Workflow | Tools |
|---|---|
| Local file on disk | `memory_ingest(path=...)` |
| Article found on the web | `web_fetch(url=...)` → `memory_store(content=markdown, class_name="corpus")` |
| Text already in context | `memory_store(content=..., class_name="corpus")` |

**Lesson:** when spec parameters are "synced" from a doc to code, verify the schema actually implements the promised parameters. String comparison tests pass even when the behavior they describe is absent.

### `memory_store` parameter surface — `valid_from` and `pending` class

`valid_from` is not exposed as a tool parameter. The tool's class enum is `stable | episodic | corpus` — not the full internal set.

`valid_from` defaults to today for the overwhelming majority of agent-in-conversation stores. The 1% back-dating case (seeding historical data) calls the pure `store_memory` function directly. Adding a `valid_from` parameter would expose a knob the LLM defaults to incorrectly most of the time.

`pending` is excluded because entries written there are invisible to every retrieval path (the indexer and file watcher skip `memory/pending/**`). Letting the LLM write to `pending` via the tool would be silent data loss.

**Lessons:** enum values that look available but are excluded from the retrieval pipeline are traps. Tool parameter names and persisted field names differ by plane — document both explicitly. Default behavior often beats new tool parameters; before exposing a knob, ask who actually needs it.

### `memory.silent_retrieval_miss` heuristic detection

A telemetry event emitted when the agent answered without invoking `memory_search` and the user's next turn looked like a re-ask was proposed and discarded.

Substring overlap does not require language parsing but has a high false-positive rate on legitimate refinement turns. Negation and correction heuristics are inherently English-shaped — they do not generalize to CJK or Spanish without per-language detector code. The only path to language-agnostic detection is an LLM classifier, but running one per user turn to compute a telemetry metric breaks the cheap-structured-event contract the rest of the telemetry system maintains.

**Lesson:** heuristic detectors with language-specific token lists are a red flag for any subsystem serving multilingual workloads. Ship the LLM judge or skip the feature — do not ship an English detector and expect it to generalize.

### `durin archive show / list` CLI commands

Dedicated CLI commands to inspect archived content were proposed and decided against.

Three existing surfaces already cover archive recovery: `memory_search(scope='archive')` for agent-visible semantic recovery, `durin memory expand <entity>` for per-entity rendering including archived predecessors, and `cat memory/archive/<class>/<id>.md` plus standard `find` for direct shell access. A dedicated command would duplicate these without a unique use case.

**Lesson:** "deferred until concrete trigger" without a written failure mode is functionally the same as discarded — it leaves a phantom item that returns each audit pass. When existing surfaces cover the use case, classify as discarded, not deferred.

### `existing_uris_cap` Dream-prompt config knob

Lifting the hard-coded cap on how many recent entity URIs appear in Dream's consolidator prompt into operator config was proposed and decided against.

Duplicate entity creation is invisible to operators: there is no telemetry measuring "duplicate avoided thanks to existing\_uris signal", so operators cannot detect "cap too low" empirically. The 100-most-recent URIs are a strong signal where duplicates actually occur — around recently-active entities. Old entities are not the typical source of duplicates. Two caps in series (producer and renderer) would require coordinated config threading for a knob no telemetry would tell the operator to turn. An operator who genuinely needs to tune the constant can change one line of code.

### `summary` slot in entity-page embedding text

Inserting a Dream-generated summary between frontmatter and body in the entity-page embedding text was proposed and decided against.

The data model does not support the slot: `EntityPage` has no `summary` field and Dream never produces one for entity pages. Only the vector path is bounded by the body truncation limit — FTS5 indexes the full composed text without truncation, and the grep fallback reads from disk. A query whose match is deep in a long body is found by the lexical and grep paths even if the vector centroid lacks those tokens. The agent that receives a canonical hit with a truncated snippet can drill to the full body in one tool call.

### Full unification of `hot_layer` and `sectioned_output` renderers

Collapsing the two renderers that produce structured memory blocks into a single module was proposed and decided against.

The two renderers are intentionally separate: `hot_layer._render_canonical_block` produces eager pre-injection with a structured `Attributes: k1 is v1` format and tight body cap; `sectioned_output._render_block` produces search-result rendering with summary-or-body preference. Forcing either into the other's shape regresses one use case. What was shared — the marker convention (`=== CANONICAL: <ref> ===`) — was extracted to a single module (`durin.memory.section_markers`). This eliminates the drift surface without merging divergent per-type logic.

### `commit_sha` in dream patch telemetry

Including the git commit SHA in the `memory.dream.patch_applied` telemetry event was proposed and decided against.

The realistic forensics use case — "when did Dream change this page?" — is served by `git log memory/entities/<type>/<slug>.md`. The operator knows the entity ref from the event; git history gives the full answer without the SHA in telemetry. No dashboard exists that needs commit SHAs at scale. Including the SHA would require restructuring the telemetry call site to fire after the commit, which is non-trivial work for nobody.

### Unified `compose_embedding_text` dispatcher

A single public classmethod routing on input type to the two specialised embedding composers was added and then removed.

The dispatcher was never called anywhere in the codebase. An `EntityPage` and a `MemoryEntry` embed structurally different fields, so the two composers are genuinely divergent — not two implementations of one rule. Every real caller already holds a concrete type, so routing through an `isinstance` dispatcher is pure indirection. The anti-drift goal is met by having exactly one composer per indexable type, each the sole authority for its type.

### Temporal decay extended to stable, entity, and corpus classes

Extending temporal decay to classes beyond `episodic` and `session_summary` was proposed and decided against.

`stable` carries explicit user-asserted facts that become invalid by contradiction, not by age. Entity pages are updated by Dream on every consolidation pass; old pages are consolidated, not stale. Corpus entries (ingested documents) become stale when superseded or removed, not when time passes. The `episodic` and `session_summary` classes decay because they represent recent observations whose relevance decreases as they age out of working memory. The per-class defaults match the semantics of each class. Operators who want a different shape use the `class_half_life_overrides` config field.

**Note:** temporal decay was subsequently removed entirely from the search pipeline. Search does not pre-judge recency; every hit carries `valid_from` and the LLM does the temporal reasoning from context.

### Auto-backup of memory workspace

A system-managed backup mechanism for the workspace was proposed and decided against.

The workspace is a normal git repo. An operator who wants off-host durability runs `git remote add origin <url>; git push origin main`. For non-git backups, `rsync`, `tar`, and `restic` cover every backup shape. Building a parallel `memory.backup.enabled` mechanism would replace a well-understood git workflow with a custom configuration surface that does the same thing through a less standard interface. Adding auto-backup introduces failure modes the system did not have: network failure handling, credential management, recovery from corrupted backup state. Mainstream LLM-in-the-loop systems leave backup to the operator.

### Data deletion (GDPR-like cascading delete)

A "forget everything about entity X" operation cascading across entity page, archive, provenance references, index rows, and git history was proposed and decided against.

durin is single-operator by architecture: the operator owns the workspace and there is no second user whose right-to-be-forgotten the operator must honor. The trigger "first external user via bot channel" presupposes a multi-tenant hosted deployment that the system is explicitly not built for. The git history rewriting required for honest deletion — `git filter-repo`-class operations — changes every commit hash from the touched point forward, breaking external clones. The operator-level escape hatch (`rm memory/entities/person/X.md; git commit`) is sufficient and keeps the decision conscious.

### Dedicated archive index

A parallel vector/FTS5/SQLite index over `memory/archive/` was proposed and decided against.

Recovery is rare by design. Frequent archive queries would signal misuse of the recovery surface, not a workload to optimize for. The current on-demand walk is fast enough at any realistic archive size. The existing surfaces (agent-visible `memory_search(scope='archive')`, `durin memory expand`, shell find/grep) already cover the use case without a parallel index.

### HyDE (Hypothetical Document Embeddings)

Embedding a hypothetical document that would answer the query, rather than the query itself, was considered in both hot-path and cold-path variants and decided against.

The hot-path variant (LLM call before each search) is the same anti-pattern as the query rewriter: it saturates rate limits, breaks the deterministic-retrieval invariant, and costs latency the system optimized away. The cold-path variant ("pre-generate hypothetical documents during Dream") has no unique problem to solve: Dream already consolidates raw observations into entity pages that are then embedded and retrieved. Layering HyDE on top would be Dream generating Dream input. Mainstream LLM-in-the-loop agent systems ship without HyDE.

### Reflection / pattern detection

A periodic process detecting recurring behavioral patterns ("X tends to postpone PRs in long sprints") and emitting reflection nodes was proposed and decided against.

Dream tier 1 consolidation plus LLM-in-the-loop reading already covers pattern queries at acceptable quality. Reflection nodes add a fifth memory class with its own write path, indexer hooks, sync invariant, and invalidation graph — roughly 300–500 LOC of complexity carried for every workspace whether or not pattern queries are frequent. Mainstream agent-memory systems do not ship reflection nodes. The lower-cost alternative if pattern queries prove expensive is caching the LLM-generated pattern read at the agent loop level, not introducing a new memory class.

### Concepts as first-class hypergraph mediators (GAAMA pattern)

Adopting GAAMA-style Personalized PageRank over a hypergraph of concept nodes as the retrieval mechanism was proposed and decided against.

durin already stores concepts as first-class `topic` entities on the same footing as persons, projects, and places. What it does not do is mediate retrieval through those topics via PageRank — and that mediation IS the GAAMA mechanism. Adopting it would require building a hypergraph index, choosing between offline (stale state) and per-query (added latency) PageRank, replacing RRF fusion, and rebuilding the test surface — a roughly 2000-LOC architectural rework. The LLM agent is the analytical mediator in durin's design; concept-level queries are answered at the LLM, not at the index.

### Multiple specialized search tools per modality

Separating tools by retrieval modality (semantic\_search vs keyword\_search) was considered. Mainstream pattern is a single tool with internal routing. LLMs do not reliably choose between similar-purpose tools. durin uses one `memory_search` tool with optional `keywords` for explicit literal signaling and internal auto-routing by query pattern.

### Exposing RRF/BM25 weights to the agent

No surveyed system exposes fusion weights to the LLM. LLMs do not have intuition for numeric weights. Weight tuning belongs to operator config and the onboarding wizard, not to tool parameters.

### Retrieval mode enum in the search tool

Exposing a search mode enum (like cognee's `GRAPH_COMPLETION | RAG_COMPLETION | CODE | CHUNKS`) was considered and rejected. Mode enums add complexity to tool descriptions and LLMs pick the wrong mode. Auto-routing by query pattern achieves similar effect without burdening the LLM.

### SPLADE / ColBERT (learned sparse or multi-vector embeddings)

Replacing the current dense bi-encoder + BM25 hybrid with SPLADE (learned vocabulary expansion) or ColBERT (multi-vector late interaction) was proposed and decided against.

durin already runs a dense + sparse hybrid: MiniLM-L12-v2 fused via RRF with FTS5 BM25. SPLADE and ColBERT are paradigm shifts: SPLADE requires a sparse vector store (LanceDB stores dense vectors), ColBERT stores one vector per token (10–100× storage growth per document) and requires a specialized PLAID index. Both break the current LanceDB-and-FTS5 setup the rest of the system is built around. The operator-accessible response if a recall plateau surfaces is to swap the embedding model via config (`MemoryEmbeddingConfig.model`) — a five-minute operation with no schema change. Mainstream LLM-in-the-loop systems ship with dense bi-encoder + BM25.

### Versioning as a separate agent tool

A dedicated `memory_history` MCP tool for git log queries was not implemented. Git history is available to Dream internally (its prompt includes recent commit history) and to the operator via any git CLI. The LLM agent accesses entity history by issuing reads against the entity page — no dedicated agent-facing tool needed.

### Active forgetting policies

Policies that automatically delete or compress old entries were considered and decided against.

The archive of consolidated episodic already handles the primary case: post-consolidation, episodic entries move to `memory/archive/episodic/` and leave the active retrieval surfaces (vector index, FTS5, default grep), so they stop competing for ranking while remaining recoverable. Deeper forgetting — deleting the 100 archived originals to keep one summary — violates the reversibility principle. If Dream consolidates wrong, source observations must be recoverable. Disk is cheap; bad consolidations are expensive to recover from without the original evidence. Mainstream systems that delete rather than archive have lifecycle policies because they have no archive concept; durin's archive IS the middle state those policies are trying to create.

### Trust scoring per source

Ranking user-provided memories above LLM-inferred memories via an explicit trust score was considered. durin's classes (stable vs episodic) already encode this implicitly — `stable` means explicitly marked durable. Not enough distinct trust tiers exist to justify a separate scoring system.

### Tool call history as structured memory

Structuring the agent's own tool-call history as a queryable memory layer was considered. Sessions already contain tool calls in their JSONL records. Grep over `sessions/<id>.jsonl` covers ad-hoc retrieval. No dedicated structured layer is needed.

### Cross-entity consistency checks (scheduled scan)

A periodic walk flagging inconsistencies between entity pages — relation reciprocity gaps, attribute conflicts, temporal contradictions — was proposed and decided against.

Duplicates (the dominant inconsistency class) are already handled by absorb-judge. Relation reciprocity is not a system invariant: "Marcelo knows X" does not imply "X knows Marcelo." Other inconsistencies surface at LLM read time, where they matter — the agent that notices a conflict reads `git log` on the affected entity. A scheduled scan that emits warnings produces output nobody reads. The implementation cost is 300–500 LOC for an output the operator can already produce on demand by asking the LLM.

---

## 3. Mechanisms in other systems NOT adopted

We surveyed mem0, Letta/MemGPT, Zep, Graphiti, Cognee, Hermes-Agent, OpenClaude, OpenClaw, OpenHands, GAAMA. The items in §2 cover the major mechanisms. Additional brief notes:

- **Body in LanceDB:** see §2 "Body stored inside LanceDB rows."
- **HyDE:** see §2 "HyDE (Hypothetical Document Embeddings)."
- **Reflection nodes:** see §2 "Reflection / pattern detection."
- **Hypergraph mediation:** see §2 "Concepts as first-class hypergraph mediators."
- **SPLADE/ColBERT:** see §2 "SPLADE / ColBERT."
- **Active forgetting:** see §2 "Active forgetting policies."
- **Cross-encoder default ON:** see §2.
- **Mode enum in search tool:** see §2.
- **Multiple modality tools:** see §2.
- **Versioning as agent tool:** see §2.
- **Trust scoring:** see §2.
- **Tool call history layer:** see §2.

---

## 4. Decisions where durin explicitly chose against the mainstream

| Topic | Mainstream | durin choice | Rationale |
|---|---|---|---|
| Cross-encoder default | Mostly opt-in OFF | Opt-in OFF | Agreed with mainstream — latency cost unjustified by default. |
| MMR | Rarely implemented | Not in MVP | Agreed — archive-of-consolidated-episodic eliminates the primary duplication source. |
| Versioning as a tool | Not standard | git history via Dream prompt + CLI | Reuse what exists; no dedicated agent-facing tool. |
| LLM in hot path | Most avoid; cognee uses LLM classifier | Strictly avoided | Cost and latency. |
| Multi-vector per facet | Rare (some research) | Single vector per doc | Simplicity. |
| Closed attribute catalog | mem0 has implicit catalog via LLM tendencies | Open, with drift control via existing\_schema | Generalist use cases. |
| Tool sectioning markers | Rare (hermes uses `<memory-context>`) | Used (CANONICAL/FRAGMENT/SESSION/INGESTED) | Structural communication outperforms imperative instructions in tool descriptions. |
| Cold-path consolidation | mem0 syncs at write | Async batched Dream | No write-path latency for the user; lower cost. |
| Archive instead of delete | Most delete or overwrite | Archive to `memory/archive/` | Reversibility. Bad consolidations must be recoverable. |
| Structural SQL index | Some ship SQLite per-entity stores | Not adopted | LLM agent is the analytical layer; index adds schema migration and drift risk. |

---

## 5. Lessons learned

### Lesson 1 — Tool description is a weak signal

Imperative instructions in tool descriptions ("USE BEFORE answering", "trust this") do not reliably change LLM behavior. Structural patterns — markers in results, distinct tool names with specific purpose — work better.

**Implication:** prefer structural communication. When you must use text, make it declarative ("issue 2–3 searches for compound questions") not imperative ("ALWAYS use multi-query").

### Lesson 2 — Fix causes, not symptoms

When retrieval fails, the temptation is to add a downstream patch (rewriter, pin, special mode). The right approach is to ask what upstream weakness is causing the failure.

The LLM query rewriter compensated for five upstream issues: frontmatter not entering the embedding, body truncation, a small embedding model, missing aliases, and cross-lingual limits of the embedding model. Fixing those upstream made the rewriter unnecessary.

**Implication:** before adding a new component, list the upstream causes it compensates for. Fix one of those instead.

### Lesson 3 — Archive over delete

Recoverability is cheap when designed in; expensive when bolted on.

Archiving consolidated episodic entries preserves provenance and enables recovery if Dream consolidates wrong. It also eliminates the main duplication problem in retrieval — archived entries leave the active search surfaces.

**Implication:** when removing data from active state, move it (archive) rather than delete. Disk is cheap; bad consolidations are expensive.

### Lesson 4 — Markdown as source of truth

When index and source of truth diverge, the source of truth must win. This requires every index to be a derivative reconstructible from the source.

All indices in this corpus (LanceDB, FTS5) are reconstructible from `.md` files. `durin memory reindex` is always available.

**Implication:** never store data in an index that does not also exist in markdown. The index is acceleration; markdown is truth.

### Lesson 5 — Single tool with internal routing over multiple specialized tools

Agents struggle to choose between similar-purpose tools. Mainstream systems use a single search tool. Cognee tried a mode enum and added a "FEELING\_LUCKY" option because the agent picked wrong modes.

**Implication:** if you can route by query pattern internally, do that. Do not make the LLM pick.

### Lesson 6 — Cold-path investment pays compound returns

Building Dream correctly (consolidation, archive, dedup, drift control) eliminates many downstream problems: duplication, drift, retrieval noise.

Archive plus consolidation makes MMR unnecessary, makes pin-by-modality unnecessary, and makes drift control structural rather than per-query.

**Implication:** invest in cold path early. Hot-path patches stack up as technical debt.

### Lesson 7 — Sync tests must exercise behavior, not just strings

Tests that compare doc strings to code strings pass green even when the behavior they describe is absent. This happened: a tool description was "synced" by copying it verbatim from a doc without verifying the schema implemented the promised parameters. The test passed throughout because it compared strings, not behavior.

**Implication:** sync tests must exercise the promised behavior end-to-end, not just check that strings match.

### Lesson 8 — Optimisation that violates an architectural principle requires measurement

Storing body in LanceDB was framed as a trade-off that avoids N disk reads. The disk reads were not the bottleneck. The optimisation violated the single-source-of-truth principle for an unmeasured gain.

**Implication:** before breaking an architectural principle for performance, measure whether the thing you are optimizing is actually the bottleneck.

### Lesson 9 — "Deferred with concrete trigger" without a measurable failure mode is functionally discarded

Several items were marked "deferred until X surfaces" where X had no telemetry and no observable failure mode. They would have sat in the backlog indefinitely. When a feature is already covered by existing surfaces and has no unique observable use case, it should be marked discarded with reasoning, not deferred.

---

## 6. Cross-references

- Architectural decisions per module: each module's decisions table (§10 or §14 or §16).
- Cross-corpus decisions: `00_overview.md` §10.
- For the active backlog (work planned or in progress), see the project roadmap.
