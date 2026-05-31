---
title: Memory system — architectural overview
version: 0.1-draft
status: current — describes the shipped system (P11 era, 2026-05-30)
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
related: ../archive/40_exploracion_datos_y_relaciones.md (prior exploration, Spanish)
---

# Memory system — architectural overview

This document is the **entry point to the specification corpus**. It defines the global design, the guiding principles, the layered diagram, the glossary, and the index of all other documents. Each module of the system lives in a dedicated doc inside this folder (`docs/architecture/memory/`).

**How to read this corpus:** start here. Then go to the doc covering the module you need to understand or modify. Each doc is the source of truth for its scope; no fact should be duplicated across docs (only referenced via `[[link]]`).

> **Heads-up — two consolidation tracks.** This corpus describes the **entity-centric** memory subsystem (the knowledge graph the agent builds about its world). A second, older consolidator — the legacy `dream` cron registered in `cli/commands.py:1604` — runs in parallel to maintain `MEMORY.md` / `SOUL.md` / `USER.md` and create `skills/<name>/SKILL.md`. It's not duplication; it's a separate layer (the agent's *own* working memory and identity, not knowledge about the world). See `05_dream_cold_path.md::§0` for the side-by-side comparison and the rationale for keeping them separate.

---

## 1. Goals

The durin memory system must allow the agent to:

1. **Remember across sessions.** Information learned in one conversation is available in the next, regardless of elapsed time.
2. **Distinguish evidence from synthesis.** Raw conversations, ingested documents, atomic observations, and canonical knowledge are different things with different roles.
3. **Find relevant information fast.** Hot path (each user turn) responds in milliseconds without external LLM calls for retrieval.
4. **Maintain coherence at low operational cost.** Consolidation, deduplication, and archival run on the cold path (Dream) without blocking the user.
5. **Be hand-editable.** Markdown as source of truth — the user can inspect and modify any data without specialized tools.
6. **Be recoverable.** Indices (vector, lexical, structural) are derived. Deleting them does not destroy memory.
7. **Be generalist.** The data model serves coders, sales, support, students, makers, personal assistance — not a single domain.

## 2. Non-goals

Scope boundaries:

1. **Not a classical knowledge graph** with SPARQL/RDF. The graph is built on markdown + indices, not on a triple store.
2. **Not a reasoning system.** It provides retrieval and structure; reasoning is the final LLM's job.
3. **Not multi-tenant.** Single-workspace per installation. Multiple users can interact with the agent via channels (Telegram, Discord, Slack, etc.); memory is **shared across all interacting users** — no per-user isolation. Each user (including the installation owner) is modeled as a `person:<name>` entity.
4. **No LLM in hot path** (search, retrieval, ranking). LLMs are used only on the cold path (Dream, ingestion).
5. **Not a replacement for the context window.** The final LLM remains the synthesizer; memory provides material.
6. **No history rewriting.** Raw sessions are immutable; the system synthesizes on top but does not modify the evidence.

## 3. Guiding principles

All design decisions rest on these principles. Any decision violating one requires explicit justification.

| # | Principle | Implication |
|---|---|---|
| **P1** | Markdown is source of truth | All knowledge lives in editable `.md` files. Indices are reconstructible derived artifacts. |
| **P2** | Hot path has no LLM | Search and ranking are deterministic. LLMs only in consolidation/ingestion (cold path). |
| **P3** | Separated layers, clear responsibilities | Evidence (sessions/ingested) ≠ atomic facts (episodic/stable) ≠ synthesis (entities). |
| **P4** | Generalist data model | Free attributes + relations. No closed catalog. Cleanup at write-time (Dream sees existing schema). |
| **P5** | Relations are first-class only if they carry information | Pure mentions are not materialized. The vector index covers them. |
| **P6** | Structure communicates better than instructions | Markers (CANONICAL/FRAGMENT) and timestamps. NOT "trust this" instructions in the prompt. |
| **P7** | Reversible decisions | Archive instead of delete. Provenance is always traceable. |
| **P8** | Fix causes, not symptoms | If retrieval fails, fix the index or the model — don't add patches (lesson from the G3.b experiment). |

## 4. Layered diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                       AGENT LLM (hot path)                        │
│                                                                   │
│   Receives structured results, decides, responds to user          │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             │ tool calls
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│   AGENT TOOLS  (memory_search, memory_store, memory_ingest, ...)  │
│                                                                   │
│   Adapter layer between LLM and engines. Sectioned results with   │
│   structural markers.                                             │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                ┌────────────┴────────────┐
                ▼                          ▼
┌────────────────────────┐   ┌────────────────────────────────────┐
│  SEARCH PIPELINE       │   │  WRITE PIPELINE                     │
│  (hot path, no LLM)    │   │  (cold path, Dream + LLM)           │
│                        │   │                                     │
│  intent router         │   │  triggers (threshold, manual, cron) │
│  ↓                     │   │  ↓                                  │
│  vector + BM25         │   │  Dream consolidator (LLM)           │
│  ↓                     │   │  ↓                                  │
│  weighted merge        │   │  entity dedup / absorb-judge        │
│  ↓                     │   │  ↓                                  │
│  entity-aware rerank   │   │  apply (write + archive + reindex)  │
│  ↓                     │   │                                     │
│  cross-encoder rerank  │   │                                     │
└──────────┬─────────────┘   └────────────────┬────────────────────┘
           │                                   │
           ▼                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│                    DERIVED INDICES                                │
│                                                                   │
│   LanceDB (vector)  │  FTS5 / SQLite (lexical BM25)                │
│                                                                   │
│   (Structural SQLite — decided against, see §10 #1)               │
│                                                                   │
│   All reconstructible from the layer below.                       │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│                 SOURCE OF TRUTH (disk, markdown)                  │
│                                                                   │
│   sessions/         (raw conversations, immutable)                │
│   ingested/         (raw documents, immutable)                    │
│   memory/                                                         │
│     corpus/         (chunks from ingested + snapshots)            │
│     episodic/       (atomic observations)                         │
│     stable/         (stable notes)                                │
│     pending/        (buffer)                                      │
│     archive/        (consolidated, recoverable)                   │
│     entities/       (typed canonical synthesis)                   │
└──────────────────────────────────────────────────────────────────┘
```

## 5. Main flows

### 5.1 Read path (hot, every user turn)

```
1. User sends a message
2. Agent decides to call memory_search(query, [keywords])
3. memory_search runs:
   a. Intent router classifies the query (semantic, structural, mixed)
   b. Vector search in LanceDB
   c. BM25 search in FTS5
   d. Weighted merge → top-K candidates
   e. Entity-aware reranking (RRF)
   f. (Future) Cross-encoder reranks top-50 → top-10
   g. Sectioned output: results grouped under CANONICAL / FRAGMENT / SESSION / INGESTED markers
4. Agent receives structured results, synthesizes the response
```

### 5.2 Write path (cold, async)

```
1. Agent or user calls memory_store / memory_ingest
2. Tool creates entry in memory/{episodic|stable|corpus}/<ts>.md
3. Entry is vectorized immediately (re-embed-on-write) → LanceDB + FTS5
4. Threshold trigger evaluates: if entity accumulated N entries, dispatch Dream
5. Dream (daemon thread, locked, throttled):
   a. Reads post-cursor entries for entity X
   b. Receives in prompt: existing_schema + known URIs
   c. LLM emits JSON Patch over the entity page + body delta + commit
   d. apply(): validates YAML, updates .md, advances cursor
   e. Entity dedup (absorb-judge if alias overlap)
   f. Archive: consolidated episodic entries → memory/archive/episodic/
   g. Index re-derivation (LanceDB + FTS5)
```

## 6. Glossary

| Term | Definition |
|---|---|
| **Source of truth (SoT)** | The canonical data from which everything else is derived. In durin, the `.md` files. |
| **Hot path** | Operations per user turn. Latency-critical. No LLM. |
| **Cold path** | Deferred operations (Dream, ingestion). LLM permitted. |
| **Entity** | Typed synthesis with identity (`person:marcelo`, `bug:auth_leak`). Lives in `memory/entities/<type>/<slug>.md`. |
| **Attribute** | Primitive fact about an entity (`email: x@y.com`). Free-form dict, no closed catalog. |
| **Relation** | Connection to another entity (`spouse → person:susana`). List of objects. First-class only if it carries info. |
| **Provenance** | Traceability: which entry created/updated each attribute/relation. |
| **Episodic** | Short atomic observation. Raw material for Dream. |
| **Canonical** | Marker for consolidated entity pages. Indicates "stable" info. |
| **Fragment** | Marker for post-cursor episodic (recent, not yet consolidated). |
| **Dream** | Cold path that consolidates episodic into entity pages. |
| **Cursor** | Per-entity timestamp up to which Dream has processed. |
| **Hot layer** | Injection of canonical pages + recent fragments into the LLM's prompt before tool calls. |
| **Index** | Derived structure (LanceDB, FTS5, SQLite) that accelerates retrieval. Reconstructible. |
| **Owner** | The person who installed the agent. Modeled as a `person:<name>` entity like any other user — there is no special "owner" record. The distinction (if any) lives in channels/auth config, not in memory. |
| **Interlocutor** | Any user who interacts with the agent via a channel. The owner is one interlocutor among many; all are modeled the same way in memory. |

## 7. Current state vs final state (snapshot)

This section will be replaced by a detailed roadmap once all modules are specified. For now, a general picture:

Audit E24 (2026-05-28) rebuilt this snapshot — most rows had been
stale since Phase 1.9 / Phase 3 shipped. Per-row pointers go to
the specific module where the work landed.

| Component | Current state | Notes |
|---|---|---|
| **Data types** | 5 classes (`stable`, `episodic`, `corpus`, `pending`, `session_summary`) + `entity_page` (separate model) | `MEMORY_CLASSES` in `durin/memory/paths.py`. `session_summary` was added in audit A10 (was 4 classes before). |
| **Vector index** | LanceDB + intfloat/multilingual-e5-small (default since 2026-05-30; replaced MiniLM-L12) with v2.a embedding text for entity pages (name + aliases + rendered_frontmatter + body) | Shipped audit E9 (2026-05-28). Schema v4. E5-family prefix (`passage: ` / `query: `) applied automatically by FastembedProvider. |
| **Lexical** | FTS5 BM25 (`unicode61` + `trigram` + `like_substring` for short CJK) | Shipped Phase 3. Auto-detection of identifier tokens (P3.3) boosts lexical weight on URL/email/UUID/file-path queries (audit E14). |
| **Fusion** | Cross-source RRF over per-source rank lists | Shipped Phase 3 (`durin/memory/rrf_fusion.py`). Score-scale invariant. |
| **Reranking** | Entity-aware RRF (default ON) + cross-encoder (opt-in, default OFF) | Cross-encoder opt-in via `memory.search.cross_encoder.enabled`. Pre/post-cursor logic in entity_ranker restored audit E11. MMR deferred. |
| **Recency handling** | The LLM does it. Every hit carries `valid_from`; the agent reasons about which fact is current given the question's intent. Search does not pre-filter by age. | Implicit decay was removed 2026-05-30 after the LoCoMo conv-5-q20 chicken case showed it perjudicates factual atemporal queries. Rationale in doc 03 §10. |
| **Versioning / audit** | Git history exists; webui surfaces it via memory dashboards (Phase 4 dashboards shipped) | Dream uses git log internally; operators access via any git CLI or webui. |
| **Tools** | `query` + `keywords` + `scope` + `level` + `limit` + sectioned results | Shipped Phase 5 d1. `limit` exposed audit A3. |
| **Dream** | Consolidates facts into entities via JSON Patch + BODY_DELTA + COMMIT (Phase 1.9). Archives consumed episodic. Absorb-judge for entity dedup (opt-in). 3-strike quarantine on structural failures. | All shipped Phase 1.9 (commit `6aafc3f`). Auto-absorb skips user-authored entity pages (audit E19). |
| **Sessions** | Session summaries are a first-class memory class (`session_summary`) indexed by FTS5 + LanceDB | Shipped audit A10 (2026-05-28). `Consolidator._persist_last_summary` writes `memory/session_summary/<key>.md`. |

## 8. Document index

The following documents live in `docs/architecture/memory/` and are the detailed specification of the system. Each is the source of truth for its scope.

| # | Document | Scope |
|---|---|---|
| 00 | `00_overview.md` (this) | Overview, principles, diagram, glossary, index |
| 01 | `01_data_and_entities.md` | Data types, schemas, lifecycle, entity model (attributes + relations + provenance), naming, paths |
| 02 | `02_indexing.md` (pending) | LanceDB vector index, FTS5 lexical, SQLite structural (if applicable), re-derivation, file watcher |
| 03 | `03_search_pipeline.md` (pending) | Intent router, vector search, BM25, weighted merge, entity-aware ranker, cross-encoder |
| 04 | `04_agent_tools.md` (pending) | memory_search, memory_store, memory_ingest, memory_drill, result sectioning, markers |
| 05 | `05_dream_cold_path.md` (pending) | Triggers, consolidator, JSON Patch, schema preservation, archive, dedup, absorb-judge, cursor |
| 06 | `06_prompts_and_instructions.md` (pending) | identity.md Memory section, tool descriptions, marker conventions, LLM-facing messages |
| 07 | `07_telemetry_and_observability.md` (pending) | Events, metrics, dashboards, health alarms |
| 08 | `08_scope_and_discarded.md` (pending) | Revised non-goals, lessons from discarded experiments (G3.b), unadopted mechanisms from other systems |
| 09 | `09_implementation_roadmap.md` (pending) | Concrete phasing: current state to final state, step by step, with done criteria |

The number and naming may be adjusted as we write. What matters is that each doc has a closed scope and explicit cross-references to related ones.

## 9. How this corpus is modified

| Change type | Process |
|---|---|
| **New decision** | Discussion → update the affected module's doc → update this overview if it affects diagram/principles |
| **Refactor** | Change in the module's primary doc + cross-ref adjustment |
| **Feature discarded** | Move description to `08_scope_and_discarded.md` with rationale |
| **Lesson learned** | Add to `08_scope_and_discarded.md` even if no other doc changes |

Each doc has `version` + `last_updated` in frontmatter. Substantive changes bump the version.

## 10. Cross-corpus decisions

These decisions impact multiple modules. Resolutions below; details live in the affected docs.

### Resolved (2026-05-27)

| # | Decision | Resolution | Affects |
|---|---|---|---|
| **1** | SQLite structural (counting / analytical queries via JSON_EXTRACT) | **Decided against** (audit B-5, 2026-05-28; doc 08 §2.5). Grep + parse on-the-fly handles MVP scale; FTS5 over rendered frontmatter covers attribute lookups; mainstream systems ship without a structural layer; the LLM agent is the analytical layer when one is needed. The "deferred" wording before B-5 was a soft defer with no observable trigger — upgraded to discarded. | `02_indexing.md`, `08_scope_and_discarded.md` §2.5 |
| **2** | Cross-encoder reranker (top-50 → top-10 with dedicated reranking model, no LLM in hot path) | **In MVP as opt-in, OFF by default.** Multilingual cross-encoders add 300-1500ms latency on CPU, breaking the default search budget; comparable systems (mem0, graphiti) ship reranking opt-in too. Default model when enabled: `jinaai/jina-reranker-v2-base-multilingual`. User surface: workspace config + onboarding wizard question + web dashboard toggle. | `03_search_pipeline.md` |
| **3a** | MMR (Maximal Marginal Relevance — diversity in top-K) | **Not in MVP, deferred.** Original concern was top-K redundancy, but archive of consolidated episodic (§3.6 doc 01) eliminates the primary source of duplication. The remaining concern (corpus chunks from the same long source) is handled differently via a per-source cap in sectioning (§12.4 doc 03). Mainstream systems don't implement MMR either. If post-MVP bench shows residual duplication, the algorithm is standalone and easy to add. | `03_search_pipeline.md`, `08_scope_and_discarded.md` |
| **3b** | Temporal decay | **Removed 2026-05-30.** Search must not pre-judge recency without the question's context. Every hit carries `valid_from`; the LLM does the temporal reasoning. See doc 03 §10 for the removal rationale (LoCoMo conv-5-q20 chicken case). | `03_search_pipeline.md` §10 |
| **4** | Explicit versioning of memory | **In MVP via git history, no dedicated tool.** `memory/.git/` already exists. Dream pipeline reads `git log` internally when preparing its prompt (no MCP tool exposure to the agent). Users access via any git CLI today; web UI rendering is post-MVP and lives outside this corpus. | `01_data_and_entities.md`, `05_dream_cold_path.md` |
| **5** | Active forgetting policies (compress / delete old entries) | **Not in MVP.** Destructive — needs explicit policies for what's deleted, when, recovery. Distinct from #3 (which only affects ranking). Backlog. | `05_dream_cold_path.md`, `08_scope_and_discarded.md` |

### Open

None at the corpus level currently. Module-specific open decisions are listed in each doc's `Open decisions` section.
