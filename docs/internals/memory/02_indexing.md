---
title: Indexing — LanceDB vector index and FTS5 lexical index
version: 0.1-draft
status: current — describes the shipped system (P11 era, 2026-05-30)
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 01_data_and_entities.md
related: 03_search_pipeline.md, 05_dream_cold_path.md
---

# Indexing

This document specifies the two derived indices that accelerate retrieval over the markdown source of truth: a **vector index** (LanceDB + intfloat E5) for semantic retrieval, and a **lexical index** (SQLite FTS5 with BM25) for keyword and phrase retrieval. Both are derived from `.md` files and reconstructible at any time.

**Key invariant:** indices never hold information that isn't in the markdown layer. Deleting `.durin/index/` and rebuilding produces an identical search state. This is the operational guarantee that makes "markdown is source of truth" real.

---

## 1. Scope and non-scope

### In scope for this document

- Schema of each index (LanceDB table layout, FTS5 schema).
- Embedding text composition rules.
- Re-embed and re-index triggers.
- File watcher behavior.
- Archive exclusion enforcement.
- Auto-commit of user manual edits.
- Failure handling and rebuild.

### Out of scope (covered elsewhere)

- How indices are queried — see `03_search_pipeline.md`.
- How tools invoke indices — see `04_agent_tools.md`.
- Dream's write pipeline (which triggers re-index) — see `05_dream_cold_path.md`.
- SQLite structural / analytical index — **not in MVP** (cross-corpus decision #1).

---

## 2. Storage locations

```
<workspace>/
├── memory/                              Source of truth (markdown)
│
└── .durin/
    └── index/
        ├── lance/                       LanceDB tables (vector)
        │   ├── memory.lance/            Main searchable index
        │   └── (no archive table — archive is not indexed)
        ├── fts.sqlite                   FTS5 SQLite database (lexical)
        └── meta.json                    Indexer state: last_full_rebuild, schema_version, etc.
```

`.durin/index/` is in `.gitignore` by default. Deleting it does not destroy memory — only forces a rebuild. The path itself is configurable; the layout is not.

---

## 3. Vector index (LanceDB)

### 3.1 Schema

Each row represents one indexable `.md` file under `memory/` (excluding `memory/archive/` and `memory/pending/`). The row is **metadata + the embedding vector**; the body content lives in the `.md` on disk, which is the single source of truth.

| Column | Type | Description |
|---|---|---|
| `id` | string (PK) | For entries: 12-char content hash. For entity pages: `<type>:<slug>` (e.g. `person:marcelo`). FTS5 calls this `uri` — asymmetry documented in §5.1. |
| `class_name` | string | `episodic`, `stable`, `corpus`, or `entity_page`. FTS5 calls this `type` — asymmetry documented in §5.1. `pending` is in `MEMORY_CLASSES` but never reaches the index (walker/indexer skip it). |
| `summary` | string | For entries: frontmatter `summary` (may be empty). For entity pages: `name (alias1, alias2)` derived at upsert time (audit F23, 2026-05-28 corrected the pre-F23 spec which claimed `name (also: alias1, alias2)` — the shipped composer at `vector_index.py:142` + `:516` joins aliases without an `also:` prefix). |
| `headline` | string | For entries: frontmatter `headline`. For entity pages: the entity `name`. |
| `vector` | fixed-size list of floats | Dim depends on the configured model — default `intfloat/multilingual-e5-small` → **384**; heavy-recall users on `multilingual-e5-large` → 1024. Validated at startup via `VectorIndex._guard_dim_match`. |
| `valid_from` | string | ISO date if the entry frontmatter carries one; empty string `""` when absent. |
| `entities` | list[string] | For entries: frontmatter `entities` field (e.g. `["person:marcelo", "project:durin"]`). For entity pages: `[]` (a page IS the entity; the ranker treats `class_name="entity_page"` specially). Used by `entity_ranker` for the post-cursor boost. |
| `path` | string | Relative path to the `.md` from workspace root. Used by consumers that need to read the body on demand. |

**No `body` column. The body lives on disk.** When a cold-tier caller (`memory_search(level="cold")`) needs the full body, the consumer reads the `.md` via `memory_search._enrich_body`. This is a deliberate architectural choice: the `.md` is the single source of truth; LanceDB is a derivable, disposable cache that can be rebuilt from disk at any time. P2.5 (commit `a266344`) briefly stored the body here as a latency micro-optimisation; audit A4 reverted it because:

1. The latency saved (~5-10 ms for 10 file opens on SSD) was not bottleneck — the LLM call downstream takes seconds.
2. Storing the body duplicated content and opened a drift window between disk edits (e.g., via the file watcher path of P2.3) and LanceDB reads.
3. Doubling the index size compounded at scale.
4. FTS5 already honors the "indexed for search, content lives on disk" model — LanceDB now matches.

See `docs/internals/memory/design_rationale.md` for the full rationale and the lesson on "premature optimisation that violates an architectural principle".

### 3.2 Embedding model

Single global choice per workspace (configurable via `memory.embedding.model`). Default since 2026-05-30: `intfloat/multilingual-e5-small`.

| Property | Default value |
|---|---|
| Model | `intfloat/multilingual-e5-small` |
| Dim | **384** (varies by model — see below) |
| Size | ~450 MB disk / ~200 MB RAM |
| Max sequence | 512 tokens (~1500 chars) |
| Multilingual | Yes — 100+ languages (intfloat training set) |
| Training objective | InfoNCE contrastive retrieval (vs paraphrase generic of MiniLM-L12) |
| License | MIT |

Wizard offers 2 tiers (see `durin/cli/onboard_wizard.py::_EMBEDDING_CHOICES`):
- **Default**: `intfloat/multilingual-e5-small` (above).
- **Heavy**: `intfloat/multilingual-e5-large` (2.24 GB, 1024-dim, MIT) — top quality for large workspaces.

**Why e5-small over the prior MiniLM-L12 default.** `multilingual-e5-small` is fine-tuned FROM the same backbone architecture as `paraphrase-multilingual-MiniLM-L12-v2` (`microsoft/Multilingual-MiniLM-L12-H384`) but with a modern retrieval-specific InfoNCE objective + larger training set. Same dim (384), comparable RAM (~200 MB int8 vs ~280 MB fp32), measurably better recall on MTEB retrieval tasks (+8 points avg). For durin's pattern — agent issues many short tool-call queries per turn — retrieval-tuned embeddings matter more than paraphrase-generic ones. The legacy `paraphrase-multilingual-MiniLM-L12-v2` and `all-MiniLM-L6-v2` (English-only minimum) were retired from the wizard in the same change; they're still loadable via `memory.embedding.model` if explicitly configured (fastembed catalog includes them), but no longer surfaced as options.

**E5 query/passage prefix.** E5-family models were trained with asymmetric prompts: documents prefixed with `passage: ` and queries with `query: `. `FastembedProvider.embed_passages()` and `embed_query()` apply the prefix automatically when the model is detected as E5-family (see `_is_e5_family` in `durin/memory/embedding.py`). Skipping the prefix degrades recall measurably (~2-5pp on MTEB retrieval). For non-E5 models the methods pass through unchanged.

**Custom model registration.** fastembed's default catalog does not include `multilingual-e5-small`. `durin/memory/embedding.py::_register_custom_models` calls `TextEmbedding.add_custom_model` at module load to register it (pointing to the official ONNX export at `intfloat/multilingual-e5-small/onnx/model.onnx`). Idempotent: if the model later lands in fastembed's catalog upstream, the registration becomes a no-op without code change here.

The model is **not configurable per-row**. All vectors in the index share this model. Changing the model requires a full rebuild (`durin memory reindex`). The model identifier is stored in `<workspace>/.durin/index/meta.json`; `VectorIndex._guard_dim_match` checks the on-disk table's vector dim against the provider's reported dim at every read/write and raises `VectorIndexDimensionMismatch` if they differ, so the search pipeline can fall back to lexical instead of mixing 384-dim and 1024-dim rows.

**Provider abstraction.** `durin/memory/embedding.py` defines an `EmbeddingProvider` ABC plus a concrete `FastembedProvider` (uses ONNX in-process, no GPU required). The abstraction is so adding a new provider (e.g., for a different model family, a remote inference endpoint, or quantized variants) is a one-class addition without touching the rest of the indexer. The provider validates model identifier at construction and exposes telemetry hooks for load + per-embed timings.

### 3.3 What is indexed vs not

| Indexed (rows present) | Not indexed |
|---|---|
| `memory/entities/<type>/<slug>.md` (class_name = `entity_page`) | `memory/archive/**` (decision §3.6 of doc 01) |
| `memory/episodic/<id>.md` (post-cursor + pre-cursor both) | `memory/pending/<id>.md` (intake buffer; walker/indexer skip it) |
| `memory/stable/<id>.md` | `sessions/<key>.jsonl` (the raw event stream; the rendered `.md` view is what gets indexed — see §3.3.2) |
| `memory/corpus/<id>.md` | `ingested/<id>/source.*` (raw artifacts; only the derived `memory/corpus/<id>.md` chunks are indexed) |
| `memory/session_summary/<sanitized_key>.md` (audit A10 — see §3.3.1) | |
| `sessions/<key>.md` — FTS only, one row per turn (schema v6 — see §3.3.2) | |

> **§3.3.2 Raw session turns (session-fts, schema v6, 2026-06-09)**: the indexer's third pass walks `sessions/*.md` (the deterministic markdown views) and upserts one FTS row per `## turn-N` block: `uri=sessions/<key>.md#turn-N`, `type="session"`, text = the turn body including its `**role** · timestamp` header (so a hit shows when it was said without a schema change). The uri deliberately matches the grep path's anchored uri shape so RRF fusion accumulates both sources (the H28 same-uri principle). Per-turn rows — not per-file — so BM25 isn't diluted by transcript length. The preamble and `## consolidated-1` note are boilerplate and not indexed. Sessions are NOT vector-indexed (embedding cost); the compaction summary covers the semantic layer via `session_summary`. Reactive path: `SessionManager.save` calls `reindex_session_file` after regenerating the `.md` — incremental (only turn uris missing from `fts_meta` are inserted), so saves stay O(new turns). Rationale: sessions were grep-only, which structurally capped raw conversational content at `w_grep = 0.3` — a session holding the best literal answer could never outrank an indexed entry (see doc 03 §6-§7); and since the legacy consolidator was removed, no dream pass distills general session content into indexed entries anymore.

> **§3.3.1 Session summaries (audit A10, 2026-05-28)**: when the consolidator persists a session summary (`Consolidator._persist_last_summary` in `durin/agent/memory.py`), it writes the canonical copy to `memory/session_summary/<sanitized_key>.md`. Pre-A10 sessions kept the text in `session.metadata["_last_summary"]` (JSON sidecar); A10 picks the single-source-of-truth path per A4 lessons — the JSON field is dropped on the next compaction and the markdown becomes authoritative. The walker picks the directory up automatically (`MEMORY_CLASSES` now includes `session_summary`), the indexer assigns `class_name="session_summary"`, and A9's 120-day half-life applies. The agent-facing `memory_store` enum deliberately excludes `session_summary` — only the compactor produces these rows.

The shared workspace walker (`walk_memory(workspace, include_archive=False)`) is the single chokepoint. Indexer, ranker, alias bootstrapper, and any future scanner all consume its output.

---

## 4. Embedding text composition

The text passed to the embedding model determines what the vector represents. This section specifies exactly what gets composed for each indexable `.md`. Composition is **type-specific** — an entity page and a memory entry embed structurally different fields — so there is exactly **one authoritative composer per indexable type**, and this section is the single source of truth for their rules:

- `EntityPage` → `VectorIndex._compose_entity_page_text` (name + aliases + rendered_frontmatter + body, 1500 chars). See §4.2.
- `MemoryEntry` → `VectorIndex._embed_text` (headline + summary + entities + body, 1500 chars). See §4.3.

Each composer is the sole place that builds embedding text for its type, so the entity-page path and the entry path cannot drift — that is the anti-drift intent originally tracked as audit F12. A unified `compose_embedding_text(item)` dispatcher over the two was tried and reverted: it added an `isinstance` indirection over genuinely-divergent per-type logic without unifying anything (every caller already holds a concrete type).

### 4.1 Common rules

- Hard char budget: **1500 chars**. Anything beyond is truncated.
- Composition order: most-distilled signal first, longest-and-truncatable last.
- Joiner: `\n\n` between sections.
- Whitespace inside individual sections preserved (CJK-safe).

### 4.2 Entity pages

**Shipped (v2.a, audit E9 2026-05-28):** `name + aliases + rendered_frontmatter + body`, in that order, until 1500-char budget exhausted. The optional `summary` slot from the v2 spec is **decided against** (audit G6, 2026-05-28) — see "`summary` slot — decided against" below and (see design_rationale.md).

**The composed text deliberately omits the entity's own `type:` prefix** (Phase 0.1 finding). The embedded text is the bare name/aliases, never `project:durin`. Phase 0.1 measured `project:durin` vs `durin` at cosine **0.517**, against **0.755** for the bare name — the literal `type:` token introduces noise that pulls the page centroid away from the natural-language query (which never contains a `type:` prefix). The type lives only as structural metadata (`class_name` / `entities` columns), never as embedding tokens. (This is also why relation URIs are stripped to slug-only — see "Why slug-only" below.)

Concretely for an entity page like Marcelo:

```
Marcelo

Aliases: Marcelo Marmol, 马塞洛

Email: marcelo@mxhero.com. Phone: +34123. Current residence: Spain. Spouse: Susana (since 2010). Maintains durin (since 2024-01).

(Optional Dream-generated summary if body > budget.)

(Body prose, truncated to fill remaining budget.)
```

**Rendered frontmatter** translates the structured `attributes` and `relations` into prose sentences. Rules:

| Frontmatter element | Rendered as |
|---|---|
| `attributes.<key>: <value>` | `<Key.title()>: <value>.` |
| `attributes.<key>: { current: <v>, history: [...] }` (stateful) | Renders `<Key.title()>: <current>.` Historical values are not rendered to the embedding text (to avoid centroid drift toward defunct facts). |
| `relations[i] = { to: <uri>, type: <t>, since: <date>, ... }` | `<type.title()>: <slug> (since <date>).` See "Why slug-only and not the target's resolved name" below — audit G5 (2026-05-28) tightened the F22 defer. |
| `provenance` | **Not rendered.** Internal metadata, no retrieval value. |
| `created_at`, `updated_at` | **Not rendered.** Internal timestamps. |

**Why slug-only and not the target's resolved name** (audit G5,
2026-05-28, tightening the F22 defer): the v1 spec proposed
rendering relations as `<Type>: <to_name_resolved> (since <date>).`
where `to_name_resolved` reads `name:` from the target entity's
`.md` file. The shipped composer only strips the `type:` prefix
from the URI to surface the slug. We have left this gap open
intentionally — the explanation is long enough that the next audit
pass should not re-litigate it without new evidence.

**Why the current behaviour might already be sufficient.** Most
slugs in practice are derived from the entity's name
(`person:marcelo`, `project:durin`); slug == name modulo case and
separator. In those cases, slug-only puts the relevant tokens into
the centroid: a relation `Spouse: susana` already contains the
string `susana`. The cases where resolution would materially help
are those where `slug != name` (e.g. `person:m_canonical` with
`name: Marcelo Marmol`); we have no data showing this is common
enough in real workspaces to justify the cost.

**Why we did not just ship it preventively.** Resolution adds a
disk read per relation per embed: each `_render_frontmatter` call
would need to `EntityPage.from_file(workspace / memory / entities /
<type> / <slug>.md)` and parse YAML for every relation. A typical
entity page with 10 relations costs 10 reads per upsert; a Dream
pass touching 50 entities costs ~500 reads. On modern SSD that is
~5ms total — not catastrophic. The actual cost is implementation
surface: a per-compose cache keyed by URI (otherwise the same
target is read N times per page), invalidation when the target's
name changes, and the rebuild that ships the change.

**What would make us ship it.** A single failure mode, made
concrete so the next audit pass can check it without re-litigating:

> Phase 8 LoCoMo bench run reports **≥ 2 percentage points lower
> recall** on questions whose gold answer hinges on a relation
> target's full name (e.g. "Who is Marcelo's spouse?") **AND** the
> failure trace shows the target's `name` would have entered the
> top-K had it been in the entity page's embedding text.

The "AND" matters: it is not enough that LoCoMo regresses; the
regression has to be diagnosable as "missing name token in the
relation render". If the regression is something else (FTS
tokenisation, sectioning), shipping resolution does not fix
it.

**What would confirm we keep the slug-only behaviour permanently.**
If Phase 8 runs and the recall on relation-target questions is at
or above target, slug-only is empirically validated and the next
audit can mark the open question closed (move from "deferred with
trigger" to "decided against, evidence in Phase 8 results").

**Estimated implementation cost when triggered**: ~40 LOC in
`VectorIndex._render_frontmatter` (per-compose URI cache + lazy
`EntityPage.from_file` lookup; fall back to slug on any failure
because telemetry must never break a Dream apply), plus a TDD
surface (~3 cases: resolves when target exists, falls back to slug
when target missing, cache hits on repeated targets), plus a forced
schema bump + rebuild so existing centroids re-embed with the new
shape. Total ~80 LOC + reindex.

**Status**: deferred with Phase 8 trigger, not discarded — there
IS an observable failure mode (LoCoMo relation-target recall
metric) on the dated roadmap (Phase 8 in doc 09 §11). G5 tightened
the trigger from "below target" to the concrete condition above so
future audits can check the trigger without re-running the cost
analysis.

**`summary` slot — decided against** (audit G6, 2026-05-28, closing the E9 defer): the original v2 spec inserted a Dream-generated `summary` between rendered_frontmatter and body, intended to replace the truncated body in the centroid when body exceeded the 1500-char budget. E9 deferred the slot with the wording "if bench shows recall regression on long-body entity pages, restore the slot." Investigation under G6 (triggered by drill bugs) revealed three things that change the analysis:

1. **The data model does not support the slot.** `EntityPage` has no `summary` field; Dream produces zero summaries for entity pages today. The "defer" was built on a factual error ("Dream produces it sometimes" — no, it never does).

2. **Three retrieval paths reach the page; only the vector path is bounded by the 1500-char cap.** FTS5 indexes the full composed text (no truncation, doc 02 §5.2), and the grep fallback reads the file from disk. A query whose match is at char 7000 of a 10000-char body is found by lexical and grep; the canonical surfaces in the result set; the agent receives the URI.

3. **G6 fixed drill for entity-page URIs** (`memory/entity_page/<type>:<slug>` now resolves to the on-disk file). The agent that gets a canonical hit with a truncated snippet can drill to the full body. The body-recovery loop is closed.

With three retrieval paths reaching the page and drill closing the body-recovery loop, the summary slot would only help the vector path's specific corner case where the embedding model could not retrieve via name + aliases + rendered_frontmatter. Shipping it would require adding a `summary` field to `EntityPage`, modifying the Dream prompt and apply to emit it, modifying this composer to substitute body for summary when present, plus a schema bump and reindex. The marginal vector-only benefit does not justify that work, and there is no failure mode whose occurrence would empirically produce a request for it (same shape as G4). Status: **decided against**, not deferred. See design_rationale.md for the discarded entry.

### 4.3 Entries (episodic / stable / corpus)

**Shipped (v1, audit E9 2026-05-28):** `headline + summary + entities_list + body`.

The originally-planned v2 (`entities_with_aliases` — expanding entity URIs to include aliases inline in the embedding text) is **superseded by the entity-aware ranker (audit A1)**. The ranker extracts entities from the query at search time and boosts entries tagged with that URI, achieving alias-aware retrieval without inflating the embedding centroid. Implementing entities_with_aliases on top would duplicate the ranker's work without measurable benefit; see audit E9 in the 2026-05 audit reconciliation (historical) for the analysis.

The change in v2: when listing entities, expand each URI to include the entity's known aliases. Concretely, an episodic entry tagged with `entities: ["person:marcelo"]` composes:

```
<headline>

<summary>

Entities: person:marcelo (also: Marcelo Marmol, 马塞洛)

<body>
```

The alias expansion uses the workspace's alias index (read-only at compose time). If the alias index doesn't have entries for some URIs, just emit the URI alone.

### 4.4 Session summaries

One row per session in the vector index. The composed text is just the `_last_summary.text`, with no truncation beyond the 1500 char budget. The session title (if present in metadata) is set as `headline`.

If a session has no `_last_summary` yet (too short for compaction to have run), it does **not** appear in the vector index. Grep over the raw `.jsonl` is the only retrieval path for those.

---

## 5. Lexical index (FTS5 / BM25)

### 5.1 Schema — dual FTS5 (Hermes-style)

A single SQLite database at `.durin/index/fts.sqlite` with **two parallel FTS5 virtual tables**: one with the default `unicode61` tokenizer for Latin/Cyrillic/Greek/Arabic and most whitespace-separated scripts, and one with the `trigram` tokenizer for CJK and substring search.

```sql
-- Default tokenizer table (Latin and similar)
CREATE VIRTUAL TABLE memory_fts USING fts5(
    uri UNINDEXED,           -- primary identifier
    path UNINDEXED,
    type UNINDEXED,
    entity_type UNINDEXED,
    text,                    -- indexed: composed BM25 text (§5.2)
    tokenize = 'unicode61 remove_diacritics 2'
);

-- Trigram tokenizer table (CJK, substring queries)
CREATE VIRTUAL TABLE memory_fts_trigram USING fts5(
    uri UNINDEXED,
    path UNINDEXED,
    type UNINDEXED,
    entity_type UNINDEXED,
    text,
    tokenize = 'trigram'
);

CREATE TABLE fts_meta (
    uri TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    indexed_at TEXT NOT NULL
);
```

`UNINDEXED` columns are stored but not tokenized — they're available in result rows but not searched over.

**Naming asymmetry with LanceDB** (see §3.1). This index uses `uri` and `type` as column names; the LanceDB row schema uses `id` and `class_name` for the same logical fields. The two indices were built at different points in time and the names were never reconciled. Consumers (the search pipeline in `search_pipeline._resolve_meta`) translate at the boundary: a vector hit's `id` becomes the FTS-style `uri` for cross-index joins, and `class_name="entity_page"` is normalised to `type="entity"` in the meta dict. The asymmetry is benign once you know about it; renaming either side would mean a one-time rebuild with no functional benefit.

**Body indexed, body not stored**: similar to LanceDB, FTS5 indexes the composed `text` (headline + summary + entities + body) but never returns the `text` column in query results — queries return only the UNINDEXED metadata, and the consumer reads the body from disk if it needs the full content. This mirrors §3.1's decision: indices are for retrieval, the `.md` is the source of truth.

**Why dual:**
- `unicode61` tokenizes by whitespace/punctuation and word boundaries. Excellent for Latin and similar scripts. Splits CJK into single-character tokens, which works but is suboptimal for substring/phrase queries.
- `trigram` generates overlapping 3-character sequences. Excellent for CJK (3-char windows approximate "words" in CJK) and for substring search ("marc" matches "marcelo"). Less efficient per-byte for Latin queries where word boundaries are clear.
- Maintaining both lets the search pipeline route each query to the tokenizer that produces meaningful matches, with no operator configuration.

Storage cost (Hermes-style): ~4-6x the raw indexed text in total FTS5 size. For a medium workspace (~10k entries) this is typically 40-200 MB — trivial on modern disks.

Every write goes to **both** tables. Concretely, the indexer issues paired inserts/deletes on each `.md` change. SQL triggers or application-level coordination both work; the indexer uses application-level inserts for clarity and easier diagnostics.

### 5.2 BM25 text composition

The `text` column indexed by FTS5 is composed similarly to the embedding text but with **different priorities and no truncation**:

| For | Composed text |
|---|---|
| Entity pages | `name` + `aliases` + `rendered_frontmatter` + `body` (full) |
| Entries | `headline` + `summary` + `entities_list` + `body` (full) |
| Session summaries | `_last_summary.text` (full) |

Two key differences from §4 embedding text:

1. **No 1500-char truncation.** BM25 benefits from full content — it scores by term frequency and document length, both of which use the whole document.
2. **No alias expansion in entries** in v2 (aliases are still in the entity page's text, so a search for "Marcelo Marmol" still hits Marcelo's entity).

### 5.3 Tokenizers — what each does

| Tokenizer | Strengths | Weaknesses | Best for |
|---|---|---|---|
| **`unicode61 remove_diacritics 2`** | Word-level tokens for whitespace-separated scripts; cheap; canonical | CJK becomes single-char tokens (suboptimal substring/phrase) | Latin, Cyrillic, Greek, Arabic — most "normal" prose |
| **`trigram`** | Excellent CJK (3-char windows approximate words); native substring matching | ~3-5x storage and indexing cost; less semantic for Latin | CJK, mixed-script queries, substring lookups |

`remove_diacritics 2` strips accents in `unicode61` so accented variants match their plain forms.

Both tokenizers are fixed at table creation. Neither is configurable per-document or per-language. The search pipeline (see §6.1 of `03_search_pipeline.md`, pending) routes each query to one of the two tables based on script detection, transparent to the agent.

### 5.4 Routing principle (consumed by `03_search_pipeline.md`)

Although query-time routing is specified in detail in the search pipeline doc, the indexer needs to be aware of it so both tables receive correct content. The routing logic the search pipeline uses (verified pattern from Hermes-agent `hermes_state.py:2197-2280`):

1. Count CJK chars in the raw query.
2. If `cjk_count >= 3` AND every non-operator token has >= 3 chars, query `memory_fts_trigram`.
3. Else if `cjk_count > 0` but the query has short CJK tokens (< 3 chars), fallback to `LIKE` substring scan over the indexed text column (trigram cannot match tokens shorter than 3 chars).
4. Otherwise (no CJK), query `memory_fts` (the default `unicode61` path).

The indexer's responsibility: keep both tables in sync. The pipeline's responsibility: route correctly.

---

## 6. Indexer responsibilities

The indexer is a stateful component with the following responsibilities, all of which run on the cold path:

### 6.1 Triggers

| Trigger | Action |
|---|---|
| Tool writes a new `.md` (e.g., `memory_store`, `memory_ingest`, Dream apply) | Re-embed + re-FTS that file synchronously before returning to the caller |
| User edits a `.md` manually | File watcher detects mtime change; indexer re-derives that row; indexer commits the change to `memory/.git/` with `author: user` |
| Dream moves a file to archive | Remove the corresponding row from both indices |
| Dream merges entities (absorb-judge) | Move absorbed entity to `archive/`; remove its row; update aliases on the canonical entity |
| `durin memory reindex` command | Wipe `.durin/index/`, walk `memory/` (excluding archive), re-derive every row |

### 6.2 Re-embed-on-write — synchronous, not queued

When a tool writes a `.md` and expects the change to be searchable, the indexer call is **synchronous**: the tool waits for the embedding and the LanceDB/FTS5 row writes before returning.

Reasoning:
- Asynchronous queueing introduces races (a search right after `memory_store` may not see the new entry).
- The e5-small embed for a single document is fast (~3-10ms on modern hardware).
- FTS5 insertion is fast (~1ms).
- Combined latency is well within tool-response budgets.

**Exception:** during `durin memory reindex` (bulk rebuild), embedding is batched (32 documents per batch by default) for throughput. The bulk path is separate from the per-write path.

### 6.3 File watcher

Tech: `watchdog` (Python lib, polling fallback if inotify/FSEvents unavailable).

The watcher monitors `memory/` (excluding `archive/`) for `mtime` changes on `.md` files. When detected:

1. Lock the file (advisory).
2. Re-parse the markdown.
3. If parse succeeds: compute new embedding + FTS text; update LanceDB row + FTS5 row.
4. If the change came from a process other than Dream (i.e., user manual edit), commit the change to `memory/.git/` with `author: user`.
5. Release the lock.

**Coordination with Dream apply (no new lock added):** durin already has `memory/.dream.lock` (file-based) that serializes Dream runs between processes. The indexer does **not** introduce a new workspace-wide lock. Coordination relies on idempotent writes + mtime comparison:

- Dream apply writes the `.md`, then immediately re-indexes that file (synchronous, §6.2).
- The file watcher detects the mtime change and re-derives the same file. By the time it runs, the indexer's `indexed_at` for that uri already equals or exceeds the file mtime, so the watcher skips.
- If they do race (watcher fires before Dream's index update completes), both write the same content — worst case is one wasted embedding, never corruption.
- LanceDB and FTS5 each serialize concurrent writes internally; no external lock needed at the storage layer.

**Bursts:** if many files change in a short window (e.g., bulk import), the watcher coalesces events. Documents are re-indexed in batches.

**Lifecycle (audit A11, 2026-05-28).** The watcher is started by `AgentLoop.__init__` when `cfg.memory.file_watcher.enabled` is true — **default ON**. Failure to start (e.g. watchdog import error, filesystem permissions) is isolated: a warning logs, the loop keeps running. `memory_ingest` still indexes references at ingest time, but entity-page writes (`memory_writer`) rely on the watcher for re-indexing (FTS + vector, N2) — so a failed watcher means new/edited entities aren't searchable until `durin memory reindex`. `AgentLoop.stop()` drains it cleanly so the daemon thread + Observer terminate before the process exits.

Disable via `[memory.file_watcher] enabled = false` if the workspace lives on a filesystem that doesn't play well with `watchdog`, or if the user just wants one fewer daemon thread.

### 6.4 Auto-commit of user manual edits

When the watcher attributes a change to a non-Dream process, it commits the `.md` change to `memory/.git/`:

```
[commit] author: user <user@local>
message: "manual edit: <relative_path>"
```

Reasoning:
- Preserves version history for git-based audit (§10b of doc 01).
- Distinguishes user edits from Dream edits in the log (different `author`).
- The commit is local-only; no remote pushing.

If `memory/.git/` is not initialized (e.g., fresh workspace), the indexer initializes it on first write.

### 6.5 Archive exclusion enforcement

The walker `walk_memory(workspace, include_archive=False)` is the **single chokepoint** for "what files does the indexer touch". It:

- Skips `memory/archive/**`.
- Skips `memory/pending/**`.
- Yields all other `.md` files under `memory/` (recursively, sorted by path).

Audit E20 (2026-05-28) removed a stale fourth bullet that said the walker "also yields `sessions/<id>/<id>.meta.json` if a `_last_summary` is present". That contract was the pre-A10 path. Since A10 (2026-05-28) the session summary lives on disk as a real markdown file at `memory/session_summary/<sanitized_key>.md` (see [`session_summary_store.py`](../../../durin/memory/session_summary_store.py)), so the walker treats it like any other class — there is no special peek into `sessions/<id>/meta.json` anymore.

The indexer always uses this walker. Any future scanner that needs to enumerate workspace markdown must use this walker too, or explicitly justify why.

---

## 7. Rebuild and recovery

### 7.1 `durin memory reindex` command

Wipes `.durin/index/` and rebuilds from scratch by walking the workspace.

```
\$ durin memory reindex
Walking memory/ ... 1,243 files found
Embedding (batch 32) ... [progress bar]
Writing LanceDB ... done (1,243 rows)
Writing FTS5 ... done (1,243 rows)
Updating meta.json ... done
Rebuild complete in 47s.
```

This is the **operational guarantee** that markdown is the source of truth. If the index is corrupt or stale, the operator runs `durin memory reindex` and the system recovers.

### 7.2 Schema version mismatch

`meta.json` includes `schema_version` and `embedding_model_id`. On startup, the indexer checks:

| Mismatch | Behavior |
|---|---|
| Embedding model in `meta.json` differs from current code | Refuse to operate. Log: "embedding model changed from X to Y; run `durin memory reindex`." |
| Schema version older than current code expects | Same: refuse + log to rebuild. |
| `meta.json` missing | Treat as fresh install; run an automatic rebuild on first use. |

This prevents silent operation against a partially-incompatible index.

### 7.2.1 Planned migration procedure (when changing embedding model intentionally)

When the operator deliberately switches embedding model (e.g., the current MiniLM is deprecated on HF, a stronger model becomes available, or quantization needs change), the procedure:

1. **Backup** current `meta.json` and (optionally) `~/.durin/workspace/memory/.git/` push to a remote.
2. Update code/config to reference the new model identifier (e.g., bump `EMBEDDING_MODEL = "<new-model-id>"`).
3. Run `durin memory reindex` with the new model already configured (the command auto-detects the model from `cfg.memory.embedding.model_id`). Audit E29 (2026-05-28) removed a `durin embed-migrate` reference — that command was a proposed name in an earlier draft; it never shipped. The migration workflow is: set the new model in config, run `durin memory reindex`. The command:
   - Writes the new identifier into a fresh `meta.json` after backing up the old.
   - Wipes `.durin/index/lance/` (vector index — the new model produces different-dim vectors typically).
   - **Preserves FTS5** unless the new model requires a tokenizer change (FTS5 is tokenizer-driven, not embedding-driven).
   - Walks `memory/` and re-embeds + writes new LanceDB rows.
   - Bumps `schema_version` and records previous model in `meta.json::previous_models` (audit trail).
4. Smoke test: a representative query returns sensible results post-migration.

If the new model has different vector dimensions (almost always the case), step 3's wipe is mandatory — LanceDB can't mix dims in one table.

If something goes wrong mid-migration, the operator restores the backed-up `meta.json` and `memory/.git/` HEAD, and the system reverts to the old model.

### 7.3 Partial corruption

If a single LanceDB or FTS5 row is corrupt or missing for a known `.md`:

- The indexer detects on read (during search) via mtime comparison.
- On detection, that single row is re-derived synchronously.
- This makes partial corruption self-healing without requiring a full rebuild.

### 7.4 Disk-full / write failure

When a write to LanceDB or FTS5 fails:
- The tool that triggered the write receives an error.
- The `.md` write to disk is preserved (it succeeded first, before the index attempt).
- The system is in a partial-coherence state: `.md` is newer than the index. On the next search, the staleness detection picks up the gap and re-derives if possible.
- If repeated failures, the operator must intervene (e.g., free disk, run reindex).

---

## 8. Staleness handling

The system tolerates brief windows where `.md` and indices are not in sync (e.g., between a write and the next index update). Staleness is detected by:

- `fts_meta` per-uri `(mtime, indexed_at)` rows in `fts.sqlite` (§5.1) compared against the `.md` file mtime on disk. Audit E30 (2026-05-28) corrected an earlier reference to "`meta.json::indexed_at` per row" — `.durin/index/meta.json` is workspace-level state (`schema_version`, `embedding_model_id`, `last_full_rebuild`), not a per-row store; the per-row staleness data lives in the SQLite `fts_meta` table managed by `fts_index.py`.
- When a search returns a hit whose `path` no longer exists on disk: the row is removed lazily.
- When the file watcher detects a change but the indexer write fails: the file is re-queued.

**`row_for_missing_file` self-heal (2026-06-04).** `detect_index_staleness` reports an indexed uri whose backing `.md` is gone (a raw `rm`, an external sync, a crash mid-write — the file watcher can't observe deletions). The health-check now *prunes* these orphans in one batched, model-free pass (`HealthChecker._prune_orphans` → `FTSIndex.delete_by_uris` + `vector_index.delete_ids`) instead of leaving them until a manual reindex. Pruning needs only the uri, never the embedding model, so reconciling a bulk deletion stays cheap.

**Lance↔disk reconcile (2026-06-06).** The `row_for_missing_file` pass above is *FTS-driven*: it walks `fts_meta` and can only prune orphans that still have an FTS row. A row that exists **only in the Lance table** — never in `fts_meta` — is invisible to it. That state arises from an FTS-only rebuild, a reinstall, or a partial cleanup that wiped `memory/<class>/` *and* the FTS rows but left the vector table untouched (the real trigger was 203 `helpjuice` corpus chunks stranded in Lance, surfacing in the webui "Entradas" tab and 404ing on click). The health-check now also runs `vector_index.prune_orphan_rows(workspace)` every tick: it projects `id` + `path` from the Lance table (model-free, no vector load), drops every row whose `path` no longer resolves to a file on disk, and reports the count via `lance_orphans_pruned` in the `memory.health_check` payload. Rows with an empty `path` are left alone (can't verify → don't delete).

**Symmetric watcher delete (2026-06-06).** `reindex_one_file`'s file-vanished branch previously deleted only the FTS row, stranding the Lance row as a search-able orphan. It now also calls `vector_index.delete_ids` (via the shared `vector_id_for_uri` mapping) so a deletion observed by the watcher reconciles both indices at once. The periodic reconcile above remains the backstop for deletions the watcher never sees.

> **`_uri_for` entry-uri fix (2026-06-04).** `_uri_for` derived the bare filename stem for memory entries while `_payload_for` indexes — and `fts_meta` stores — the full `memory/<class>/<id>` form. The mismatch double-flagged *every present entry* as `row_for_missing_file` + `missing_row` (re-indexing the whole vault each tick) and left `forget` / drift-repair unable to delete an entry's FTS row. `_uri_for` now returns the canonical `memory/<class>/<id>`.

**Search-time policy:** if a hit's `.md` is unreadable or whose `mtime` exceeds the indexed `mtime` by more than 60 seconds (heuristic), the hit is filtered out of results and a re-index of that file is scheduled.

---

## 9. Operational constraints

| Constraint | Reason |
|---|---|
| LanceDB and FTS5 live on local disk | Workspace portability; no network dependency for hot path. |
| Indices are not committed to git | `.durin/index/` in `.gitignore`. Reconstructible from `memory/`. |
| Single-writer to indices at a time | Workspace lock held during writes; readers don't take the lock. |
| No background re-indexing on idle | Rebuilds are explicit operator actions or triggered by writes. Avoids surprise CPU usage. |
| Embedding model loaded lazily | First search may have a cold-start cost (~1-2s to load e5-small); subsequent are fast. |

---

## 10. Module-level decisions

All open decisions for this module have been resolved (2026-05-27) in line with the architectural choices from the cross-corpus decisions in `00_overview.md`.

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| **1** | What gets indexed | Entity pages + entries (episodic/stable/corpus) + session summaries + raw session turns (FTS-only, per-turn, schema v6 — §3.3.2). NOT indexed: archive, pending, raw session `.jsonl` streams, raw ingested files. | §3.3 |
| **2** | Single vs multiple embedding models | **Single model per workspace** (default `intfloat/multilingual-e5-small` since 2026-05-30). Stored in `meta.json`; mismatch on startup forces rebuild. | §3.2, §7.2 |
| **3** | Body in the vector row | **Not stored.** Body is read from disk on demand for cold-tier enrichment. Storing in LanceDB doubles index size for no retrieval benefit. | §3.1 |
| **4** | Embedding text composition (entity pages) | **Shipped (v2.a, audit E9 2026-05-28):** `name` + `aliases` + `rendered_frontmatter` + `body`, hard cap 1500 chars. Frontmatter renders as prose; provenance + internal timestamps skipped; stateful attributes render `current` only. Optional `summary` slot **decided against** (audit G6, 2026-05-28; see design_rationale.md). | §4.2 |
| **5** | Embedding text composition (entries) | **Shipped v1; v2.b superseded (audit E9 2026-05-28):** `headline` + `summary` + `entities_list` + `body`. The originally-planned `entities_with_aliases` expansion is covered at query time by the entity-aware ranker (audit A1) — implementing it in the embedding text would duplicate the ranker's work without measurable benefit. | §4.3 |
| **6** | Sessions in the vector index | One row per session as `type=session_summary` using `_last_summary.text` as content. Sessions without a summary yet are not in the vector index (grep over raw `.jsonl` covers them). | §3.3, §4.4 |
| **7** | Re-embed sync vs async | **Synchronous on write** for single-document updates. Bulk rebuild path uses async batching (32 docs/batch). | §6.2 |
| **8** | File watcher technology | `watchdog` (Python) with polling fallback. Coalesces bursts. | §6.3 |
| **9** | Auto-commit of user manual edits | Indexer commits user edits to `memory/.git/` with `author: user`. Local only; no remote push. | §6.4 |
| **10** | Race between Dream apply and watcher | **No new lock added.** Existing `memory/.dream.lock` (file-based) already serializes Dream runs. Coordination between Dream's index update and the watcher's index update relies on idempotent writes + `indexed_at` vs `mtime` comparison. Worst case is a wasted embedding, never corruption. LanceDB/FTS5 each serialize internal writes. | §6.3 |
| **11** | FTS5 tokenizers — single vs dual | **Dual FTS5 tables (Hermes-style):** `memory_fts` with `unicode61 remove_diacritics 2` for Latin/Cyrillic/Greek/etc., and `memory_fts_trigram` with `trigram` for CJK + substring queries. Both tables receive every write. Query pipeline routes by CJK detection (verified pattern from `hermes-agent/hermes_state.py:2197-2280`). Storage cost ~4-6x raw indexed text (40-200 MB for a 10k-entry workspace), accepted because it removes operator burden and makes CJK out-of-the-box. | §5.1, §5.3, §5.4 |
| **12** | BM25 text truncation | **None.** Full document is indexed. BM25 needs term frequencies and doc length. | §5.2 |
| **13** | Embedding model mismatch on startup | System refuses to operate; logs the mismatch; requires `durin memory reindex`. Prevents silent inconsistency. | §7.2 |
| **14** | Staleness detection | Per-row `indexed_at` vs file `mtime`. 60-second tolerance; beyond that, row is re-derived on read or filtered from results. | §8 |

### Open

None at the module level. Cross-references to other modules:
- How these indices are queried: `03_search_pipeline.md`.
- How Dream interacts with re-index during apply: `05_dream_cold_path.md`.
- How `durin memory reindex` and `durin archive ...` commands surface: `04_agent_tools.md` (CLI section, pending).

---

## 11. Implementation status (current — audit C7, 2026-05-28)

Rebuilt from scratch — the original v1 table described a "current state" that was already obsolete when written. The full v2 set has shipped.

| Aspect | Status | Where |
|---|---|---|
| Vector index (LanceDB) | ✅ Active. `intfloat/multilingual-e5-small` (384-dim default since 2026-05-30; was MiniLM-L12-v2). 8-column schema per §3.1 (no `body` column — see A4). | `durin/memory/vector_index.py` |
| Embedding text — entities | ✅ v2.a shipped (audit E9, 2026-05-28). `name + aliases + rendered_frontmatter + body`, 1500-char cap (`_compose_entity_page_text`). Attribute queries ("Marcelo's email", "X's spouse") now hit the centroid. Schema bumped to v4; rebuild forced. | `durin/memory/vector_index.py:_compose_entity_page_text` |
| Embedding text — entries | ✅ v1 (final shape). `headline + summary + entities_list + body`, 1500-char cap (`_embed_text`). Originally-planned `entities_with_aliases` expansion superseded by entity-aware ranker (A1); decision recorded in audit E9. | `durin/memory/vector_index.py:_embed_text` |
| Vector rebuild walks entity pages | ✅ Shipped (audit E9, 2026-05-28). `rebuild_from_workspace` now walks `memory/entities/<type>/*.md` in addition to `memory/<class>/*.md`, so a forced rebuild (e.g. schema bump) doesn't silently drop entity page rows. | `durin/memory/vector_index.py:rebuild_from_workspace` |
| Session summaries indexed | ✅ A10 (2026-05-28). `Consolidator._persist_last_summary` writes `memory/session_summary/<sanitized_key>.md`; walker picks it up; indexer assigns `class_name="session_summary"`. | `durin/memory/session_summary_store.py` |
| FTS5 lexical index | ✅ Active. `.durin/index/fts.sqlite` with two FTS5 tables (`memory_fts` unicode61 + `memory_fts_trigram`); paired writes; query-time routing in `query_router.py`. | `durin/memory/fts_index.py`, `durin/memory/query_router.py` |
| File watcher | ✅ Active. `watchdog`-backed `MemoryFileWatcher` started by `AgentLoop.__init__` when `cfg.memory.file_watcher.enabled` (default true). A11 (2026-05-28). | `durin/memory/file_watcher.py` |
| Health-check cron | ✅ Active. `HealthCheckScheduler` daemon thread driving `HealthChecker.run_tick()` every 900s by default. A11. | `durin/memory/health_check.py` |
| Archive exclusion | ✅ `walk_memory` excludes `memory/archive/**` (and `memory/pending/**`). Single chokepoint per §6.5. | `durin/memory/paths.py:walk_memory` |
| `durin memory reindex` command | ✅ Active CLI: `durin memory reindex --target {fts,lancedb,all}`. | `durin/cli/memory_cmd.py:cmd_reindex` |
| Schema version + auto-rebuild | ✅ `index_meta.py::CURRENT_SCHEMA_VERSION` (4 as of audit E9 / F13 verification, 2026-05-28; bumped from 3 when entity-page composition gained `rendered_frontmatter`). `ensure_index_fresh` triggers clean rebuild on mismatch. | `durin/memory/index_meta.py` |

---

## 12. Cross-references

- Storage layout and data classes: `01_data_and_entities.md` §1, §2.
- Archive exclusion rule: `01_data_and_entities.md` §3.6.
- Versioning via git history (which the indexer commits to): `01_data_and_entities.md` §10b.
- Search-time consumption of these indices: `03_search_pipeline.md` (pending).
- Dream's write path triggering re-index: `05_dream_cold_path.md` (pending).
