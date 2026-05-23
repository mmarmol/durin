# arch / memory — memory subsystem, dream, entity-centric retrieval

> Durin's memory layer: how the agent records what it learns, consolidates
> it into navigable entity pages, and retrieves it at query time.
>
> Design source of truth: [../18_entity_centric_plan.md](../18_entity_centric_plan.md)
> (principles + schema + retrieval) and
> [../archive/19_implementation_plan.md](../archive/19_implementation_plan.md) (phases, ejecutado).
> Forward-looking deferred items in
> [../25_post_t1_state_and_t2_horizon.md](../25_post_t1_state_and_t2_horizon.md).

---

## 1. Layers and sources of truth

```mermaid
flowchart TB
    subgraph srcs["Sources of truth (canonical)"]
        S["sessions/KEY.jsonl<br/>conversation turn log"]
        I["ingested/ID/source.ext<br/>external artifacts"]
        E["memory/CLASS/ID.md<br/>episodic + stable + corpus + pending entries"]
    end

    subgraph cons["Consolidation"]
        D["DreamConsolidator<br/>LLM pass over episodic entries"]
    end

    subgraph entities["Entity pages (derived)"]
        EP["memory/entities/TYPE/SLUG.md<br/>+ git history"]
        AR["memory/entities/TYPE/SLUG/archive/<br/>absorbed pages"]
    end

    subgraph idx["Indexes (derived)"]
        AI["AliasIndex<br/>rebuild-only"]
        VI["VectorIndex (LanceDB)<br/>entries + entity_pages"]
    end

    E --> D
    D --> EP
    EP --> AI
    E --> VI
    EP --> VI
    EP --> AR
```

Three canonical sources of truth + two derived layers (entity pages + indexes). The agent's `memory_search` consults the indexes; the markdown is always reconstructible from the entries + dream.

| Kind | Where | Mutability |
|---|---|---|
| Sessions | `<workspace>/sessions/<key>.jsonl` | Append-only |
| Ingested docs | `<workspace>/ingested/<id>/source.<ext>` | Frozen at ingest |
| Memory entries | `<workspace>/memory/<class>/<id>.md` | Mutable (agent or user) |
| Entity pages | `<workspace>/memory/entities/<type>/<slug>.md` | LLM-produced (dream), git-versioned |
| Aliases index | rebuilt in-memory from entity pages | Lazy, no sidecar |
| Vector index | `<workspace>/memory/.index.lance` (LanceDB) | Incremental upserts |

The 6 utility classes from doc 19 §0a map onto directories `memory/stable/`, `memory/episodic/`, `memory/corpus/`, `memory/pending/` (procedural skills and the prospective time-trigger half live in `skills/` and `cron/`).

---

## 2. On-disk layout

```
<workspace>/
├── sessions/<key>.jsonl            # canonical conversation log
├── sessions/<key>.meta.json        # derived: lifecycle events + summary
├── sessions/<key>.md               # derived: navigable view w/ #turn-N anchors
├── ingested/<id>/source.*          # canonical external artifact
├── ingested/<id>/meta.json         # derived: summary + entities + relations
├── memory/
│   ├── stable/<id>.md              # long-lived learnings
│   ├── episodic/<id>.md            # per-event observations (dream input)
│   ├── corpus/<id>.md              # large bodies of reference text
│   ├── pending/<id>.md             # entries awaiting promotion
│   ├── entities/
│   │   ├── <type>/<slug>.md        # consolidated entity page
│   │   └── <type>/<slug>/
│   │       └── archive/<slug>.md   # absorbed pages (linked from canonical)
│   ├── .git/                       # git substrate over entity pages
│   ├── .index.lance/               # LanceDB vector index
│   └── history.jsonl               # consolidator narrative summaries
```

Git lives under `memory/.git/` and tracks the entity pages — not the whole workspace, not the indexes. `.gitignore` keeps `*.lance/`, `.aliases.json`, `.usage.json`, `.dream.lock` out.

---

## 3. Entry schema

`durin/memory/schema.py` defines a pydantic `MemoryEntry` with `extra="forbid"`. Frontmatter carries multi-resolution:

- `headline` (~10 words) — pulled in bulk into the hot layer.
- `summary` (~50 words) — returned by `memory_search(level="warm")`.
- `body` (~200-500 words) — returned by `memory_search(level="cold")` or by `memory_drill`.
- `entities: list[str]` — typed refs `<type>:<value>` (see §4).
- `class_name`, `valid_from`, `valid_until`, `source_refs`, `author` (`agent_created` | `user_authored`, driven by `_MEMORY_AUTHOR` ContextVar).

Markdown links in `source_refs` point to specific session turns (`sessions/<key>.md#turn-N`) or document sections (`ingested/<id>/source.md#section`).

---

## 4. Typed entities

Format: `<type>:<value>`. `<type>` is lowercase `[a-z][a-z0-9_]*`, `<value>` is anything non-empty after the first `:`. Validation in `durin/memory/entities.py`. Eight suggested types from doc 18 §4 (open vocabulary — types outside the suggested set are legal):

```
person   place      project   topic
event    artifact   stance    practice
```

`SUGGESTED_TYPES` in `entities.py` is a hint for the dream prompt, not an enforced enum.

**Two-tier validation policy** (per archived doc 14 §3.2):

- `memory_store` write path → **strict**: invalid refs raise `InvalidEntityRefError` and the error returns to the model so it can rewrite.
- `consolidator_tags` read path → **lenient**: invalid refs are dropped with a log warning; the entry survives.

A model that writes `decision:auth-rewrite` succeeds — the type is captured verbatim. Pruning is via **ranker weight**, not by rejecting writes.

---

## 5. Entity pages

```yaml
# Frontmatter (open vocabulary — extra fields allowed)
---
type: person
name: Marcelo Marmol
aliases: [Marcelo, marcelo, mmarmol]
identifiers:
  email: [mmarmol@mxhero.com]
  github: [mmarmol]
dream_processed_through: 2026-05-20T18:30:00+00:00
---

# Marcelo Marmol

## Current State
…

## History
…

## Sources
- [s1](../../sessions/k1.md#turn-12)
…
```

Parser: [durin/memory/entity_page.py](../../durin/memory/entity_page.py). Open vocabulary — anything beyond the well-known fields lands in `page.extra`. Loader is tolerant: malformed frontmatter returns `None` instead of raising.

---

## 6. Dream consolidation

```mermaid
flowchart LR
    A["episodic entries<br/>tagged with person:marcelo<br/>(post-cursor)"] --> B["DreamConsolidator<br/>.consolidate_entity(ref, entries)"]
    B --> C["LLM (pydantic-validated<br/>retry + context budget)"]
    C --> D["===PAGE===<br/>frontmatter + body<br/>===COMMIT===<br/>subject + trailers<br/>===END==="]
    D --> E["EntityPage.save<br/>(force-set cursor)"]
    D --> F["GitRepo.commit<br/>(Sources, Entities-touched,<br/>Cursor-after trailers)"]
    E --> G["VectorIndex.upsert_entity_page<br/>(LanceDB row)"]
```

**Manual trigger only**: `durin memory dream [entity] [--dry-run]` in [durin/cli/memory_cmd.py](../../durin/cli/memory_cmd.py). Walks `memory/episodic/` for entries with entity tags newer than each entity page's `dream_processed_through` cursor, groups by entity, invokes the consolidator.

**Auto-trigger deferred** to T2 (see [doc 25](../25_post_t1_state_and_t2_horizon.md) §2.A): scheduling without LLM-judge + confirmation flow has silent-drift risk.

**Consolidator** ([durin/memory/dream.py](../../durin/memory/dream.py)) :

- Pydantic-validated LLM output (page + commit envelope).
- Retry on malformed response.
- Context budget guard (cap body shrink between revisions).
- `Cursor-after` trailer force-set on the page's `dream_processed_through` so the next pass excludes the entries we just absorbed.

**Commit envelope**:

```
Consolidate person:marcelo (rev 3)

Three observations merged: preference for pytest, ownership of durin,
glm-5.1 daily-driver.

Sources: e12, e15, e18
Entities-touched: person:marcelo
Entities-referenced: project:durin, topic:pytest
Cursor-after: 2026-05-20T18:30:00+00:00
```

Trailers are machine-readable and surfaced by `durin memory history` / `expand`.

---

## 7. Aliases index

`durin/memory/aliases_index.py`. **Rebuild-only** per doc 23 T1.4 — no `.aliases.json` sidecar, no save/load. Built in-memory on first call (sub-second for typical <100-page corpora).

Case-insensitive lookup keyed on `name` + `aliases` from each entity page's frontmatter. Returns a `list[str]` of entity refs (not a single ref) so alias collisions (R6 in doc 18 §10) are surfaced rather than masked.

Lazy build is invoked by `MemorySearchTool._get_alias_index()`; each tool instance builds its own. Sharing across tools via `ctx` is deferred to T2 ([doc 25](../25_post_t1_state_and_t2_horizon.md) §2.C).

---

## 8. Vector index

`durin/memory/vector_index.py` wraps a LanceDB table at `<workspace>/memory/.index.lance`. Schema:

| Field | Source |
|---|---|
| `id` | `MemoryEntry.id` for entries; `<type>:<slug>` for entity pages |
| `class_name` | `stable` / `episodic` / `corpus` / `pending` / `entity_page` |
| `summary` | entry summary or `name + aliases` for pages |
| `headline` | entry headline or `name` for pages |
| `vector` | embedding of composed text (`name + aliases + body`, budget 1500 chars, no `<type>:` prefix per Phase 0.1 finding) |
| `valid_from` | ISO timestamp from the entry; empty for pages |
| `entities` | list of entity refs the entry references; empty for pages |
| `path` | workspace-relative path |

Two write paths:

- `upsert(entry, class_name, path)` — incremental, called by `memory_store` after a single entry is written.
- `upsert_entity_page(entity_ref, name, aliases, body, path)` — called by `DreamConsolidator.apply` so consolidated pages enter the index alongside entries.

Read path: `search(query, top_k=10)` returns the top-K nearest LanceDB rows (`L2` distance — embedding models we ship are normalized).

**Opt-in install** via `pip install durin[memory]` (adds `fastembed` + `lancedb`). Default model `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (220 MB, 384-dim, multilingual). For CJK-heavy users `intfloat/multilingual-e5-large` (2.24 GB, 1024-dim); for minimal English-only `sentence-transformers/all-MiniLM-L6-v2` (90 MB, 384-dim). Set via `memory.embedding.model` override.

On first vector call the model auto-downloads into `~/.cache/fastembed/` and stays resident for the process lifetime — no idle eviction in V1 per the data-driven decision recorded in archived `docs/archive/08_memory_phase2_proposal.md` §0d.2.

`VectorIndexDimensionMismatch` raised when the on-disk index has a vector dim that disagrees with the current provider, or when the schema lacks the `entities` column — caller is told to `durin memory reindex`.

---

## 9. Retrieval — entity-aware ranking

```mermaid
flowchart TB
    Q["query string"] --> M["MemorySearchTool.execute"]
    M --> V["VectorIndex.search<br/>top-K=10"]
    V --> R{"alias index<br/>has data<br/>+ query mentions<br/>known entity?"}
    R -- yes --> RA["entity_ranker.rank_with_entities<br/>(RRF fusion)"]
    R -- no --> RB["pass-through<br/>(default ranking)"]
    RA --> O["Result rows<br/>strategy=vector,<br/>ranking=entity_aware"]
    RB --> O2["Result rows<br/>strategy=vector,<br/>ranking=default"]
    M --> T["telemetry:<br/>memory.recall.vector"]
```

`durin/agent/tools/memory_search.py`. Strategy by `(scope, level)`:

| scope | level | strategy |
|---|---|---|
| `dreamed` | `warm` | vector only |
| `all` | `warm` | vector (memory entries + entity pages) + grep (sessions + ingested) — hybrid |
| any other | any | grep only (fallback path) |

Vector failures silently fall back to grep so search never returns nothing because of an index issue.

### Entity-aware ranker (RRF)

`durin/memory/entity_ranker.py`. When the query mentions a known alias (`extract_query_entities` resolves via `AliasIndex`), `rank_with_entities` fuses two ranks via Reciprocal Rank Fusion (k=60):

- **Distance rank** — order by `_distance` ascending (closer = better).
- **Entity-match rank** — entity_page rows for the matched ref come first, then entries that reference the matched ref in their `entities` field.

Pre-cursor entries (timestamp ≤ page's `dream_processed_through`) get a small demote — they should already be absorbed in the page. Numeric cursors (legacy `msg_idx`) are NOT comparable to ISO timestamps and skip the demote (fail-open). G3 fix from archived doc 23.

RRF replaces an earlier score-multiplier design that mishandled the negative-distance regime (cluster B). Score normalization is not needed with rank-based fusion.

### Tool response shape

```json
{
  "results": [...],
  "total": 12,
  "strategy": "vector",
  "ranking": "entity_aware"
}
```

`strategy` and `ranking` are independent: downstream pattern-matches on `strategy=="vector"` don't break when the ranker activates.

### Telemetry

`memory.recall.vector` event payload:

| Field | Meaning |
|---|---|
| `query` | the query string |
| `scope` | `all` / `dreamed` / `undreamed` |
| `embedding_model` | resolved model name |
| `hit_count` | rows returned |
| `duration_ms` | wall-clock for the vector path |
| `ranking` | `default` or `entity_aware` |
| `query_entities_count` | how many refs resolved from the query |
| `reordered` | true iff top-1 changed pre vs post rerank |
| `top_1_id_before` / `top_1_id_after` | for offline tuning |

Aggregate `memory.recall` always fires too.

---

## 10. Drill-down and lifecycle commands

| Command | Purpose |
|---|---|
| `durin memory dream [entity] [--dry-run]` | Manually trigger consolidation. |
| `durin memory history <entity> [-n N]` | Chronological diff overview. |
| `durin memory show <entity> [--rev SHA]` | Page content at HEAD or a revision. |
| `durin memory diff <entity> <from>..<to>` | Unified diff between revisions. |
| `durin memory revert <commit>` | Reverse a consolidation (Phase 4 v1 prints guidance for `git revert`). |
| `durin memory expand <entity>` | Sources + related entities + archived absorptions in one view. |
| `durin memory absorb <canonical> <absorbed> [--reason …] [--yes]` | Merge two pages into one canonical, archive the absorbed, deindex from vector. |
| `durin memory absorb-suggest` | List candidate pairs that share at least one alias. |

Implementation: [durin/cli/memory_cmd.py](../../durin/cli/memory_cmd.py).

---

## 11. Absorption

```mermaid
flowchart LR
    A["canonical.md<br/>aliases: Marcelo, mm"] --> M["EntityAbsorption.absorb"]
    B["absorbed.md<br/>aliases: Marcelo, mmarmol"] --> M
    M --> C["canonical.md<br/>aliases: Marcelo, mm, mmarmol"]
    M --> D["canonical/archive/absorbed.md<br/>+ absorbed_into: ../../canonical.md"]
    M --> E["AliasIndex.remove(absorbed_ref)"]
    M --> F["VectorIndex.delete_by_id(absorbed_ref)"]
    M --> G["git commit: Absorb absorbed into canonical"]
```

[durin/memory/absorption.py](../../durin/memory/absorption.py). One git commit covers canonical update + absorbed deletion + archive create.

Idempotent — re-running when the absorbed page is already archived returns `None`. Auto-trigger post-dream is deferred to T2 (see [doc 25](../25_post_t1_state_and_t2_horizon.md) §2.D).

`find_candidates()` returns pairs that share at least one alias in the current `AliasIndex`. Stronger overlap (more shared aliases) sorts first. Surface via `durin memory absorb-suggest`.

---

## 12. Tool surface

| Tool | Module | Purpose |
|---|---|---|
| `memory_ingest` | `durin/agent/tools/memory_ingest.py` | Copy a markdown/text file to `ingested/<id>/` (content-hash idempotent) and return its content. |
| `memory_store` | `durin/agent/tools/memory_store.py` | Write a memory entry to `memory/<class>/<id>.md` with auto-headline + entity validation. Upserts into vector index. `author=agent_created` stamped via ContextVar. |
| `memory_search` | `durin/agent/tools/memory_search.py` | Vector + entity-aware rerank (when applicable) over dreamed + grep over undreamed. `read_only=True`. |
| `memory_drill` | `durin/agent/tools/memory_drill.py` | Resolve `path.md#anchor` to the addressed section. `read_only=True`. |

`memory_store` validates `entities` as `<type>:<value>` strictly (T1.1). Invalid refs are rejected at the tool boundary, not silently dropped.

---

## 13. Hooks into existing systems

- `SessionManager.save()` calls `regenerate_session_md(path)` after writing `.jsonl` so the navigable `.md` view (with stable `#turn-N` anchors) is always current.
- `SessionManager._DERIVED_METADATA_KEYS` includes `_last_tags`; per-session entity/topic tags emitted by the consolidator land in `<key>.meta.json::derived`.
- `Consolidator.archive()` returns `(summary, tags)` and `Consolidator._merge_session_tags` accumulates tags into `session.metadata["_last_tags"]` across compactions.
- `ContextBuilder._build_stable_layer` appends `read_hot_layer(workspace).render()` at the end of the stable prompt tier. Cache-friendly: the hot layer is read-only between dreams.

---

## 14. Telemetry events

| Event | Emitted by | Carries |
|---|---|---|
| `memory.ingest` | `memory_ingest` | `entry_id`, `size_bytes`, `suffix` |
| `memory.store` | `memory_store` | `entry_id`, `class_name`, `author`, `headline` |
| `memory.recall` | `memory_search` | `query`, `scope`, `level`, `result_count` |
| `memory.recall.vector` | `memory_search` (vector path) | see §9 |
| `memory.embedding.load` | first lazy load of the embedder | `model`, `duration_ms` |
| `memory.embedding.embed` | per batch | `model`, `batch_size`, `duration_ms` |

The schema-catalog meta-test in `tests/telemetry/test_schema_catalog.py` confirms emit sites and catalog stay in sync in both directions.

---

## 15. What we explicitly do NOT do (in T1)

Per doc 19 §14, deferred until evidence justifies:

- **Auto-trigger of dream** — manual `durin memory dream` only.
- **Identifier-based extraction in queries** — alias-only today; emails / Slack IDs / GitHub handles don't resolve.
- **Shared AliasIndex via ctx** — lazy per-tool today.
- **Auto-absorb post-dream** — manual CLI today.
- **L2+ retrieval** — graph traversal / cross-encoder / PageRank.
- **Sub-paging mega-hub** — single page per entity even when very large.
- **Obsidian-style viewer** — read with any markdown viewer.
- **User manual editing** — supported (markdown is markdown), but no UI to mediate.
- **Sync remoto** — local-only.

Roadmap for these items: [doc 25 §2](../25_post_t1_state_and_t2_horizon.md).

---

## 16. The "other" Dream (legacy MEMORY.md / SOUL.md)

`durin/agent/memory.py` carries a separate `Dream` class predating the entity-centric system. It processes `history.jsonl` and edits `MEMORY.md` / `SOUL.md` via an inner `AgentRunner` with read/edit tools. Cron-scheduled via `agent.dream.Dream` and registered as a system job by `cli/commands.py:1559`.

This is the legacy memory layer (per-workspace markdown files in the system prompt, not entity-centric). It coexists with the entity-centric system — they don't share state. Long-term plan is for entity-centric to subsume it, but no migration is in flight.

Don't confuse `durin.agent.memory.Dream` (legacy) with `durin.memory.dream.DreamConsolidator` (entity-centric).
