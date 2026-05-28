---
title: Agent tools — memory_search, memory_store, memory_ingest, memory_drill
version: 0.1-draft
status: under construction
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 01_data_and_entities.md, 02_indexing.md, 03_search_pipeline.md
related: 06_prompts_and_instructions.md
---

# Agent tools

This document specifies the tool API surface that the agent LLM sees. It defines each tool's parameters, return shape, tool description (the prompt the LLM reads to decide when to call), and result rendering — including the structural markers introduced in `03_search_pipeline.md` §12.

**Invariant:** these are the ONLY memory-related tools the agent sees. Anything beyond this is either internal to the pipeline (no LLM-facing surface) or a CLI / web surface for human operators. The agent never sees cross-encoder weights, RRF coefficients, decay half-lives, index schemas, or LanceDB internals.

---

## 1. The four tools

| Tool | Purpose | Hot/cold path |
|---|---|---|
| `memory_search` | Find relevant content across all memory layers | Hot |
| `memory_store` | Persist a short observation as episodic/stable | Cold (write) |
| `memory_ingest` | Take a long external source (URL, file path, raw text) and add it to corpus | Cold (write, may chunk) |
| `memory_drill` | Inspect a specific URI by reading its full body (entity page, episodic, etc.) | Hot (read-only file access) |

All four are exposed as MCP/tool calls to the agent. None invokes an LLM internally on the hot path.

---

## 2. `memory_search`

### 2.1 Parameters

```json
{
  "query": "string (required)",
  "keywords": "string | null (optional)",
  "scope": "dreamed | undreamed | all (default: all)",
  "level": "warm | cold (default: warm)",
  "limit": "integer (default: 10, max: 50)"
}
```

**Param semantics:**

| Param | Semantics |
|---|---|
| `query` | Natural-language query. Used for both vector embedding and FTS5 lexical search. |
| `keywords` | Optional literal-match string. When provided, the lexical RRF weight is boosted (§7.2 of doc 03) — useful when the agent wants exact match on emails, IDs, URLs, etc. |
| `scope` | `dreamed` = structured memory (entities + episodic + stable + corpus + session summaries). `undreamed` = raw sessions + raw ingested via grep fallback. `all` = both, with results from each appearing in the sectioned output. |
| `level` | `warm` returns headline+summary+metadata per hit. `cold` enriches each hit with full body (more tokens, more cost — used when the agent needs full content). |
| `limit` | Final result count. Cap at 50 to prevent token blowup. |

### 2.2 Return shape

```json
{
  "results": [
    {
      "uri": "person:marcelo",
      "type": "entity",
      "path": "memory/entities/person/marcelo.md",
      "score": 0.87,
      "headline": "Marcelo",
      "summary": "Founder of durin...",
      "body": "...full markdown body (only when level=cold)...",
      "valid_from": "2024-01-15",
      "rendered": "=== CANONICAL: person:marcelo (consolidated 2026-05-20) ===\n..."
    },
    ...
  ],
  "total": 10,
  "recovered_from": null,
  "recovery_duration_ms": null
}
```

`rendered` carries the section-marker block (§5 of this doc). Agents should prefer reading `rendered` over reconstructing the block from individual fields — the rendering is the source of truth for what the LLM should consume.

`recovered_from` and `recovery_duration_ms` (`null` in normal operation) communicate when the pipeline performed recovery work (§14 of doc 03). The agent can mention to the user when latency was caused by recovery.

### 2.3 Tool description (what the LLM sees)

```
Search durin's memory for content relevant to your question. Searches across
canonical entity pages, recent observations, session summaries, and ingested
documents in one call.

Usage:
- For most queries, use a single call with a natural-language `query`.
- For multi-part questions, issue 2-3 calls with different phrasings rather
  than one long query.
- For literal-match queries (emails, IDs, URLs), pass the literal string in
  `keywords` in addition to a natural-language `query`. This biases the search
  toward exact matches.
- Use `level: "cold"` only when you need full body content (verbose; consumes
  many tokens). `warm` (default) returns headline + summary, enough for most
  tasks.

Results come pre-sectioned with structural markers:
- `=== CANONICAL: <uri> ===` — consolidated entity pages (durable knowledge)
- `=== FRAGMENT: <path> ===` — recent observations not yet consolidated
- `=== SESSION: <id> ===` — conversation summaries
- `=== INGESTED: <id> ===` — chunks of documents the user has loaded

When sources disagree, more recent fragments may reflect updates that have
not yet been consolidated into the canonical entity page. Use timestamps in
the markers to reason about recency.

State the source of any fact you cite (uri or section marker) in parentheses.
Do not claim facts that are not in the search results.
```

This description is exact text from `templates/agent/identity.md::Memory` and `tools/memory_search.py::DESCRIPTION`. The two must stay in sync (see `06_prompts_and_instructions.md`).

### 2.4 When to call (guidance baked into description)

The description above embeds these patterns based on what worked in the LoCoMo v2 prompts (+3.9pp result):

- "Don't answer from cold recall." If you might need a fact, call.
- "Multi-query for compound questions." 2-3 calls with phrasings beat one long query.
- "Cite by uri in parentheses."

These are declarative facts, not imperatives ("USE BEFORE answering" was tested and is weak signal per `feedback_tool_description_weak_signal.md`). Verified pattern; LoCoMo v2 gained 12pp on single-hop after adding these.

---

## 3. `memory_store`

### 3.1 Parameters

```json
{
  "content": "string (required, full markdown body)",
  "class_name": "stable | episodic | corpus (default: episodic)",
  "headline": "string (optional, auto-generated from first ~10 words of content)",
  "summary": "string (optional)",
  "entities": "array of <type>:<value> strings (optional)",
  "source_refs": "array of markdown link strings (optional)",
  "force": "boolean (optional, default false)"
}
```

**Param semantics:**

| Param | Required | Semantics |
|---|---|---|
| `content` | ✓ | The full markdown body to remember. Persisted as the `body` field of the resulting `MemoryEntry` (the doc-04-vs-internal-schema naming asymmetry is deliberate — `content` is the action the LLM takes, `body` is the field on disk). |
| `class_name` | — | Enum: `stable`, `episodic`, `corpus`. Default `episodic`. `pending` exists in `MEMORY_CLASSES` but is **excluded** from the tool-facing enum — see decision 5b. |
| `headline` | — | Optional. When omitted, auto-generated as the first ~10 words of `content` ([store.py::_auto_headline](../../durin/memory/store.py)). |
| `summary` | — | Optional ~50-word summary, returned by `memory_search(level="warm")`. |
| `entities` | — | List of entity URIs. Format strict: `<type>:<slug>` (e.g., `person:marcelo`, `project:durin`). Drives entity-aware ranking. Open-vocabulary on type. |
| `source_refs` | — | Markdown links to originating turns / ingested doc sections (e.g., `[turn 42](../sessions/abc.md#turn-42)`). |
| `force` | — | Default `false`. The write path runs a dedup pre-check via vector similarity; near-duplicates (cosine ≥ 0.95 = LanceDB L2 ≈ 0.10) return a warning instead of persisting. Set `force=true` to bypass and intentionally re-affirm. Rare. |

**Not exposed as parameters** (but present in the persisted `MemoryEntry`):

- `valid_from` — defaults automatically to `date.today()` in [store.py::store_memory](../../durin/memory/store.py). Not exposed to the LLM because (a) the default covers the 99% case of "agent learned this just now", and (b) back-dating use cases go through `store_memory` directly (e.g. the LoCoMo bench seeds with conversation dates via the pure function, not the tool). See `08_scope_and_discarded.md` §2.9.
- `decay_half_life`, `evergreen` — Dream-managed; LLM does not set these directly.
- `related`, `author` — derived (`related` from heuristics; `author` from `provenance.current_author()` ContextVar).

### 3.2 Return shape

**Happy path** (entry persisted):

```json
{
  "id": "<12-char content hash>",
  "class": "episodic",
  "path": "memory/episodic/<id>.md",
  "headline": "<provided or auto-generated>",
  "author": "agent_created"
}
```

`id` is deterministic — `sha256(class_name + "\0" + content)[:12]` — so a repeated store of the same `(class_name, content)` writes to the same path (idempotent).

**Near-duplicate blocked** (cosine ≥ 0.95 to an existing entry, and `force` is `false`):

```json
{
  "warning": "near-duplicate",
  "nearest_id": "<existing id>",
  "nearest_headline": "<existing headline>",
  "nearest_distance": 0.07,
  "hint": "Content is nearly identical to an existing entry (cosine ≈ 0.95+). To store anyway, re-call with force=true."
}
```

**Validation error**:

```json
{"error": "<message>"}
```

### 3.3 Tool description

```
Persist an observation to memory. Use this when you learn a fact the user is
likely to need again — preferences, decisions, facts about people/projects/
tasks, etc.

Storage class (default: episodic):
- `episodic`: working memory; short atomic observation. Most uses.
- `stable`: durable, identity-level. Use sparingly — only when the user has
  explicitly said "remember this" or the fact is clearly identity-level.
- `corpus`: chunks of inline reference text. For files on disk use
  memory_ingest instead — it preserves the original artifact and handles
  chunking.

Always populate `entities` with the URIs this observation mentions (format:
`<type>:<value>`, e.g., `person:marcelo`, `project:durin`). This enables
entity-aware retrieval later.

Keep `headline` short and specific — it can be omitted and the system will
auto-generate one from the first ~10 words of `content`. `content` is the
full body of the observation; don't truncate.

If the user is restating something already known, do NOT call this tool — it
creates duplicates. The Dream consolidation process will eventually fold
duplicates but in the meantime they pollute results. A near-duplicate
(cosine ≥ 0.95 of an existing entry) returns a warning instead of persisting;
pass `force=true` only when you intentionally want to re-affirm an existing
fact.
```

---

## 4. `memory_ingest`

### 4.1 Parameters

```json
{
  "path": "string (required, absolute or workspace-relative file path)"
}
```

**Param semantics:**

| Param | Semantics |
|---|---|
| `path` | Local file path (absolute or workspace-relative). The tool only ingests files already on disk — markdown and plain-text formats. Binary or other formats raise an error. |

**Scope deliberately narrow.** URL fetch and inline content are NOT supported here; see §10 below for the rationale. The relevant workflows are:

- **Web content** → use `web_fetch(url=...)` (which already returns clean markdown via Jina/readability + SSRF protection) followed by `memory_store(content=..., class_name="corpus", source_refs=[url])`.
- **Inline text** (a paragraph or two the agent has in context) → call `memory_store(content=..., class_name="corpus")` directly.

The chunking pipeline (`durin/memory/text_splitter.py::split_text`, P5.3) runs inside `memory_ingest` only; if you need chunking on inline content, call `split_text` first and emit one `memory_store` per chunk.

### 4.2 Return shape

```json
{
  "id": "<12-char sha256[:12] of (filename + content)>",
  "saved_to": "/abs/path/.../ingested/<id>/source.<ext>",
  "meta_path": "/abs/path/.../ingested/<id>/meta.json",
  "size_bytes": 12345,
  "content": "<full text of the ingested file>",
  "corpus_entry_id": "<id of the first chunked memory/corpus entry>"
}
```

Notes:
- `saved_to` and `meta_path` are **absolute** paths (the tool returns `str(target)` from [`ingestion.py`](../../durin/memory/ingestion.py)).
- `id` is `sha256(filename + "\0" + content)[:12]` — re-ingesting the same file is idempotent, but renaming the file before re-ingest produces a different id (and therefore a duplicate entry under `ingested/`). If the user wants to "update" a previously-ingested file, the workflow is: re-ingest, then archive the old `ingested/<old-id>/` directory manually (or accept the duplicate; both versions live in git history).
- `content` is returned so the agent can read the file in the same turn (without a follow-up `memory_drill`).
- `corpus_entry_id` is the first chunk's memory entry id; subsequent chunks live under `memory/corpus/` with headlines annotated `(chunk N/M)` and are findable via `memory_search`.

### 4.3 Tool description

```
Add a local document (markdown or plain text) to durin's memory corpus.
Use this when the user wants a file on disk remembered as reference
material — research notes, transcripts, technical specs, exported pages,
markdown books, etc.

`path` is the absolute or workspace-relative path to the file. The file
is copied to `ingested/<id>/` for preservation (so the original is
recoverable verbatim) and the content is chunked into searchable
`memory/corpus/*.md` entries. Re-ingesting the same file is idempotent
— the id is derived from a content hash.

For web content, use `web_fetch(url=...)` first to get clean markdown,
then `memory_store(content=..., class_name="corpus", source_refs=[url])`.
`web_fetch` already handles URL extraction (Jina/readability),
SSRF protection, redirects, and image detection.

For short inline text (a paragraph or two), call `memory_store` directly
with `class_name="corpus"` — `memory_ingest` is specifically for files
on disk where preserving the original artifact matters.
```

---

## 5. `memory_drill`

### 5.1 Parameters

```json
{
  "uri": "string (required)"
}
```

### 5.2 Return shape

```json
{
  "uri": "person:marcelo",
  "path": "memory/entities/person/marcelo.md",
  "content": "---\ntype: person\nname: Marcelo\n...\n---\n\n# Marcelo\n\nFounder of durin..."
}
```

`content` is the full markdown of the file.

### 5.3 Tool description

```
Read the full content of a memory item by URI. Use this when memory_search
returned a hit and you need to see the full body, including any structured
data in the frontmatter.

For related context (recent post-cursor observations mentioning this URI),
use memory_search instead — its sectioned output already groups canonical
+ fragments.

This tool is read-only. It does not modify state.
```

---

## 6. Result rendering — structural markers

Per `03_search_pipeline.md` §12, each hit is rendered with one of four markers. The exact format:

### 6.1 CANONICAL (entity pages)

```
=== CANONICAL: <uri> (consolidated <ISO_timestamp>) ===

<rendered frontmatter + body, up to ~500 chars summary OR full body if cold>

```

Example:
```
=== CANONICAL: person:marcelo (consolidated 2026-05-20T14:23:00Z) ===

Marcelo (also: Marcelo Marmol, 马塞洛). Email: marcelo@mxhero.com.
Current residence: Spain. Maintains durin (since 2024-01).

```

### 6.2 FRAGMENT (post-cursor episodic + stable)

```
=== FRAGMENT: <path> (ts <ISO_timestamp>) ===

<headline + summary OR full body if cold>

```

Example:
```
=== FRAGMENT: memory/episodic/2026-05-26T10-12-uuid.md (ts 2026-05-26T10:12:00Z) ===

Marcelo mentioned moving to Argentina next month for personal reasons.

```

### 6.3 SESSION (session summaries + raw session hits)

```
=== SESSION: <session_id>/<summary_or_turn> (ts <ISO_timestamp>) ===

<summary text OR turn excerpt if raw grep hit>

```

Example:
```
=== SESSION: c155274d/summary (ts 2026-05-25T19:00:00Z) ===

Discussed memory architecture redesign. Decided to drop closed catalogs in
favor of free attributes + relations. Discarded G3.b query rewriter.

```

### 6.4 INGESTED (corpus chunks + raw ingested grep hits)

```
=== INGESTED: <ingest_id>/<chunk_or_file> ===

<chunk text>

```

Example:
```
=== INGESTED: 2026-05-26-paper-arxiv-2602.12345/chunk-3 ===

...cross-encoder reranking improves recall@10 by 12-18% over bi-encoder-
only retrieval at the cost of 50-100ms additional latency per query...

```

### 6.5 Ordering and empty sections

- Sections appear in fixed order: CANONICAL → FRAGMENT → SESSION → INGESTED.
- Within a section, hits ordered by score descending.
- **Empty sections are omitted entirely.** No empty `=== CANONICAL: (none) ===` headers.
- Per-source cap (§12.4 of doc 03): if more than 3 corpus chunks share an `ingest_id`, only the top 3 are rendered.

### 6.6 No valuative language in markers

Markers carry **only descriptive metadata** (uri, timestamp, path). They do NOT carry instructions like `(treat as authoritative)` or `(this is the source of truth)`. The LLM infers reliability from the marker type and timestamp — that is structural communication, not imperative instruction. Per `feedback_tool_description_weak_signal.md`.

---

## 7. Configuration surface (recap from doc 03)

The cross-encoder reranker (opt-in, OFF by default) is configurable through three surfaces, all backed by the same workspace config file:

| Surface | Location | What it exposes |
|---|---|---|
| **Config file** | `~/.durin/config.json` `memory.search.cross_encoder.{enabled, model, batch_size}` | Power users edit directly |
| **Onboarding wizard** | CLI prompt during `durin init` | Yes/No question with latency + RAM trade-off explanation |
| **Web dashboard** | Settings → Memory (P4.4 + B12) | Three controls: (a) cross-encoder enable toggle + free-form model id input with datalist of suggested ids + "Test" button that loads + scores the value live (audit B12, 2026-05-28 — no closed enum, any sentence-transformers compatible id works); (b) consolidation threshold count for `memory.dream.threshold_entries`; (c) read-only summary of `CLASS_HALF_LIFE_DEFAULTS`. Backed by `webui/src/components/settings/MemorySettings.tsx`. |

Other memory.search.* settings are config-file-only (advanced) and not surfaced in UI in MVP.

### 7.1 Read-only webui surfaces (informational)

The web dashboard also consumes three read-only endpoints exposed by the memory subsystem for visualization. These are NOT agent-facing tools (the agent never invokes them); they are HTTP APIs the webui calls directly. Detailed API shape lives in webui docs.

| Surface | Source code | Purpose |
|---|---|---|
| **`get_entity_detail(uri)`** | `durin/memory/graph_api.py` | Returns an entity page's full content + recent history (default last 20 commits) for the dashboard sidebar |
| **`get_edge_detail(from_uri, to_uri)`** | `durin/memory/graph_api.py` | Returns the co-mention evidence between two entities (which sessions/entries mention both) |
| **`search_memory_api(query, ...)`** | `durin/memory/graph_api.py` | Webui equivalent of `memory_search`. Same pipeline; different return shape (paginated, with stable IDs for UI rendering) |
| **Graph canvas data** | `durin/memory/graph.py::GraphBuilder` | Builds `{nodes: [...], edges: [...]}` for an Obsidian-style canvas view. Includes session nodes + entity nodes; edges from co-mention counts. Caps at 500 nodes / 2000 edges to keep the canvas usable. |

Read-only by design — no mutation through these surfaces. Mutations flow through the agent tools (§2-§5) or direct `.md` editing.

---

## 8. Tool description sync requirement

The tool descriptions in §2.3, §3.3, §4.3, §5.3 are the **canonical text**. They must appear verbatim in:

- Each tool's `.description` property (e.g. `durin/agent/tools/memory_search.py::MemorySearchTool.description`). The property delegates to `_PARAMETERS["description"]` so both fields stay identical — `.description` is what `Tool.to_schema()` emits as `function.description` in the OpenAI function-calling spec, i.e. what the LLM actually reads.
- `durin/templates/agent/identity.md::Memory` section (where relevant).
- Tool schemas exposed to MCP / OpenAI Tools format.

Any divergence between code, identity.md, and this document is a bug. The text is decided here; code reflects it.

Audit C9 + B1 (2026-05-28) corrected this section's earlier reference to `memory_*.py::DESCRIPTION` constants that never existed.

---

## 9. Module-level decisions

All decisions are consistent with cross-corpus decisions in `00_overview.md` and decisions in docs 01, 02, 03.

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| 1 | Number of memory tools | **Four:** `memory_search`, `memory_store`, `memory_ingest`, `memory_drill`. Single search tool with internal routing — aligned with mainstream (mem0, hermes, openclaw, cognee). | §1 |
| 2 | `memory_search` parameter shape | `query` (required) + optional `keywords` + `scope` + `level` + `limit`. No `mode` / `type` enum — auto-routing happens internally per `03_search_pipeline.md`. | §2.1 |
| 3 | Result format — sectioned with markers | Pre-rendered per hit in a `rendered` field; agents read `rendered` directly. Markers are CANONICAL/FRAGMENT/SESSION/INGESTED — descriptive only, no valuative language. | §2.2, §6 |
| 4 | Tool description style | Declarative, not imperative. Embeds patterns proven by LoCoMo v2 (+3.9pp): multi-query for compound questions, cite by URI, don't answer cold. | §2.3, §2.4 |
| 5 | `memory_store` class default | `episodic`. `stable` is reserved for explicit-durability cases. Description warns against creating duplicates. | §3.1, §3.3 |
| 5b | `memory_store` enum excludes `pending` | `MEMORY_CLASSES` has 4 values (`stable`, `episodic`, `corpus`, `pending`) but the agent-facing enum offers only the first 3. Reason: the walker / indexer / file_watcher all skip `memory/pending/**` (intake buffer for compaction). Exposing `pending` to the LLM would let it write entries the rest of the system silently ignores. Internal callers that legitimately need to write to `pending` use the pure `store_memory` function. | §3.1, `durin/agent/tools/memory_store.py::_AGENT_FACING_CLASSES` |
| 6 | `memory_ingest` chunking | Always on (1500-char chunks with 200-char overlap, recursive paragraph→line→sentence→word split per P5.3). Re-ingest is idempotent on `(filename, content)` — renaming the file before re-ingest yields a different id. Docs shorter than `chunk_size` collapse to one entry naturally. | §4.1 |
| 6b | `memory_ingest` scope = local files only | URL fetch and inline content branches deliberately not supported. `web_fetch` already handles URLs (with Jina/readability, SSRF protection, image detection); `memory_store(class_name="corpus")` handles inline text. Avoiding duplication of those policies. See `08_scope_and_discarded.md` for full rationale. | §4.1, §10 |
| 7 | `memory_drill` purpose | Read full body of a single URI by reference. Read-only. Single `uri` parameter — for related context use `memory_search`. | §5 |
| 8 | Tool description as source of truth | The text in this doc is canonical; code and identity.md must match. | §8 |
| 9 | Configuration surface | Cross-encoder opt-in exposed in config file + onboarding wizard + web dashboard. Other settings config-file-only. | §7 |

### Open

None at the module level.

---

## 10. Implementation status (current vs target)

| Aspect | Current state | v2 target | Migration work |
|---|---|---|---|
| `memory_search` parameters | `query`, `scope`, `level` | + `keywords` optional + cap `limit` at 50 | Schema update; pipeline wires `keywords` to RRF dynamic boost |
| `memory_search` return | `results` with `to_dict()` + `rendered` | Same shape + `recovered_from` + `recovery_duration_ms` fields | Wire recovery info from pipeline |
| Result rendering | CANONICAL/FRAGMENT markers exist | Extend to SESSION + INGESTED; per-source cap | Update renderer; integrate cap |
| `memory_store` parameters | `content` (req), `class_name` (enum 3 values), `headline`, `summary`, `source_refs`, `entities`, `force` | Same | None — earlier doc-04 draft proposed `body` (vs `content`), `headline` required, `valid_from` exposed, and a 2-value enum; reconciled in audit A2 (see doc 11) |
| `memory_ingest` parameters | Active (`path` only) | Same (`path` only) | None — earlier spec proposed `source`/URL/`inline` branches; removed because `web_fetch` + `memory_store(class_name="corpus")` already cover those workflows (see decision 6b + doc 08) |
| `memory_drill` | Active (single `uri` param) | Same | None — earlier draft proposed an `include_context` flag; removed because `memory_search` already covers that need with sectioned output |
| Tool descriptions | In code, partially in identity.md | Canonical in this doc; sync to code + identity.md | Audit and reconcile |
| Cross-encoder UI surface | Not implemented | Onboarding wizard + dashboard | New onboarding step; new webui setting |

---

## 11. Appendix — Operator CLI commands (informational)

The agent invokes the four tools in §2-§5. Separately, the **operator** has CLI commands for maintenance and inspection. These are NOT agent-facing; they are run from a terminal by the human running durin. Consolidated here so readers don't hunt for them across docs.

| Command | Purpose | Doc reference |
|---|---|---|
| `durin reindex [--target lancedb|fts5|all]` | Wipe `.durin/index/` and rebuild from `.md` files | `02_indexing.md` §7.1, `09` Phase 2 |
| `durin embed-migrate --to <model_id>` | Switch embedding model with safe migration (backup + rebuild) | `02_indexing.md` §7.2.1 |
| `durin dream run [--entity <uri>]` | Manually trigger a Dream consolidation pass, optionally filtered to one entity | `05_dream_cold_path.md` §2 |
| `durin memory absorb [--auto|--interactive]` | Run absorb-judge over alias-overlap candidates and merge approved pairs | `05_dream_cold_path.md` §8 |
| `durin archive show <uri>` | Read an archived entry by URI (recovery / audit) | `01_data_and_entities.md` §3.6 |
| `durin archive list` | List archived entries (walks `memory/archive/` on demand) | `01_data_and_entities.md` §3.6 |
| `durin memory health [restore --component <name>]` | Inspect health-check cron state; manually retry restoration for a paused component | `03_search_pipeline.md` §14.4 |
| `durin memory history <uri> [--since <date>]` | Git log for an entity's `.md` file. Shows Dream consolidation history. | `00_overview.md` §10 #4 (versioning) |

Future commands (deferred — see `08_scope_and_discarded.md` §5 backlog):

- `durin memory export ...` — structured dump
- `durin memory import ...` — load from another installation or competing system
- `durin memory forget <uri>` — GDPR-style cascading delete

Each CLI command emits the same telemetry events the in-process callers would (so audit logs are unified).

---

## 12. Cross-references

- Data classes and entity URIs: `01_data_and_entities.md`.
- Indexing details (LanceDB schema, FTS5 dual table, archive exclusion): `02_indexing.md`.
- Search pipeline (intent routing, RRF, recovery, sectioning): `03_search_pipeline.md`.
- Identity.md Memory section and onboarding wizard text: `06_prompts_and_instructions.md` (pending).
- Telemetry events from tool calls: `07_telemetry_and_observability.md` (pending).
