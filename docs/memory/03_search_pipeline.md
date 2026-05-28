---
title: Search pipeline — hot path retrieval
version: 0.1-draft
status: under construction
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 01_data_and_entities.md, 02_indexing.md
related: 04_agent_tools.md
---

# Search pipeline

This document specifies the **hot path** that runs every time the agent calls `memory_search`. It transforms a raw query string into a ranked set of structured, sectioned results — without invoking any LLM. The pipeline composes vector retrieval (LanceDB), lexical retrieval (FTS5), weighted fusion, entity-aware reranking, cross-encoder reranking, MMR diversity, and (opt-in) temporal decay.

**Invariant:** no step in this document calls an LLM. The pipeline is fully deterministic per query + workspace state. LLMs only run on the cold path (Dream, ingestion).

---

## 1. Pipeline overview

```
       ┌─────────────────────────────────────────────────┐
       │  Query: (query: str, [keywords: str | null],    │
       │          scope: dreamed|undreamed|all,          │
       │          level: warm|cold)                      │
       └────────────────────┬────────────────────────────┘
                            │
                            ▼
       ┌─────────────────────────────────────────────────┐
       │  STEP 1 — Query analysis                         │
       │  - CJK detection                                 │
       │  - extract entity references from query          │
       │  - normalize whitespace                          │
       └────────────────────┬────────────────────────────┘
                            │
                ┌───────────┴────────────┐
                ▼                         ▼
       ┌────────────────┐       ┌────────────────────┐
       │ STEP 2a        │       │ STEP 2b            │
       │ Vector search  │       │ Lexical search     │
       │ (LanceDB)      │       │ (FTS5)             │
       │                │       │ - routes to one of │
       │ embed(query)   │       │   memory_fts,      │
       │ → top-50       │       │   memory_fts_      │
       │                │       │   trigram, or LIKE │
       │                │       │ → top-50           │
       └────────┬───────┘       └─────────┬──────────┘
                │                          │
                └────────────┬─────────────┘
                             │
                             ▼
       ┌─────────────────────────────────────────────────┐
       │  STEP 3 — Weighted merge                         │
       │  RRF (Reciprocal Rank Fusion) cross-RRF between  │
       │  vector_results and lexical_results              │
       │  → unified ranked list (top-50)                  │
       └────────────────────┬────────────────────────────┘
                            │
                            ▼
       ┌─────────────────────────────────────────────────┐
       │  STEP 4 — Entity-aware rerank (RRF boost)        │
       │  Hits whose `entities` column matches the query  │
       │  entities get rank boost. Pre/post-cursor logic. │
       └────────────────────┬────────────────────────────┘
                            │
                            ▼
       ┌─────────────────────────────────────────────────┐
       │  STEP 5 — Cross-encoder rerank                   │
       │  top-50 → cross-encoder model (no LLM, 80M       │
       │  params) → top-10 by full-relevance score        │
       └────────────────────┬────────────────────────────┘
                            │
                            ▼
       ┌─────────────────────────────────────────────────┐
       │  STEP 6 — Temporal decay (opt-in)                │
       │  Multiply each score by exp(-Δt / half_life)     │
       │  Disabled by default; configurable per workspace │
       └────────────────────┬────────────────────────────┘
                            │
                            ▼
       ┌─────────────────────────────────────────────────┐
       │  STEP 7 — Sectioning + rendering                 │
       │  Group hits by class with structural markers:    │
       │  CANONICAL / FRAGMENT / SESSION / INGESTED       │
       │  Apply per-source cap (e.g., max N chunks per    │
       │  ingest_id) to prevent same-source duplication.  │
       └────────────────────┬────────────────────────────┘
                            │
                            ▼
       ┌─────────────────────────────────────────────────┐
       │  Result: list of sectioned, ranked results       │
       │  with provenance (path, uri, score, snippet)     │
       └─────────────────────────────────────────────────┘
```

Each step is detailed in §3 onwards.

---

## 2. Inputs and outputs

### 2.1 Inputs

These are the inputs to the `memory_search` **tool** surface
(`MemorySearchTool.execute`). Audit E10 (2026-05-28) clarified the
layer boundary: `query` + `keywords` are forwarded directly into
`run_search_pipeline`; `scope` + `level` + `limit` are orchestrated
at the tool layer around the pipeline call. See "Tool vs pipeline
boundary" below the table.

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | yes | The semantic + lexical query. Used for embedding AND for FTS5 search. |
| `keywords` | string \| null | no | Optional literal-match string. When provided, biases the lexical search; the vector search ignores it. |
| `scope` | enum `dreamed|undreamed|all|archive` | no (default `all`) | Limits which classes are searched. `dreamed` = entities + episodic + stable + corpus + session summaries; `undreamed` = raw session/ingested grep fallback; `all` = both; `archive` = on-demand walk of `memory/archive/**` for recovery / diagnostic queries (audit F2, 2026-05-28). Audit F14 (2026-05-28) also corrected the grep-fallback coverage note: the fallback walks `memory/` in addition to `sessions/` + `ingested/` so memory entries written outside the tool layer (tests, scripts) still surface. |
| `level` | enum `warm|cold` | no (default `warm`) | `warm` returns the headline+summary+metadata; `cold` enriches each hit with its body. |
| `limit` | int | no (default 10) | Max results returned after sectioning. |

**Tool vs pipeline boundary** (audit E10): the `run_search_pipeline`
function signature only takes `query`, `keywords`, `vector_index`,
`limit`, `cross_encoder`, `cross_encoder_top_n`,
`temporal_decay_enabled`. The other tool inputs are handled around
the call:

- `scope=undreamed` → the tool passes `vector_index=None` so the
  pipeline skips step 2a, and post-filters hits to keep only
  `session_summary` / `corpus` types.
- `scope=dreamed` → vector_index is passed; grep fallback in §6 of
  the pipeline still runs but the tool keeps its hits intact.
- `level=cold` → the tool enriches each `SectionedHit` with the
  body read from disk after the pipeline returns; pipeline rows
  carry only the snippet on either level.
- `limit` is clamped to `[1, 50]` at the tool and forwarded to the
  pipeline.

### 2.2 Output

A list of `Result` objects, sectioned and rendered with structural markers (see §10 in this doc). Each result carries `uri`, `path`, `type`, `score`, `snippet`, and optionally the full `body` (when `level=cold`).

---

## 3. Step 1 — Query analysis

Performed once per call, before any retrieval. Output of this step feeds the rest of the pipeline.

### 3.1 CJK detection

A helper `count_cjk_chars(query: str) -> int` counts characters in the CJK Unicode blocks (CJK Unified Ideographs, Hiragana, Katakana, Hangul). The pipeline uses this count to route to the right FTS5 table in step 2b.

The routing thresholds are the same as Hermes-agent's (verified):

| `cjk_count` | Token check | Lexical path |
|---|---|---|
| 0 | n/a | `memory_fts` (unicode61) |
| `>= 3` | All non-operator tokens have `>= 3` chars | `memory_fts_trigram` |
| `> 0` and `< 3`, OR has tokens with `< 3` CJK chars | n/a | `LIKE %query%` substring scan over the text column |

### 3.2 Entity extraction from query

The pipeline calls `extract_query_entities(query, alias_index)` (existing helper in `durin/memory/entity_ranker.py`). It returns the set of entity URIs the query mentions, either:
- By exact alias match (`alias_index` has `Marcelo` → `person:marcelo`).
- By tokenized substring match against the alias table.

This set feeds step 4 (entity-aware rerank). If empty, step 4 becomes a no-op.

### 3.3.bis Auto-keyword detection (audit E14, P3.3)

`_detect_auto_keywords` (`durin/memory/query_router.py`) scans the
query for an identifier-shaped token. If one is found, the lexical
weight in the RRF fusion is boosted (0.7 → 2.5) automatically —
the agent does NOT have to pass `keywords` explicitly. Documented
as P3.3 in commit `bc55686`; audit E14 (2026-05-28) lifted it from
implementation detail into this spec.

Matched patterns (order matters: longest / most-specific first):

| Pattern | Example | Why |
|---|---|---|
| `https?://...` | `https://github.com/foo/bar` | URLs need verbatim match — tokenisers split them |
| File paths with extension | `src/foo/bar.py`, `/Users/.../file.md` | Same |
| Bare absolute / relative paths | `/var/log/app.log` | Same |
| UUID (with or without dashes) | `550e8400-e29b-41d4-a716-446655440000` | Hex sequences degrade in cosine matching |
| Email addresses | `mmarmol@mxhero.com` | Verbatim match wins; cosine misses subdomain noise |

When a match fires, `RoutingDecision.auto_keywords` carries the
matched substring verbatim. The pipeline forwards
`keywords_provided=bool(keywords or decision.auto_keywords)` to
the RRF fusion (`search_pipeline.py:121`); the lexical weight bumps
identically whether the boost came from the agent's `keywords`
input or from auto-detection.

Explicitly NOT matched:
- Version strings (`v1.2.3`) — too ambiguous; would catch
  conversational "version 1" mentions and over-boost.
- Bare numbers (`12345`) — high false-positive rate.
- Domain-only (`mxhero.com`) — captured incidentally by the URL
  pattern when prefixed with `http(s)://` only.

### 3.3 Whitespace normalization

The query is NFC-normalized and whitespace-collapsed before being passed to both retrieval engines. This avoids embedding mismatches on the same query with different leading/trailing whitespace.

---

## 4. Step 2a — Vector search (LanceDB)

### 4.1 Inputs

The normalized query string.

### 4.2 Mechanics

```python
vector = MiniLM.embed(query)                       # 384-dim (audit F3, 2026-05-28)
rows = lancedb.search(vector, top_k=50)            # cosine distance
```

LanceDB returns up to 50 hits with `_distance` column (cosine distance; lower is better). Hits carry the row's persisted columns plus `_distance`: `id` (used as `uri`), `class_name` (mapped to `type`), `summary`, `headline`, `valid_from`, `entities`, `path`. Audit E12 (2026-05-28) removed a stale `mtime` claim — the LanceDB schema does NOT store `mtime`; the temporal-decay step (§10) reads file `mtime` from disk when needed.

The top-K size for this step is **50** (not 10). The pipeline narrows down later through rerank and MMR. Retrieving more candidates here gives the rerank steps meaningful room to operate.

### 4.3 What gets searched

All `.md` files indexed in LanceDB per `02_indexing.md` §3.3:
- Entity pages, episodic, stable, corpus, session summaries.
- Excludes archive, pending, raw sessions/ingested.

### 4.4 Scope filtering

If `scope=dreamed`, vector results are kept as-is (they only contain dreamed classes by construction). If `scope=undreamed`, the vector path is **skipped** (step 2a returns empty); only the grep fallback (§6 below) provides results. If `scope=all`, vector results are taken and the grep fallback runs in parallel.

---

## 5. Step 2b — Lexical search (FTS5)

### 5.1 Routing

Based on §3.1 CJK detection:

| Branch | SQL |
|---|---|
| `unicode61` (no CJK) | `SELECT ... FROM memory_fts WHERE text MATCH ? ORDER BY bm25(memory_fts) LIMIT 50` |
| `trigram` (CJK ≥ 3, tokens ≥ 3 chars) | `SELECT ... FROM memory_fts_trigram WHERE text MATCH ? ORDER BY bm25(memory_fts_trigram) LIMIT 50` |
| `LIKE` substring (short CJK) | `SELECT ... FROM memory_fts WHERE text LIKE '%query%' LIMIT 50` (no scoring — returned in arbitrary table order; audit E12 corrected pre-existing "mtime order" claim — there is no ORDER BY) |

### 5.2 Query construction

The raw query is quoted token-by-token to escape FTS5 special characters (`%`, `*`, `:`), with `AND`/`OR`/`NOT` operators preserved unquoted. Pattern from Hermes-agent `hermes_state.py:2207-2213`.

If `keywords` is provided (the optional second input parameter), the lexical query becomes the conjunction of `query` tokens + `keywords` tokens — boosting hits that match both.

### 5.3 BM25 scoring

FTS5 returns negative scores (more negative = better match). Audit
E13 (2026-05-28) removed an obsolete normalisation paragraph: the v1
pipeline normalised `score = -bm25_raw / max(-bm25_observed)` to
`[0,1]` so BM25 could be linearly fused with cosine scores. The v2
pipeline fuses via RRF (§7) which operates in **rank space** and is
score-scale invariant — the raw BM25 value is read only for the
sort within the lexical source; the rank position is all that
crosses into the fusion step.

---

## 6. Grep fallback (raw sessions and ingested)

When `scope=undreamed` or `scope=all`, the pipeline also runs `search_undreamed(workspace, query)` — a literal `ripgrep`-style scan over `sessions/<id>/<id>.jsonl` and `ingested/<id>/`. These artifacts are not in LanceDB or FTS5 by design (see `01_data_and_entities.md` §3.1, §3.2; `02_indexing.md` §3.3).

Results from this fallback enter the pipeline as a third source feeding into the RRF fusion in §7 (alongside vector + lexical). Per E13: there is no "normalised score" or "weighted merge" — RRF takes the per-source rank position only.

This path is the only way short, non-indexed session turns or raw ingested artifacts become reachable. It is conceptually different from FTS5 and serves a different content layer.

---

## 7. Step 3 — Weighted merge (cross-RRF)

Three result sets exist after steps 2a, 2b, and §6: `vector_results`, `lexical_results`, and `grep_results` (only when scope includes undreamed).

### 7.1 Reciprocal Rank Fusion (RRF)

The pipeline computes a fused score per uri:

```
RRF_score(uri) = Σ over sources:  w_source / (k + rank_in_source(uri))
```

Where:
- `k = 60` (standard RRF constant).
- `w_source` is a per-source weight (defaults, see §7.2 for dynamic boost):
  - `w_vector = 1.0`
  - `w_lexical = 0.7` (slightly lower because lexical can hit on common words)
  - `w_grep = 0.3` (lower still — grep is best-effort fallback, not a primary scoring signal)
- `rank_in_source(uri)` is the 1-based rank of `uri` in that source's result list. If not present, the term contributes 0.

A uri appearing in **multiple** sources accumulates contributions and ranks higher. This implements the principle of §9.2 of doc 29: items found by both vector and lexical should rank above items found by only one.

### 7.2 Dynamic weight boost when `keywords` is provided

When the agent passes the optional `keywords` parameter (§2.1), the LLM has explicitly signaled that some literal substring is important. The pipeline boosts `w_lexical` for that search to elevate hits matching the keyword:

| Condition | `w_lexical` (during this call only) |
|---|---|
| `keywords` is None or empty | 0.7 (default) |
| `keywords` provided | **2.5** (boosted) |

Reasoning: a hit that matches the keyword exactly in FTS5 likely sits at rank 1-3 in lexical results. With `w_lexical = 0.7` it might lose to a vector hit ranked 1; with `w_lexical = 2.5` it surfaces robustly. This avoids the need for a separate "pinned exact match" mechanism and removes the burden of measuring keyword specificity — the LLM's choice to pass `keywords` is the signal that the literal match matters.

No similar boost is applied to `w_vector` or `w_grep`. Grep is a fallback and the system trusts the LLM's hint as a lexical-side signal, not a grep-side signal.

If the LLM does NOT use `keywords` when it should have (tool description = weak signal), bench will surface this as `no_retrieval` failures for exact-match queries. The fix at that point is improving tool description / identity.md guidance, not redesigning the retrieval mechanism.

### 7.3 No "pinned exact matches" mechanism

The MVP intentionally does **not** include a pin-by-modality feature (i.e., guaranteeing a grep_exact hit visibility regardless of RRF score). Reasoning:
- Without measuring keyword specificity (IDF, hit count, pattern type), pin becomes noise for common keywords.
- The `keywords` + dynamic boost mechanism above already covers the legitimate case (LLM explicitly identified the literal match it cares about).
- No mainstream system (mem0, hermes, openclaw, cognee, graphiti, letta) implements pin-by-modality.

If bench shows that `keywords` is under-used and exact matches still get lost, the mitigation is to add a specificity-aware pin (using hit-count + pattern detection). Not in MVP.

### 7.2 Deduplication

If the same uri appears in multiple sources, only one row carries forward, with the merged RRF score.

### 7.3 Output

Up to 50 uris with their fused scores. Carried forward to step 4.

---

## 8. Step 4 — Entity-aware rerank

Reuses the existing `entity_ranker` (`durin/memory/entity_ranker.py`). When `query_entities` from §3.2 is non-empty, hits whose `entities` column contains any of those URIs receive an RRF boost combined with the fused score from step 3.

### 8.1 RRF constant `K = 60`

```
RRF_score(uri) = Σ over lists:  1 / (rank_in_list(uri) + K)
```

K = 60 follows **Cormack, Clarke, Buettcher 2009** ("Reciprocal Rank Fusion outperforms Condorcet and individual Rank Learning Methods"). Standard across IR systems. Tuning the constant:

- **Smaller K** weights top ranks more heavily — more aggressive ranking.
- **Larger K** flattens contributions — top-1 and top-5 contribute almost equally.
- **K = 60 is the de-facto standard.** Graphiti uses it. We adopt without modification.

### 8.2 Why RRF, not multiplicative boost

The naive alternative is `combined_score = vector_score × entity_match_score` (or additive equivalent). Rejected because:

- LanceDB L2 distances are **non-linear and corpus-dependent**: for poorly-normalized embeddings they can range 10-50; for well-normalized, 0-2. Applying `score × 1.5` over `1/(1+d)` distorts ordering rather than improving it.
- RRF operates in **rank space** — invariant to score-scale differences across sources. The same algorithm works for cosine, L2, BM25, and any other rank-producing signal.

This is the same reason cross-RRF is used in step 3 (§7.1) to combine vector + lexical + grep. Consistency across the pipeline.

### 8.3 List-length asymmetry — deliberate

The entity-match list is typically much shorter than the vector list (3-5 items vs 50-100). Through RRF, this means the entity signal accumulates less aggregate weight than the vector signal.

**This is deliberate.** Entity matching is a **nudge** to surface the canonical entity page + fresh post-cursor entries, NOT an override of semantic similarity. The signal "this hit is tagged with an entity the query mentions" is a useful hint but should not flip top-1 against strong cosine evidence.

Documented in `entity_ranker.py` module docstring (note "G9").

### 8.4 Pre/post-cursor logic

For each query entity URI, the pipeline reads the entity page's `dream_processed_through` cursor. Episodic/stable hits associated with that entity are partitioned:

- **Post-cursor entries** (timestamp newer than the cursor): enter the entity-match list with recency-based ordering. These are recent observations the canonical entity page does not yet reflect.
- **Pre-cursor entries** (already consolidated into the entity page): **excluded from the entity-match list entirely**. Their information already lives in the canonical page; surfacing the raw entry would duplicate context.

The canonical entity page (`type=entity`) is always in the entity-match list when its URI matches a query entity — independent of cursor logic.

This avoids the redundancy of returning `entity:marcelo` (canonical, with all the facts) alongside 5 episodic entries each repeating one of those facts.

### 8.5 Ordering when query mentions multiple entities

When `query_entities` has N > 1 (e.g., query "what did Marcelo say about Susana"), the entity-match list contains canonical pages for both. Ordering within: **preserves vector-sort order** of the input candidates. No internal ranking among pages — the assumption is that whichever the vector ranked higher is more relevant to the specific query phrasing.

Documented in `entity_ranker.py` module docstring (note "G13").

### 8.6 Output

The 50 hits, reordered. Score modified by the entity-aware boost. Carried forward to step 5 (cross-encoder, when enabled) or directly to step 6 (temporal decay).

---

## 9. Step 5 — Cross-encoder rerank (opt-in, OFF by default)

A dedicated reranker model (no LLM, dedicated transformer) takes the top-50 hits from step 4 and the original query, scoring each hit on full query-document relevance. The model runs the query and each document text **together** in a single forward pass, unlike bi-encoders (LanceDB embeddings) that score them separately.

**This step is OPT-IN, OFF by default.** Reasoning:

- All multilingual cross-encoder models add 300-1500ms latency on CPU, breaking the default ~100ms search budget.
- All comparable systems (mem0, graphiti) ship cross-encoder reranking as opt-in, not default-on.
- The fused RRF + entity-aware rank from steps 3-4 already produces useful retrieval for most queries.
- Users who want maximum retrieval quality (and accept the latency cost) enable it via config or UI.

When OFF (default), the pipeline jumps from step 4 directly to step 6 (temporal decay) or step 7 (MMR).

### 9.1 Default model (when enabled)

**The model set is open** (audit B12, 2026-05-28). The four entries below are bundled in the install as suggestions surfaced in the webui datalist and the onboarding wizard, but the config field accepts any `sentence_transformers.CrossEncoder` compatible id — HuggingFace handles, local paths, future ollama / API-served models. Validation is dynamic: the operator clicks "Test" in Settings → Memory (or runs `durin doctor`) and the backend loads + scores a trivial pair, surfacing the result before the value is committed. No closed enum is enforced in the backend or in the frontend dropdown.

When the user enables the cross-encoder, the default model is **`jinaai/jina-reranker-v2-base-multilingual`**:

| Property | Value |
|---|---|
| Params | 278M |
| RAM | ~1.1GB |
| Multilingual | Yes (100+ languages incl. CJK) |
| Context length | 1024 tokens |
| CPU latency (50 docs, batched) | ~300-800ms |
| HuggingFace | `jinaai/jina-reranker-v2-base-multilingual` |

Why this choice (over alternatives, see also §16 #4):

| Alternative | Why not default |
|---|---|
| `cross-encoder/ms-marco-MiniLM-L-6-v2` (22M, ~80ms CPU) | English-only; fails silently on multilingual queries. Anti-UX for durin's multi-channel reality. |
| `BAAI/bge-reranker-base` (278M) | Comparable size; older training. Used by mem0 as their default. Slightly weaker multilingual than jina-v2. |
| `BAAI/bge-reranker-v2-m3` (568M, ~2.3GB RAM, ~1500ms CPU) | Top-tier multilingual but 2x RAM and 2x latency vs jina-v2. Used by graphiti as their default. Available as an opt-in upgrade. |
| `Qwen3-Reranker-0.6B` (600M, ~2.4GB RAM) | Top MTEB 2026; very new. Available as an opt-in upgrade for power users. |

The configuration also exposes a `model` field so users can pick any of the alternatives without code changes.

### 9.2 Input shape

The cross-encoder receives `(query, doc_text)` pairs. The `doc_text` is composed similarly to the embedding text (§4 of doc 02) but capped at the model's context (1024 tokens for jina-v2). For entity pages, it's `name + summary + body_first_chars`. For entries, it's `headline + summary + body`.

### 9.3 Score combination

When enabled, the cross-encoder score replaces the fused score for the top-50 set. Hits ranked below position 10 by the cross-encoder are dropped. The remaining top-10 carry forward.

### 9.4 Graceful degradation

If the cross-encoder is enabled but the model fails to load (missing file, OOM, incompatible CPU instruction set), step 5 logs a warning and becomes a no-op. Fused scores from step 4 carry forward. Search continues working at default latency.

### 9.5 Configuration surface

The cross-encoder setting is surfaced in three places, all reading the same workspace config:

| Surface | How |
|---|---|
| **Workspace config file** (`~/.durin/config.json`) | `memory.search.cross_encoder.{enabled, model, batch_size}` — power users edit directly |
| **Onboarding wizard** (durin install CLI) | A question during setup: "Enable advanced re-ranking for better search results? (Adds 300-1500ms per query, requires ~1GB RAM)" — default No |
| **Web dashboard settings** | A toggle under Memory → Search with the same trade-off explanation. Includes a dropdown for picking the model from the curated list (jina-v2, bge-base, bge-v2-m3, qwen3-reranker-0.6b). |

Detailed specs for these UI surfaces live in `06_prompts_and_instructions.md` (onboarding text) and the webui component spec (out of scope for this corpus).

---

## 10. Step 6 — Temporal decay

Default **enabled with generous half-lives**, but applied **only to types whose timestamp is intrinsically information** (observation-like). Types representing canonical state (entity, stable, corpus) do NOT decay.

### 10.1 Conceptual model

Decay is meaningful for documents where the time of the document IS information about the content. A session that happened 2 years ago is intrinsically older than one from yesterday. But an entity page that says "Marcelo lives in Spain" represents the canonical current state — its `mtime` reflects "when Dream last touched the page", not "how old the fact is". Decaying entity pages would be wrong: untouched ≠ obsolete.

### 10.2 Per-class defaults

```
decayed_score(hit) = score(hit) × exp(-Δt / half_life)
```

Where `Δt` is time since the document's `mtime` in days, and `half_life` is looked up per class:

| Class | Half-life default | Reasoning |
|---|---|---|
| `episodic` | **90 days** | Observation with timestamp; naturally ages |
| `session_summary` | **120 days** | Past conversation; ages with time |
| `entity` / `entity_page` | **null (never decays)** | Canonical state; `mtime` is "last Dream update", not "fact age". Audit E15 (2026-05-28) added the `entity_page` alias to match `CLASS_HALF_LIFE_DEFAULTS` (`decay.py:67-73`); the LanceDB row carries `class_name="entity_page"` while in-memory entries use `"entity"`. Decay lookups normalise both. |
| `stable` | **null (never decays)** | Explicit user intent to persist |
| `corpus` | **null (never decays)** | Chunk of an ingested source; source relevance dictates, not chunk age |

Hits older than `5 × half_life` (about 450 days for episodic, 600 days for session) round to ~0.7% of original score — effectively suppressed but not deleted. Strong relevance can still surface them.

### 10.3 Per-entry override

A document's frontmatter can specify `decay_half_life: <int|null>` to override the class default. This handles edge cases:

```yaml
---
id: 2010-05-15-marcelo-wedding
headline: "Marcelo y Susana se casaron en 2010"
decay_half_life: null   # permanent fact; override the 90-day episodic default
---
```

Dream sets this field when it recognizes a fact as permanent (timestamp-bound vs eternal). Users can edit manually. Without the field, the class default applies.

### 10.4 Evergreen exemptions

A flag `evergreen: true` in frontmatter forces no decay regardless of class or override. By default this applies to `memory/MEMORY.md` (the index) and to any entry or entity the user explicitly marks. Evergreen wins over `decay_half_life`.

### 10.5 Decision logic

```
half_life_for(doc):
  if doc.frontmatter.evergreen == True:
      return null
  if 'decay_half_life' in doc.frontmatter:
      return doc.frontmatter.decay_half_life
  return CLASS_DEFAULT[doc.type]
```

If `half_life` is null, the decay step is a no-op for that hit. Score passes through unchanged.

### 10.6 Why default enabled (revised)

The previous draft said "disabled by default". This is revised. Reasoning:

- Decay applies only to types where it conceptually makes sense (observations).
- On types where it does NOT make sense (entities, stable, corpus), the default is null — same effect as disabled.
- For types where it does apply (episodic, session_summary), recent docs in an MVP workspace barely register decay (half-lives are generous).
- Comparable systems (mem0, openclaw) ship with decay on. Durin aligning is consistent.
- Removes the "discover later" UX where a user with an aging workspace doesn't know to enable it.

The mechanism can be globally disabled via `memory.search.temporal_decay.enabled = false` if a workspace operator wants to opt out.

### 10.7 What audit A9 actually shipped (2026-05-28)

Up to audit A9, §10 was a promise — `decay.py` had the half-life table and the `half_life_for` resolver, but the search pipeline never called either. A9 wired the ranking-time consumer:

- New helper `durin.memory.decay.apply_class_decay(score, class_name, valid_from_iso, now=None) → (decayed_score, decay_factor)` — pure function, never raises.
- New pipeline step `_temporal_decay_step` in `durin/memory/search_pipeline.py`, inserted after the cross-encoder and before sectioning. Reorders `fused` by the new scores so the per-source cap (`apply_per_source_cap`) and the final `[:limit]` slice see the decayed ranking.
- New config knob `memory.search.temporal_decay.enabled: bool = True` (`MemoryTemporalDecayConfig`). Read by `memory_search.execute` and threaded through to `run_search_pipeline(..., temporal_decay_enabled=...)`.
- New telemetry event `memory.recall.decay` with `{hits_total, hits_decayed, avg_decay_factor}` so a dashboard can see how often decay actually bites.

**Scope of A9 — class defaults only.** The per-entry override (`evergreen`, `decay_half_life` in `MemoryEntry`) is honoured in paths that read the full entry off disk (hot_layer, dream consolidator). The search pipeline only sees `meta` dicts derived from LanceDB / FTS5 rows, which don't carry those fields today. Adding them would require a LanceDB schema bump (3 → 4) and a forced rebuild — verified `grep` shows zero producers actually set `evergreen` or `decay_half_life` in entries today (Dream's prompt doesn't instruct the LLM to emit them; no entry in the workspace uses them). The override stays declared in `MemoryEntry` and resolved by `half_life_for`; if a future use case shows up, the second step is promoting those two fields into the row schema.

**Per-class decision table** (verified by enumeration — recorded in doc 11 A9):

| Class | Half-life | Why |
|---|---|---|
| `entity_page` (alias `entity`) | null | `valid_from = ""`; file mtime tracks "last Dream pass", not "age of fact" |
| `episodic` | 90 days | Observations naturally age |
| `stable` | null | Explicitly marked durable by user/agent |
| `corpus` | null | `valid_from` is the INGEST date, not content date — decay would penalise old books in your pipeline |
| `session_summary` | 120 days | Session digests age slower than raw episodic (broader topic surface) — currently inert until A10 emits these rows |
| `pending` | N/A | Walker excludes it (A2) — never reaches the pipeline |

---

## 11. (Removed) MMR — deferred to backlog

The original plan included a Maximal Marginal Relevance (MMR) step here to diversify top-K results. Audit E31 (2026-05-28) renamed this section from "Step 7 — MMR" to "(Removed) MMR" so the live pipeline numbering stops at step 6 (temporal decay) and §12 picks up directly with sectioning. The MMR step was **removed from the MVP** after analysis:

1. **Archive of consolidated episodic** (§3.6 of doc 01) already eliminates the primary source of top-K duplication. Post-archive, the typical pattern is `entity (canonical) + 0-3 fragment + 1 session_summary` — that's triangulation, not redundancy, and the agent wants to see it.
2. **Mainstream systems don't use MMR.** mem0, graphiti, hermes, letta, cognee: none implement it. Only openclaw does.
3. **Cost is non-zero**: ~50-100 LOC + a `λ` hyperparameter to tune without clear metric, additional test surface.
4. **Risk of regression** on exact-match queries: MMR can push the best hit out of top-K in favor of diversity, which hurts queries like "exact email of X".

If post-MVP bench shows residual duplication (e.g., > 2 nearly-identical hits in top-10 after archive is active), MMR can be added then — it's a standalone algorithm with no dependencies on the rest of the pipeline.

**The remaining duplication concern** — corpus chunks from the same ingested source surfacing together — is handled differently in step 8 (per-source cap, §12.4).

---

## 12. Step 7 — Sectioning and rendering (audit E31, 2026-05-28: renumbered to step 7 after MMR was officially removed from the pipeline)

The final top-K (default 10) is grouped by source class and rendered with structural markers. This is what the agent sees.

### 12.1 Sections

Audit F4 (2026-05-28) completed the Phase 3 sectioned-rendering
migration. The `memory_search` tool now emits a single
`sectioned_rendered` string carrying section intros + per-block
markers + END closes — the per-row `rendered` field was dropped
(WebUI consumes raw fields directly; the LLM consumes the sectioned
string).

| Marker | Source class | Order priority |
|---|---|---|
| `=== CANONICAL: <uri> (consolidated <ts>) ===` (ts present) or `=== CANONICAL: <uri> (canonical entity page) ===` (no ts) | entity pages | 1 |
| `=== FRAGMENT: <path> (ts <ts>) ===` or `=== FRAGMENT: <path> ===` (no ts) | episodic post-cursor + stable | 2 |
| `=== SESSION: <uri> (ts <ts>) ===` or `=== SESSION: <uri> ===` (no ts) | session summaries + raw session hits | 3 |
| `=== INGESTED: <ingest_id>/<uri> ===` | corpus + raw ingested hits | 4 |

Each block closes with `=== END KIND ===` so the LLM can boundary-detect without relying on section intros.

### 12.2 Rendering rules

- Within each section, hits ordered by score descending.
- Body inside each block follows the preference `summary > body > snippet` so warm-tier responses stay compact.
- Non-canonical hits carry an `Entities: <ref>, <ref>` tail so the LLM can drill to the canonical entity page.
- Sections with zero hits are omitted entirely.
- Section intros precede each section (e.g. "Consolidated entity pages — the main memory; fragments below amend them with newer information.") — descriptive metadata only, no valuative language ("treat as authoritative", "trust this"), which has been verified as weak signal.

### 12.3 Snippets

For warm-level results, each hit has a 200-char snippet around the strongest BM25 match (if from lexical) or the headline (if from vector). For cold-level, snippet is replaced by the full body.

### 12.4 Per-source cap (avoids duplication of corpus chunks)

When an ingested document (e.g., a long PDF) was chunked into many corpus entries, a single semantic query can match 5-10 consecutive chunks of the same source. Without intervention, top-K becomes monotopic.

The sectioning step applies a **per-source cap** at most 3 hits per `ingest_id` reach the final result set. Concretely:

- Group corpus hits by `source_refs[0]` or `ingest_id` extracted from the corpus entry's frontmatter.
- For each group with > 3 hits in the top-K candidates: keep the top-3 by score, drop the rest.
- Hits below position 3 within the same group are removed from the final output.

This is **only applied to corpus** (the class where same-source clustering is structural). Other classes don't get capped:
- Entity: one entity = one canonical page; clustering by source isn't meaningful.
- Episodic/stable/session_summary: each has independent provenance; if 3 episodic mention the same fact, that's triangulation (see §11).

The cap value (`3`) lives in `durin.memory.sectioned_output.DEFAULT_MAX_PER_SOURCE` and is currently hard-coded. The `memory.search.sectioning.max_per_source` config knob is **not yet implemented**; if operators report needing to tune the cap, lifting it into config is a small change (mirrors the F1 `class_half_life_overrides` pattern).

---

## 13. Latency guidelines (observable expectations, not SLAs)

These numbers are **guidelines for what to expect**, not hard SLAs. They serve to inform debugging — if production telemetry shows persistent deviation from these ranges, the system is misbehaving and should be investigated. They are not promises to the user.

End-to-end p95 for hot path, **default configuration (cross-encoder disabled)**:

| Step | Target |
|---|---|
| 1 — Query analysis | < 1ms |
| 2a — Vector search | 5-15ms (LanceDB ANN) |
| 2b — Lexical search | < 5ms (FTS5 indexed query) |
| 6 — Grep fallback (when applicable) | 20-100ms (depends on workspace size) |
| 3 — Weighted merge | < 1ms |
| 4 — Entity-aware rerank | < 5ms |
| 5 — Cross-encoder rerank | **skipped (default)** |
| 6 — Temporal decay | < 1ms |
| 7 — Sectioning + per-source cap | < 5ms |
| **Total (default)** | **~30-130ms p95** |

When cross-encoder is enabled by the user, latency increases as follows:

| Model | Cross-encoder step | Total p95 |
|---|---|---|
| `jina-reranker-v2-base-multilingual` (default when enabled) | 300-800ms | ~400-900ms p95 |
| `bge-reranker-v2-m3` (heavy multilingual upgrade) | 600-1500ms | ~700-1600ms p95 |
| `qwen3-reranker-0.6b` (top MTEB) | 700-1500ms | ~800-1600ms p95 |

The user is informed of this trade-off both in the onboarding wizard (§9.5) and the dashboard setting, so the choice is explicit.

### 13.1 Operational rule

A search call should never exceed **10 seconds when no recovery action is active** (§14). If telemetry shows calls >10s without `recovered_from` set in the response, that is a signal of regression: index degradation, model bloat, disk contention, etc. Investigate.

When recovery IS active, longer latency (up to the per-failure timeout in §14.1, max 60s for cross-encoder model download) is expected and surfaced to the caller via `recovery_duration_ms`.

Concrete trigger for investigation:

| Condition | Action |
|---|---|
| p95 in production > 2× guideline range for the active configuration | Investigate (capture trace, check telemetry breakdown by step) |
| Single call > 10s without `recovered_from` | Log as anomaly; investigate the offending query/state |
| Recovery latency consistently hits its timeout | Investigate root cause of the underlying corruption / failure |

---

## 14. Failure handling — graceful degradation in hot path + async health-check restoration

Two-tier model:

1. **Hot path: graceful degradation.** When a component fails during a query, the pipeline bypasses it and serves results from the remaining components. NO synchronous rebuilds or model downloads in the hot path. Latency stays bounded.
2. **Cold path: health-check cron.** A background process periodically (default every 15 min, configurable) probes index health and triggers async restoration when problems are detected. The next query post-restoration uses the restored component normally.

Rationale: index corruption is infrequent (typically not at all in a healthy workspace). Paying complexity for synchronous-in-hot-path recovery is over-engineered for the failure rate. Async restoration matches the actual cadence at which problems arise. Pattern verified in OpenClaw's QMD sidecar (background readiness probes + auto-restore).

### 14.1 Hot path — graceful degradation

When a component fails mid-query, the pipeline degrades transparently:

| Failure | Pipeline behavior |
|---|---|
| LanceDB unavailable or row missing | Step 2a returns empty. Pipeline runs lexical-only + grep. |
| FTS5 corrupted or query syntax error | Step 2b returns empty. Pipeline runs vector-only + grep. |
| Both indices unavailable | Fall back to raw grep over `memory/`. Slow but functional. |
| Cross-encoder model unavailable | Step 5 no-op. Fused scores carry forward. |
| Hit's `.md` deleted between index and query | Filter the hit out at sectioning. Queue stale index entry for cleanup. |
| Disk read transient failure | Retry once after 100ms (cheap). If still fails, treat the affected file as missing. |

Every failure emits a `memory.search.failure` event (§14.5) so the cron picks it up on next tick (or sooner, see §14.3).

The pipeline returns an error ONLY when ALL retrieval paths fail simultaneously (`{results: [], total: 0, error: "memory subsystem unavailable"}`).

### 14.2 Cold path — health-check cron

A background scheduler runs the health check loop:

| Probe | Action when failing |
|---|---|
| LanceDB table open and rows queryable | If failed → rebuild from `.md` (background, no lock contention with hot path readers) |
| FTS5 tables open and queryable | If failed → rebuild from `.md` |
| Cross-encoder model loaded (if `cross_encoder.enabled = true`) | If missing → trigger one-time download |
| File watcher process running | If down → restart; if it fails twice in 1h, leave dormant + emit critical event |
| Disk free space | Warn if < 1GB free, critical if < 100MB |

**Restoration uses the existing `.dream.lock`** (same lock as Dream apply, per `02_indexing.md` §6.3) to avoid races with index writes. If the lock is held by Dream, the health check skips for this tick and retries on the next.

Each probe emits `memory.health_check` event (`07_telemetry_and_observability.md` §9.4) regardless of pass/fail, so a dashboard can show "last 24h: 96 OK, 0 degraded".

### 14.3 Eager trigger after observed failure

The cron does not have to wait its 15-min tick when a hot-path failure occurs. When `memory.search.failure` fires, the cron schedules an extra run in the next 30 seconds. This shortens the worst-case "search degraded" window from 15 min to ~30 sec without changing the hot path.

### 14.4 Escalation when restoration fails

If 3 restoration attempts within 1h all fail for the same component, the cron:
- Emits `memory.health.critical` event.
- Stops attempting that component until an operator manually triggers a recovery (`durin reindex` or `durin memory health restore --component <name>`).
- Other components continue normal probing.

This prevents wasteful retry loops on a fundamentally broken state (e.g., disk full, missing files).

### 14.5 Configuration

Under `memory.health_check.*`:

```json
{
  "memory": {
    "health_check": {
      "enabled": true,
      "interval_seconds": 900,
      "eager_trigger_after_failure_seconds": 30,
      "disk_warn_gb": 1.0,
      "disk_critical_mb": 100,
      "max_consecutive_failures_per_component": 3,
      "failure_window_hours": 1
    }
  }
}
```

### 14.6 Error response

When ALL retrieval paths fail simultaneously:

```json
{
  "results": [],
  "total": 0,
  "error": "memory subsystem unavailable",
  "telemetry_id": "<uuid>",
  "health_check_next_run_in_seconds": 22
}
```

`health_check_next_run_in_seconds` informs the caller (the agent or the operator UI) that restoration is scheduled. The agent can mention "memory is recovering, please retry in a moment" rather than just failing.

### 14.7 Failure telemetry

Every hot-path failure emits `memory.search.failure`. The canonical
field list (component, recovery_attempted, recovery_succeeded,
recovery_duration_ms, degraded_to) lives in
[`07_telemetry_and_observability.md` §8.1](07_telemetry_and_observability.md);
audit B9 (2026-05-28) shipped this event and audit E8 (2026-05-28)
collapsed this section to a single pointer so doc 03 and doc 07
stop drifting against each other.

The v1 spec proposed `kind` (exception classification enum) and
emphasised "No `recovery_attempted` field". Both were cut by B9 — the
safe wrappers catch generic `Exception` (so `kind="syntax"` vs
`kind="timeout"` would be inventing data), and the pipeline always
attempts recovery inline so `recovery_attempted` is always `True`
(field kept as forward-compat marker).

The cron's restoration attempts emit separate `memory.health_check`
events (doc 07 §9.4) — not `memory.search.failure`.

---

## 15. Configuration

The actual configuration surface in `MemorySearchConfig` (`durin/config/schema.py`) — audit B8 (2026-05-28) aligned this section to code reality:

```toml
[memory.search.cross_encoder]
enabled = false
model = "jinaai/jina-reranker-v2-base-multilingual"
batch_size = 32
top_n = 10

[memory.search.temporal_decay]   # A9
enabled = true
```

**What's hardcoded** (not exposed as config keys today — audit B8 deliberate scope decision):

| Hardcoded knob | Value | Where | Why no config? |
|---|---|---|---|
| `vector_top_k` | 50 | `search_pipeline.py:444` (audit F21 verified, 2026-05-28) | Sane default; tuning never asked for |
| `lexical_top_k` | 50 | `search_pipeline.py:459` (audit F21 verified) | Sane default |
| `rrf_constant` | 60 | `rrf_fusion.py::DEFAULT_K` | Textbook value (Cormack/Clarke/Buettcher 2009); no operator has requested override |
| `rrf_weights` | `{vector: 1.0, lexical: 0.7→2.5 boosted, grep: 0.3}` | `rrf_fusion.py::DEFAULT_W_*` | Already adaptive (lexical boost on identifier queries) |
| `max_per_source` (sectioning) | 3 | `sectioned_output.py::DEFAULT_MAX_PER_SOURCE` | Per doc 03 §12.4; tuning never asked for |
| `final_top_k` (a.k.a. `limit`) | 10 default | `memory_search.py` `_PARAMETERS["limit"]` (A3) | Now per-call configurable via the `limit` tool parameter (audit A3) — 1..50 |
| `half_life_days` per class | 90 / 120 / null | `decay.py::CLASS_HALF_LIFE_DEFAULTS` (A9) | Per-class table in code; doc 03 §10.2 documents the reasoning |

The earlier draft of §15 listed every hardcoded value as a config key as if it were configurable. The honest state is: **only the items shown in the TOML block above accept overrides today**. If a real operator workflow surfaces a need (with data), the relevant knob gets promoted to `MemorySearchConfig`. Until then, hardcoded defaults keep the config surface minimal.

All values shown are defaults. Most users never edit any of them.

---

## 16. Module-level decisions

All decisions consistent with cross-corpus decisions in `00_overview.md` §10 and module decisions in `01_data_and_entities.md`, `02_indexing.md`.

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| 1 | Vector top-K vs final top-K | 50 candidates from vector + 50 from lexical; reranked down to final 10. Wide enough for rerank to operate. | §4.2, §5.1 |
| 2 | Fusion algorithm + dynamic boost | **RRF (Reciprocal Rank Fusion)** with cross-source weights. Defaults: vector 1.0, lexical 0.7, grep 0.3, k=60. **Dynamic boost:** when the agent passes `keywords` explicitly, `w_lexical` is raised to 2.5 for that call. No pin-by-modality mechanism — pin requires keyword specificity measurement which adds complexity without justification (no mainstream system does it). | §7 |
| 3 | Entity-aware rerank | Reuse existing `entity_ranker`. Pre/post-cursor boost logic preserved. | §8 |
| 4 | Cross-encoder default state + model | **Opt-in, OFF by default.** When enabled, default model is `jinaai/jina-reranker-v2-base-multilingual` (278M, ~1.1GB RAM, ~300-800ms CPU). Selected over English-only MiniLM (silent multilingual failure), bge-base (older training), bge-v2-m3 (2x latency), and qwen3-reranker (very new). Pattern matches mem0/graphiti (opt-in). Configurable model field allows switching to bge-base, bge-v2-m3, or qwen3-reranker-0.6b without code changes. | §9, §9.1, §9.5 |
| 5 | Cross-encoder graceful degradation + UI exposure | Failure to load model logs warning; pipeline continues at default latency. **Exposed in onboarding wizard (CLI question with trade-off explanation) + web dashboard (toggle + model picker dropdown).** Workspace config file is the canonical source; UIs are surfaces over it. | §9.4, §9.5 |
| 6 | Temporal decay default | **Enabled by default, but only types where time-of-document is intrinsic information decay.** episodic (90d) and session_summary (120d) decay; entity, stable, corpus have `half_life = null` (no decay) because their `mtime` doesn't represent fact-age. Per-entry override via `decay_half_life` frontmatter field. Evergreen flag (`evergreen: true`) wins over all. Aligned with cross-corpus decision #3b (mechanism in place, conservative defaults). | §10 |
| 7 | MMR deferred to backlog | **Not in MVP.** Archive of consolidated episodic (§3.6 doc 01) eliminates the primary duplication that MMR would address; the remaining typical pattern is triangulation (entity + fragment + session), not redundancy. Mainstream systems (mem0, graphiti, hermes, letta) don't implement MMR. If post-MVP bench shows residual duplication, MMR is a standalone algorithm easy to add later. | §11 |
| 8 | Per-source cap (corpus chunks) | Sectioning step caps corpus hits to **max 3 per ingest_id** to prevent monotopic top-K when a long ingested doc was chunked into many corpus entries. Only applies to corpus; other classes are triangulation, not duplication. Configurable via `memory.search.sectioning.max_per_source`. | §12.4 |
| 9 | Sectioning markers | `=== CANONICAL/FRAGMENT/SESSION/INGESTED ===`. Descriptive metadata only — no valuative language. Empty sections omitted. | §12 |
| 10 | Failure mode — recovery first | **Recovery > Degradation > Error.** Recoverable failures (corrupt index, missing model, process crash) trigger synchronous recovery (rebuild, reload, reconnect) before serving the query — accepting up to 30s extra latency rather than returning a silently degraded result. Degradation only when recovery is exhausted or not applicable. Error only when ALL paths collapse. Recovery latency surfaces in the tool result (`recovered_from`, `recovery_duration_ms`) so the agent can communicate the delay. | §14 |
| 11 | Latency guidelines (not SLAs) | Default (cross-encoder OFF): ~30-130ms p95 end-to-end. When cross-encoder enabled: 400-1600ms p95 depending on model. **Operational rule:** any call > 10s WITHOUT `recovered_from` set is anomalous — investigate. Recovery-induced latency is expected and surfaced via `recovery_duration_ms`. | §13 |

### Open

None at the module level.

---

## 17. Implementation status

Audit E32 (2026-05-28) rebuilt this table — every row had been
stale since Phase 3 / Phase 4 shipped. Pointers go to the module
where the work lives.

| Aspect | Status | Where |
|---|---|---|
| Vector search | ✅ Active, top_k=50 | `durin/memory/vector_index.py::search` (callsites pass 50) |
| Lexical search | ✅ FTS5 with routing (unicode61 / trigram / like_substring) per §3.3.bis + §5 | `durin/memory/lexical_search.py` + `durin/memory/query_router.py` |
| Grep fallback | ✅ Used only for sessions/ingested; feeds RRF as third source | `durin/memory/search.py::search_undreamed` |
| Fusion | ✅ Cross-source RRF with weighted sources (vector=1.0, lexical=0.7→2.5 boosted, grep=0.3) | `durin/memory/rrf_fusion.py` |
| Entity-aware rerank | ✅ After RRF fusion; pre/post-cursor partition restored in audit E11 (2026-05-28) | `durin/memory/entity_ranker.py` + `durin/memory/search_pipeline.py::_entity_aware_rerank` |
| Cross-encoder rerank | ✅ Shipped (Phase 4). Default OFF, opt-in via `memory.search.cross_encoder.enabled`. Default model `jinaai/jina-reranker-v2-base-multilingual` when enabled. | `durin/memory/cross_encoder.py` |
| Temporal decay | ✅ Shipped (audit A9, 2026-05-28). Default ON for `episodic` (90d) and `session_summary` (120d); other classes `half_life=null`. | `durin/memory/decay.py` + `durin/memory/search_pipeline.py::_temporal_decay_step` |
| MMR | Removed from MVP (see §11). Re-add post-MVP only if bench shows residual duplication. | — |
| Sectioning | ✅ CANONICAL / FRAGMENT / SESSION / INGESTED markers active; empty sections omitted | `durin/memory/sectioned_output.py` |
| Configuration | ✅ `memory.search.*` config section with sub-Pydantic models | `durin/config/schema.py::MemorySearchConfig` |

---

## 18. Cross-references

**Note on the hot layer:** the search pipeline described in this document is the **lazy** retrieval path (invoked by the `memory_search` tool). A parallel **eager** retrieval mechanism — the hot layer — injects canonical entity pages, recent fragments, identity, and headlines into every agent prompt without any tool call. The hot layer is specified in `06_prompts_and_instructions.md` §8. Together they cover the recall (hot layer, always-on, fixed budget) + precision (this pipeline, on-demand, query-specific) spectrum.

---

- Data classes and entity model (input shapes): `01_data_and_entities.md`.
- Index schemas (LanceDB columns, FTS5 tables, tokenizer routing): `02_indexing.md`.
- Tools that invoke this pipeline (`memory_search`, `memory_drill`, etc.): `04_agent_tools.md` (pending).
- Markers and sectioning conventions: this doc §12; consumed by `04_agent_tools.md`.
- Cross-encoder reranker rationale: `00_overview.md` §10 #2.
- MMR and temporal decay rationale: `00_overview.md` §10 #3a, #3b.
