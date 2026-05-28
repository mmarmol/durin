---
title: Indexing — LanceDB vector index and FTS5 lexical index
version: 0.1-draft
status: under construction
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 01_data_and_entities.md
related: 03_search_pipeline.md, 05_dream_cold_path.md
---

# Indexing

This document specifies the two derived indices that accelerate retrieval over the markdown source of truth: a **vector index** (LanceDB + MiniLM) for semantic retrieval, and a **lexical index** (SQLite FTS5 with BM25) for keyword and phrase retrieval. Both are derived from `.md` files and reconstructible at any time.

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
| `summary` | string | For entries: frontmatter `summary` (may be empty). For entity pages: `name (also: alias1, alias2)` derived at upsert time. |
| `headline` | string | For entries: frontmatter `headline`. For entity pages: the entity `name`. |
| `vector` | fixed-size list of floats | Dim depends on the configured model — default `paraphrase-multilingual-MiniLM-L12-v2` → **384**; CJK-heavy users on `multilingual-e5-large` → 1024. Validated at startup via `VectorIndex._guard_dim_match`. |
| `valid_from` | string | ISO date if the entry frontmatter carries one; empty string `""` when absent. |
| `entities` | list[string] | For entries: frontmatter `entities` field (e.g. `["person:marcelo", "project:durin"]`). For entity pages: `[]` (a page IS the entity; the ranker treats `class_name="entity_page"` specially). Used by `entity_ranker` for the post-cursor boost. |
| `path` | string | Relative path to the `.md` from workspace root. Used by consumers that need to read the body on demand. |

**No `body` column. The body lives on disk.** When a cold-tier caller (`memory_search(level="cold")`) needs the full body, the consumer reads the `.md` via `memory_search._enrich_body`. This is a deliberate architectural choice: the `.md` is the single source of truth; LanceDB is a derivable, disposable cache that can be rebuilt from disk at any time. P2.5 (commit `a266344`) briefly stored the body here as a latency micro-optimisation; audit A4 reverted it because:

1. The latency saved (~5-10 ms for 10 file opens on SSD) was not bottleneck — the LLM call downstream takes seconds.
2. Storing the body duplicated content and opened a drift window between disk edits (e.g., via the file watcher path of P2.3) and LanceDB reads.
3. Doubling the index size compounded at scale.
4. FTS5 already honors the "indexed for search, content lives on disk" model — LanceDB now matches.

See `docs/memory/08_scope_and_discarded.md` §2.10 for the full rationale and the lesson on "premature optimisation that violates an architectural principle".

### 3.2 Embedding model

Single global choice per workspace (configurable via `memory.embedding.model`):

| Property | Default value |
|---|---|
| Model | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| Dim | **384** (varies by model — see below) |
| Size | 220 MB on disk |
| Max sequence | 512 tokens (~1500 chars) |
| Multilingual | Yes (EN/ES/ZH/JA/KO and ~50 others) |

Recommended overrides:
- CJK-heavy workloads: `intfloat/multilingual-e5-large` (2.24 GB, 1024-dim).
- English-only minimal: `sentence-transformers/all-MiniLM-L6-v2` (90 MB, 384-dim).

The model is **not configurable per-row**. All vectors in the index share this model. Changing the model requires a full rebuild (`durin memory reindex`). The model identifier is stored in `<workspace>/.durin/index/meta.json`; `VectorIndex._guard_dim_match` checks the on-disk table's vector dim against the provider's reported dim at every read/write and raises `VectorIndexDimensionMismatch` if they differ, so the search pipeline can fall back to lexical instead of mixing 384-dim and 1024-dim rows.

**Provider abstraction.** `durin/memory/embedding.py` defines an `EmbeddingProvider` ABC plus a concrete `FastembedProvider` (uses ONNX in-process, no GPU required). The abstraction is so adding a new provider (e.g., for a different model family, a remote inference endpoint, or quantized variants) is a one-class addition without touching the rest of the indexer. The provider validates model identifier at construction and exposes telemetry hooks for load + per-embed timings.

### 3.3 What is indexed vs not

| Indexed (rows present) | Not indexed |
|---|---|
| `memory/entities/<type>/<slug>.md` (class_name = `entity_page`) | `memory/archive/**` (decision §3.6 of doc 01) |
| `memory/episodic/<id>.md` (post-cursor + pre-cursor both) | `memory/pending/<id>.md` (intake buffer; walker/indexer skip it) |
| `memory/stable/<id>.md` | `sessions/<id>/<id>.jsonl` (raw conversation transcripts) |
| `memory/corpus/<id>.md` | `ingested/<id>/source.*` (raw artifacts; only the derived `memory/corpus/<id>.md` chunks are indexed) |

> **Session summaries**: doc v1 promised one row per session at `class_name=session_summary` derived from `sessions/<id>/<id>.meta.json::_last_summary`. **No code emits these rows today** — the walker only iterates `.md` files under `memory/`. Tracked as audit item A10 (`docs/memory/11_audit_reconciliation.md`).

The shared workspace walker (`walk_memory(workspace, include_archive=False)`) is the single chokepoint. Indexer, ranker, alias bootstrapper, and any future scanner all consume its output.

---

## 4. Embedding text composition

The text passed to the embedding model determines what the vector represents. This section specifies exactly what gets composed for each indexable `.md`. **Single source of truth: `vector_index.py::compose_embedding_text(...)`**.

### 4.1 Common rules

- Hard char budget: **1500 chars**. Anything beyond is truncated.
- Composition order: most-distilled signal first, longest-and-truncatable last.
- Joiner: `\n\n` between sections.
- Whitespace inside individual sections preserved (CJK-safe).

### 4.2 Entity pages

**v1 (current):** `name + aliases + body`.
**v2 (target):** `name + aliases + rendered_frontmatter + summary + body`, in that order, until budget exhausted.

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
| `relations[i] = { to: <uri>, type: <t>, since: <date>, ... }` | `<type.title()>: <to_name_resolved> (since <date>).` `to_name_resolved` is the `name` of the target entity if known, else the URI. |
| `provenance` | **Not rendered.** Internal metadata, no retrieval value. |
| `dream_processed_through`, `created_at`, `updated_at` | **Not rendered.** Internal timestamps. |

**v2 summary**: when Dream produces a `summary` for an entity page (because body exceeds budget or because the body is prose-heavy), the summary replaces the body in the embedding text. Body retains its full form on disk for grep and for display.

### 4.3 Entries (episodic / stable / corpus)

**v1 (current):** `headline + summary + entities_list + body`.
**v2 (target):** `headline + summary + entities_with_aliases + body`.

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
| `durin reindex` command | Wipe `.durin/index/`, walk `memory/` (excluding archive), re-derive every row |

### 6.2 Re-embed-on-write — synchronous, not queued

When a tool writes a `.md` and expects the change to be searchable, the indexer call is **synchronous**: the tool waits for the embedding and the LanceDB/FTS5 row writes before returning.

Reasoning:
- Asynchronous queueing introduces races (a search right after `memory_store` may not see the new entry).
- The MiniLM embed for a single document is fast (~10-30ms on modern hardware).
- FTS5 insertion is fast (~1ms).
- Combined latency is well within tool-response budgets.

**Exception:** during `durin reindex` (bulk rebuild), embedding is batched (32 documents per batch by default) for throughput. The bulk path is separate from the per-write path.

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
- Yields all other `.md` files under `memory/`.
- Also yields `sessions/<id>/<id>.meta.json` if a `_last_summary` is present (one yield per session).

The indexer always uses this walker. Any future scanner that needs to enumerate workspace markdown must use this walker too, or explicitly justify why.

---

## 7. Rebuild and recovery

### 7.1 `durin reindex` command

Wipes `.durin/index/` and rebuilds from scratch by walking the workspace.

```
$ durin reindex
Walking memory/ ... 1,243 files found
Embedding (batch 32) ... [progress bar]
Writing LanceDB ... done (1,243 rows)
Writing FTS5 ... done (1,243 rows)
Updating meta.json ... done
Rebuild complete in 47s.
```

This is the **operational guarantee** that markdown is the source of truth. If the index is corrupt or stale, the operator runs `durin reindex` and the system recovers.

### 7.2 Schema version mismatch

`meta.json` includes `schema_version` and `embedding_model_id`. On startup, the indexer checks:

| Mismatch | Behavior |
|---|---|
| Embedding model in `meta.json` differs from current code | Refuse to operate. Log: "embedding model changed from X to Y; run `durin reindex`." |
| Schema version older than current code expects | Same: refuse + log to rebuild. |
| `meta.json` missing | Treat as fresh install; run an automatic rebuild on first use. |

This prevents silent operation against a partially-incompatible index.

### 7.2.1 Planned migration procedure (when changing embedding model intentionally)

When the operator deliberately switches embedding model (e.g., the current MiniLM is deprecated on HF, a stronger model becomes available, or quantization needs change), the procedure:

1. **Backup** current `meta.json` and (optionally) `~/.durin/workspace/memory/.git/` push to a remote.
2. Update code/config to reference the new model identifier (e.g., bump `EMBEDDING_MODEL = "<new-model-id>"`).
3. Run `durin embed-migrate --to <new-model-id>` (or `durin reindex` with model auto-detected from code). The command:
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

- `meta.json::indexed_at` per row vs `.md` file mtime.
- When a search returns a hit whose `path` no longer exists on disk: the row is removed lazily.
- When the file watcher detects a change but the indexer write fails: the file is re-queued.

**Search-time policy:** if a hit's `.md` is unreadable or whose `mtime` exceeds the indexed `mtime` by more than 60 seconds (heuristic), the hit is filtered out of results and a re-index of that file is scheduled.

---

## 9. Operational constraints

| Constraint | Reason |
|---|---|
| LanceDB and FTS5 live on local disk | Workspace portability; no network dependency for hot path. |
| Indices are not committed to git | `.durin/index/` in `.gitignore`. Reconstructible from `memory/`. |
| Single-writer to indices at a time | Workspace lock held during writes; readers don't take the lock. |
| No background re-indexing on idle | Rebuilds are explicit operator actions or triggered by writes. Avoids surprise CPU usage. |
| Embedding model loaded lazily | First search may have a cold-start cost (~1-2s to load MiniLM); subsequent are fast. |

---

## 10. Module-level decisions

All open decisions for this module have been resolved (2026-05-27) in line with the architectural choices from the cross-corpus decisions in `00_overview.md`.

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| **1** | What gets indexed | Entity pages + entries (episodic/stable/corpus) + session summaries. NOT indexed: archive, pending, raw sessions/jsonl, raw ingested files. | §3.3 |
| **2** | Single vs multiple embedding models | **Single model per workspace** (`paraphrase-multilingual-MiniLM-L12-v2`). Stored in `meta.json`; mismatch on startup forces rebuild. | §3.2, §7.2 |
| **3** | Body in the vector row | **Not stored.** Body is read from disk on demand for cold-tier enrichment. Storing in LanceDB doubles index size for no retrieval benefit. | §3.1 |
| **4** | Embedding text composition (entity pages) | `name` + `aliases` + `rendered_frontmatter` + `summary` + `body`, in order, hard cap 1500 chars. Frontmatter is rendered as prose; provenance and internal timestamps are not rendered. Historical values of stateful attributes are not rendered (only `current`). | §4.2 |
| **5** | Embedding text composition (entries) | `headline` + `summary` + `entities_with_aliases` + `body`. v2 expands entity URIs with their known aliases to improve recall on nickname queries. | §4.3 |
| **6** | Sessions in the vector index | One row per session as `type=session_summary` using `_last_summary.text` as content. Sessions without a summary yet are not in the vector index (grep over raw `.jsonl` covers them). | §3.3, §4.4 |
| **7** | Re-embed sync vs async | **Synchronous on write** for single-document updates. Bulk rebuild path uses async batching (32 docs/batch). | §6.2 |
| **8** | File watcher technology | `watchdog` (Python) with polling fallback. Coalesces bursts. | §6.3 |
| **9** | Auto-commit of user manual edits | Indexer commits user edits to `memory/.git/` with `author: user`. Local only; no remote push. | §6.4 |
| **10** | Race between Dream apply and watcher | **No new lock added.** Existing `memory/.dream.lock` (file-based) already serializes Dream runs. Coordination between Dream's index update and the watcher's index update relies on idempotent writes + `indexed_at` vs `mtime` comparison. Worst case is a wasted embedding, never corruption. LanceDB/FTS5 each serialize internal writes. | §6.3 |
| **11** | FTS5 tokenizers — single vs dual | **Dual FTS5 tables (Hermes-style):** `memory_fts` with `unicode61 remove_diacritics 2` for Latin/Cyrillic/Greek/etc., and `memory_fts_trigram` with `trigram` for CJK + substring queries. Both tables receive every write. Query pipeline routes by CJK detection (verified pattern from `hermes-agent/hermes_state.py:2197-2280`). Storage cost ~4-6x raw indexed text (40-200 MB for a 10k-entry workspace), accepted because it removes operator burden and makes CJK out-of-the-box. | §5.1, §5.3, §5.4 |
| **12** | BM25 text truncation | **None.** Full document is indexed. BM25 needs term frequencies and doc length. | §5.2 |
| **13** | Embedding model mismatch on startup | System refuses to operate; logs the mismatch; requires `durin reindex`. Prevents silent inconsistency. | §7.2 |
| **14** | Staleness detection | Per-row `indexed_at` vs file `mtime`. 60-second tolerance; beyond that, row is re-derived on read or filtered from results. | §8 |

### Open

None at the module level. Cross-references to other modules:
- How these indices are queried: `03_search_pipeline.md`.
- How Dream interacts with re-index during apply: `05_dream_cold_path.md`.
- How `durin reindex` and `durin archive ...` commands surface: `04_agent_tools.md` (CLI section, pending).

---

## 11. Implementation status (current vs target)

| Aspect | Current state | v2 target | Migration work |
|---|---|---|---|
| Vector index (LanceDB) | Active, single table, MiniLM-L12-v2 (384-dim default), 8-column schema per §3.1 | Same engine; if session summaries are emitted (see A10) they would index as `class_name=session_summary` | Optional: session_summary emitter (A10); other knobs all already shipped |
| Embedding text — entities | `name + aliases + body`, 1500 char cap | + `rendered_frontmatter` + `summary` | Update `compose_embedding_text` for entities |
| Embedding text — entries | `headline + summary + entities_list + body` | Same + `entities_with_aliases` | Update `compose_embedding_text` for entries; integrate alias index lookup |
| Session summaries indexed | No (sessions grep-only) | Yes (one row per session with `_last_summary`) | New emitter; trigger on `_last_summary` update |
| FTS5 lexical index | Does not exist | New `.durin/index/fts.sqlite` with **two FTS5 tables** (`unicode61` + `trigram`); paired writes; query-time routing by CJK detection | Build indexer with paired inserts; implement CJK detection helper used by the pipeline; integrate into write path; integrate into search pipeline |
| File watcher | Manual rebuild only | `watchdog` watcher with auto-commit | New module; integrate with workspace lock |
| Archive exclusion | Not yet relevant (no archive) | Walker excludes by default; chokepoint enforced | Implement walker + audit all scanners |
| `durin reindex` command | Manual `_build_vector_index` helper exists | First-class CLI command + bulk batching | Wrap and expose |

---

## 12. Cross-references

- Storage layout and data classes: `01_data_and_entities.md` §1, §2.
- Archive exclusion rule: `01_data_and_entities.md` §3.6.
- Versioning via git history (which the indexer commits to): `01_data_and_entities.md` §10b.
- Search-time consumption of these indices: `03_search_pipeline.md` (pending).
- Dream's write path triggering re-index: `05_dream_cold_path.md` (pending).
