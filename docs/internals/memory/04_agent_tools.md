---
title: Agent tools — memory_search, memory_upsert_entity, memory_ingest, memory_drill, memory_forget
version: 0.1-draft
status: current — describes the shipped system (entity-centric era, 2026-06-06)
last_updated: 2026-06-06
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md, 01_data_and_entities.md, 02_indexing.md, 03_search_pipeline.md
related: 06_prompts_and_instructions.md
---

# Agent tools

This document specifies the tool API surface that the agent LLM sees. It defines each tool's parameters, return shape, tool description (the prompt the LLM reads to decide when to call), and result rendering — including the structural markers introduced in `03_search_pipeline.md` §12.

**Invariant:** these are the ONLY memory-related tools the agent sees. Anything beyond this is either internal to the pipeline (no LLM-facing surface) or a CLI / web surface for human operators. The agent never sees cross-encoder weights, RRF coefficients, index schemas, or LanceDB internals.

---

## 1. The five live tools

| Tool | Purpose | Hot/cold path |
|---|---|---|
| `memory_search` | Find relevant content across all memory layers | Hot |
| `memory_upsert_entity` | Author / update an entity page (the primary write tool) | Cold (write) |
| `memory_ingest` | Take a local document and store it whole as a reference | Cold (write, chunks the vector index) |
| `memory_drill` | Inspect a specific URI by reading its full body (entity page, episodic, etc.) | Hot (read-only file access) |
| `memory_forget` | Archive an entry + drop its index rows — the index-safe way to delete | Cold (write) |

All five are exposed as MCP/tool calls to the agent. None invokes an LLM internally on the hot path.

**Disabled (kept for reference):** `memory_store` is no longer in the live
toolset — `MemoryStoreTool.enabled()` returns `False`
(`durin/agent/tools/memory_store.py`). In the entity-centric model, facts about
a *thing* are written via `memory_upsert_entity` (§3) and documents via
`memory_ingest` (§4); interactions stay in the session for the dream to distil.
The `store_memory` *function* is retained for internal callers (compaction
summaries, etc.); the agent never sees the tool. Its spec is preserved in §3b
for reference and to keep the description in sync with the live class.

Tool discovery (`durin/agent/tools/loader.py`) calls `tool_cls.enabled(ctx)`
before registering, so a class returning `False` is simply skipped — that is the
mechanism that hides `memory_store` from the agent.

---

## 2. `memory_search`

### 2.1 Parameters

```json
{
  "query": "string (required)",
  "scope": "all | dreamed | undreamed | archive (default: all)",
  "level": "warm | cold (default: warm)",
  "keywords": "string | null (optional)",
  "limit": "integer (default: 10, min: 1, max: 50)",
  "kinds": "all | skill | fact (default: all)"
}
```

**Param semantics** (from `durin/agent/tools/memory_search.py::_PARAMETERS`):

| Param | Semantics |
|---|---|
| `query` | Natural-language query. Used for both vector embedding and FTS5 lexical search. |
| `scope` | `all` (default) = both undreamed sources and dreamed memory entries. `dreamed` = structured memory (entities + references + episodic + stable + session summaries). `undreamed` = raw sessions + raw ingested via grep fallback. `archive` = on-demand walk of `memory/archive/**` for recovery / diagnostic queries (audit F2). |
| `level` | `warm` returns headline+summary+metadata per hit. `cold` enriches each hit with full body (more tokens, more cost — used when the agent needs full content). |
| `keywords` | Optional literal-match string. When provided, the lexical RRF weight is boosted (§7.2 of doc 03) — useful when the agent wants exact match on emails, IDs, URLs, etc. |
| `limit` | Final result count. Clamped to `[1, 50]` (the tool re-clamps even though the schema declares the bounds — the LLM occasionally emits out-of-range values). The 50 cap prevents token blowup. |
| `kinds` | `all` (default) returns everything; `skill` returns only skill procedures; `fact` returns everything EXCEPT skills (facts, entity pages, references, sessions, ingested). Post-filter applied at the tool boundary. |

### 2.2 Return shape

Audit E16 (2026-05-28) rebuilt this section from the actual tool
output. Audit F5 (2026-05-28) corrected the example after audit F4
removed the per-row `rendered` field and added `sectioned_rendered`
at the top level — and fixed `valid_from` (entity pages always
write `""`; only entries have a real timestamp).

```json
{
  "results": [
    {
      "source": "memory",
      "uri": "memory/entity_page/person:marcelo",
      "headline": "Marcelo",
      "snippet": "Founder of durin…",
      "kind": "canonical",
      "summary": "Founder of durin…",
      "body": "…full markdown body (only when level=cold)…",
      "class_name": "entity_page",
      "entities": ["person:marcelo"]
    },
    {
      "source": "memory",
      "uri": "memory/episodic/abc123",
      "headline": "Marcelo prefers pytest",
      "snippet": "…",
      "kind": "fragment",
      "summary": "Marcelo prefers pytest as the default test runner.",
      "class_name": "episodic",
      "valid_from": "2026-05-23",
      "entities": ["person:marcelo", "topic:pytest"]
    }
  ],
  "total": 10,
  "strategy": "hybrid",
  "ranking": "entity_aware",
  "sectioned_rendered": "## Canonical\n\nConsolidated entity pages — the main memory; fragments below amend them with newer information.\n\n=== CANONICAL: person:marcelo (canonical entity page) ===\nFounder of durin…\n=== END CANONICAL ===\n\n## Fragment\n\nEpisodic and stable entries beyond the canonical cursor. Reconcile with the canonical above using the timestamps.\n\n=== FRAGMENT: memory/episodic/abc123 (ts 2026-05-23) ===\nMarcelo prefers pytest as the default test runner.\nEntities: person:marcelo, topic:pytest\n=== END FRAGMENT ==="
}
```

Per-result fields (defined on `durin.memory.search.Result`):

| Field | Always present | Description |
|---|---|---|
| `source` | yes | `memory | sessions | ingested` — which content layer the hit came from |
| `uri` | yes | The address the agent passes to `memory_drill` to fetch the full file |
| `headline` | yes | Title-ish summary for at-a-glance |
| `snippet` | yes | 200-char preview around the strongest match (or empty for entity pages) |
| `kind` | yes | `canonical | fragment | session | ingested` — the structural marker the LLM should treat the result as (§6 of this doc) |
| `summary` | when non-empty | Dream-generated summary (entity pages, some entries) |
| `body` | when `level=cold` | Full markdown body, read from disk after the pipeline returns |
| `class_name` | when non-empty | `entity_page | reference | episodic | stable | corpus | session_summary` (`reference` is the ingest output — §4; `corpus` only appears for legacy entries written before the references migration) |
| `valid_from` | when non-empty | ISO timestamp of when the entry's observation occurred. **Entity pages always have `""`** (file mtime tracks "last Dream pass", not "age of fact" — see doc 03 §10.4); only memory entries carry a real value. |
| `entities` | when non-empty | Entity URIs the hit pertains to (for entity pages: the page's own ref; for fragments: the entry's tags) |

Top-level fields:

| Field | Always present | Description |
|---|---|---|
| `total` | yes | Final result count (after limit) |
| `strategy` | yes | `vector | lexical | hybrid | grep` (main path) or `archive` (`scope='archive'` recovery surface) |
| `ranking` | yes | `default | entity_aware` — whether the entity-aware ranker contributed |
| `sectioned_rendered` | yes | Section-grouped marker output (audit F4, 2026-05-28). Carries section intros + per-block markers with `=== KIND: <uri> ===` headers and `=== END KIND ===` closes. Body inside each block follows `summary > body > snippet` preference; non-canonical blocks carry an `Entities: <ref>, <ref>` tail. **This is what the LLM consumes** — prefer it over reconstructing from raw fields. Per-row `rendered` was retired in F4. |
| `recovered_from` | **only on degraded runs** | List of source components that failed (e.g. `["vector"]`); omitted on clean runs |
| `recovery_duration_ms` | **only on degraded runs** | Wall-clock spent inside the failed wrappers |

`recovered_from` + `recovery_duration_ms` are not `null` in normal operation — they are simply absent from the dict. Same convention as the underlying `memory.recall` telemetry event (doc 07 §4.1).

### 2.3 Tool description (what the LLM sees)

The exact wording the LLM reads (the tool's `.description`, emitted as
`function.description`) is **not duplicated here** — that would be a third,
unguarded copy that silently drifts. The **canonical text lives in
[`06_prompts_and_instructions.md` §3.1](06_prompts_and_instructions.md)**,
and `tests/memory/test_tool_description_sync.py` enforces that
`memory_search.py`'s `.description` matches it verbatim (`.description`
delegates to `_PARAMETERS["description"]`; there is no `DESCRIPTION`
constant). Edit the spec in doc 06; the test forces the code to follow.

For reference, the description covers: a one-line purpose; single- vs
multi-call usage; the `keywords` literal-match hint; the exact-phrase
double-quote convention; the `level: "cold"` cost note; the `limit`
guidance (hard cap 50); the structural markers (SKILL / CANONICAL /
FRAGMENT / SESSION / INGESTED, defined in `03_search_pipeline.md` §12) with
their `(complete)` / `(preview N/M)` completeness qualifiers; recency
reasoning from marker timestamps; and the cite-your-source rule.

### 2.4 When to call (guidance baked into description)

The canonical description (doc 06 §3.1) embeds these patterns based on what worked in the LoCoMo v2 prompts (+3.9pp result):

- "Don't answer from cold recall." If you might need a fact, call.
- "Multi-query for compound questions." 2-3 calls with phrasings beat one long query.
- "Cite by uri in parentheses."

These are declarative facts, not imperatives ("USE BEFORE answering" was tested and is weak signal per `feedback_tool_description_weak_signal.md`). Verified pattern; LoCoMo v2 gained **+3.9pp overall** (60.8% → 64.7%) after adding these. Audit E17 (2026-05-28) corrected a stale "+12pp on single-hop" claim that this same paragraph used to carry — no per-category measurement at that magnitude exists in the verified bench data; the +3.9pp overall is the only number we can stand behind.

---

## 3. `memory_upsert_entity` — the write tool

`durin/agent/tools/memory_upsert_entity.py`. This is the **primary write tool
in the entity-centric model**: the agent authors a *thing* (person, company,
product, topic, place, …) as prose + structured edges; the dream extracts typed
attributes from that prose later. The entity page exists immediately via
`memory_writer.write_entity` (optimistic CAS — merge if it exists, create
otherwise). Dangling relations are allowed; dedup is deferred to the dream's
refine pass.

### 3.1 Parameters

```json
{
  "ref": "string (required, '<type>:<slug>')",
  "name": "string (optional, required when creating a new entity)",
  "aliases": "array of strings | null (optional)",
  "relations": "array of {to, type, ...} objects | null (optional)",
  "body": "string | null (optional, prose)"
}
```

**Param semantics** (from `memory_upsert_entity.py::_PARAMETERS`):

| Param | Required | Semantics |
|---|---|---|
| `ref` | ✓ | Entity reference `<type>:<slug>`, lowercase slug — e.g. `company:mxhero`, `person:marcelo`, `topic:smtp`. Must contain a `:` or the tool returns `{"error": ...}`. |
| `name` | — | Display name. Required *in practice* when creating a new entity (passed through to `write_entity(name=...)`). |
| `aliases` | — | Alternate names / identifiers for this entity. Each becomes an `alias` field-patch. |
| `relations` | — | Relations to other entities. Each item is an object with required `to` (`<type>:<slug>`) and `type` (relation kind, e.g. `partner`, `makes`, `works_at`); extra keys are allowed (`additional_properties=True`, e.g. `since`). Items missing `to`/`type` are skipped. |
| `derived_from` | — | Source documents this entity was distilled from — the `reference:<slug>` ref(s) returned by `memory_ingest`. Each becomes a `derived_from` field-patch (append + dedup, ref-keyed provenance). Items not starting with `reference:` are skipped. |
| `body` | — | Prose describing what you know about this entity. Applied via a `body_append` field-patch (default) or `body_replace` per `body_mode`. |
| `body_mode` | — | How to apply `body`: `append` (default) adds an attributed section without losing prior prose; `replace` overwrites the whole body. Enum `["append", "replace"]`. A `replace` over a user-authored body degrades to an `append` (precedence `user > dream > agent` on `provenance.body`). |

**Do NOT pass structured attributes.** The schema has no `attributes` param by
design — the dream extracts typed attributes from the prose `body`. The agent
supplies name + aliases + relations + body only.

**Provenance.** Each patch records a `source_ref` (the current session turn via
`memory_store._session_turn_ref()`, falling back to the literal
`"memory_upsert_entity"`) and is authored under `author_scope("agent_created")`.
Re-authoring an entity first clears any prior delete tombstone (§2.13 — the user
asked for it back).

### 3.2 Return shape

**Happy path** (page written):

```json
{
  "ref": "company:mxhero",
  "committed": true
}
```

`committed` is the CAS result from `write_entity` (whether the optimistic write
landed). The telemetry event `memory.upsert_entity` also records `retries`.

**Validation / write error**:

```json
{"error": "ref must be '<type>:<slug>' (e.g. company:mxhero)"}
```
```json
{"error": "upsert failed: <exception>"}
```

### 3.3 Tool description (what the LLM sees)

The exact wording is the tool's `.description` (which delegates to
`_PARAMETERS["description"]`). The **canonical text lives in
[`06_prompts_and_instructions.md` §3.5](06_prompts_and_instructions.md)** and is
sync-guarded by `tests/memory/test_tool_description_sync.py`. For reference, it
covers: authoring/updating an entity from `ref`/`name`/`aliases`/`relations`/
`body`; the merge-or-create semantics; the explicit "do NOT pass structured
attributes — the system extracts those from your prose" rule; the
`derived_from` linking hint (pass the `reference:<slug>` ref(s) from
`memory_ingest` for entities distilled from a document); and the routing hint
("use this for facts about a THING; use memory_ingest for documents").

---

## 3b. `memory_store` — DISABLED (kept for reference)

> **Not in the live toolset.** `MemoryStoreTool.enabled()` returns `False`
> (`durin/agent/tools/memory_store.py`), so the loader never registers it and the
> LLM never sees it (§1). The entity-centric model writes facts via
> `memory_upsert_entity` (§3) and documents via `memory_ingest` (§4);
> interactions stay in the session for the dream to distil. The `store_memory`
> *function* the tool wrapped is still used by internal callers (compaction
> summaries, etc.). The class and its description are retained — and kept in sync
> with doc 06 §3.2 — only so a future re-enable starts from a correct spec.

The parameters and return shape it *would* expose (still implemented on the
disabled class):

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

- `class_name` enum is `stable | episodic | corpus` (default `episodic`).
  `pending` and `session_summary` exist in `MEMORY_CLASSES` but are **excluded**
  from the agent-facing enum (`_AGENT_FACING_CLASSES`) — see decision 5b.
- The write path runs a vector dedup pre-check; near-duplicates (cosine ≥ 0.95 =
  LanceDB L2 ≈ 0.10) return a `{"warning": "near-duplicate", ...}` instead of
  persisting unless `force=true`.
- Happy-path return: `{"id", "class", "path", "headline", "author"}`. `id` is
  `sha256(class_name + "\0" + content)[:12]` (idempotent on repeat).

The canonical description is in [`06_prompts_and_instructions.md` §3.2](06_prompts_and_instructions.md), marked DISABLED there too.

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

- **Web content** → use `web_fetch(url=...)` (which already returns clean markdown via Jina/readability + SSRF protection) followed by `memory_ingest` on the saved file.
- **A fact about a *thing*** (a person, company, product, topic, …) → use `memory_upsert_entity` (§3) instead — `memory_ingest` is for whole documents, not individual facts.

**Storage model — references (audit A2).** `memory_ingest` no longer writes the
legacy chunked `memory/corpus/` entries. It now stores the document **whole as a
reference** and indexes it for all three retrieval mechanisms:

1. The verbatim file is copied to `ingested/<id>/` for preservation (grep-able
   via `memory_search(scope="undreamed")`).
2. The whole document is written to `memory/references/<slug>.md` (with a
   `type: reference` frontmatter block) and **FTS-indexed as one lexical unit**
   (`durin/memory/reference.py::ingest_reference` + `indexer.reindex_one_file`).
3. The document is split into **token-aware ≤512-token chunks**
   (`reference.py::chunk_by_tokens`, matching the e5-small embedder's
   `max_seq`), and each chunk is vector-indexed keyed `<ref>#<idx>`
   (`VectorIndex.upsert_reference_chunk`) so a fragment hit resolves to the
   parent reference.

When memory is disabled (no embedding model / `[memory]` extra absent), steps 2-3
are best-effort and may be skipped; the `ingested/` copy in step 1 still makes
the document grep-able.

### 4.2 Return shape

```json
{
  "id": "<12-char sha256[:12] of (filename + content)>",
  "reference": "reference:<slug>",
  "saved_to": "/abs/path/.../ingested/<id>/source.<ext>",
  "meta_path": "/abs/path/.../ingested/<id>/meta.json",
  "size_bytes": 12345,
  "content": "<full text of the ingested file>"
}
```

Notes:
- `saved_to` and `meta_path` are paths returned from [`ingestion.py`](../../../durin/memory/ingestion.py) (`result["source"]` / `result["meta_path"]`).
- `reference` is the `reference:<slug>` id of the stored reference. It is present **only when the reference write succeeded** — the reference write is best-effort and does not roll back the verbatim ingest if it fails (the key is omitted on failure or when memory is disabled).
- **Key order (C1):** `id` + `reference` are emitted **first**, before `content` (the whole doc). The agent result is head-truncated at 16 KB; placing the ref before the body keeps `reference:<slug>` readable on large documents so the entity-linking flow (`memory_upsert_entity(derived_from=[...])`) survives truncation.
- `id` is `sha256(filename + "\0" + content)[:12]` — re-ingesting the same file is idempotent, but renaming the file before re-ingest produces a different id (and therefore a duplicate entry under `ingested/`). If the user wants to "update" a previously-ingested file, the workflow is: re-ingest, then archive the old `ingested/<old-id>/` directory manually (or accept the duplicate; both versions live in git history).
- `content` is returned so the agent can read the file in the same turn (without a follow-up `memory_drill`).

### 4.3 Tool description

The canonical text lives in [`06_prompts_and_instructions.md` §3.3](06_prompts_and_instructions.md); the live `.description` delegates to `_PARAMETERS["description"]` and is sync-guarded. Reproduced here for reference:

```
Add a local document (markdown or plain text) to durin's memory as a
REFERENCE — coherent source material the user wants kept whole:
research notes, transcripts, technical specs, exported pages, markdown
books, etc.

`path` is the absolute or workspace-relative path to the file. The
original is preserved verbatim and the document is indexed for
retrieval. Re-ingesting the same file is idempotent — the id is a hash
of (filename + content). The result includes a `reference:<slug>`; when
you then author an entity distilled from this document, pass that ref in
`memory_upsert_entity(derived_from=[...])` so the entity links back to
its source.

For web content, use `web_fetch(url=...)` first to get clean markdown,
then `memory_ingest` on the saved file. For a fact about a *thing* (a
person, company, product, topic…), use `memory_upsert_entity` instead —
`memory_ingest` is for whole documents, not individual facts.
```

---

## 5. `memory_drill`

### 5.1 Parameters

```json
{
  "uri": "string (one of uri | uris required)",
  "uris": "array of strings (one of uri | uris required, max 10)"
}
```

`memory_drill` accepts **either** a single `uri` **or** a batch `uris`
(list, max 10) — the two are mutually exclusive (passing both errors).
The `uris` batch form merged the old standalone `memory_drill_batch`
tool into this one (H9 consolidation, 2026-05-29; verified
`durin/agent/tools/memory_drill.py:105-111`). The batch cap is
`MAX_BATCH_URIS = 10`.

### 5.2 Return shape

```json
{
  "uri": "person:marcelo",
  "content": "---\ntype: person\nname: Marcelo\n...\n---\n\n# Marcelo\n\nFounder of durin..."
}
```

`content` is the full markdown of the file. Audit E18 (2026-05-28) removed a `path` field from this spec — `memory_drill` does not echo the resolved path back to the agent (see `durin/agent/tools/memory_drill.py:148`, the single-`uri` return). The agent receives the URI it sent + the file contents. If the agent needs the resolved path for downstream tools, it can derive it from the URI.

**URI shapes drill accepts** (audit G6, 2026-05-28 — pre-G6 two of these were silently broken):

| URI | What it addresses |
|---|---|
| `memory/<class>/<id>` | Memory entries (`episodic`, `stable`, `corpus`, `reference`). `.md` suffix appended automatically. The grep path emits reference hits as `memory/reference/<slug>`, which resolves the same way. |
| `memory/entity_page/<type>:<slug>` | The canonical-shape URI `memory_search` emits for entity-page hits. G6 translates it to `memory/entities/<type>/<slug>.md` before reading. |
| `memory/entities/<type>/<slug>.md` | Direct on-disk path to an entity page — legacy form, still works. |
| `memory/archive/<class>/<id>.md` | Archived content surfaced by `memory_search(scope='archive')`. The archive path generates relative paths under `memory/archive/` post-G6 so the URIs are directly drillable. |
| `memory/archive/entities/<type>/<slug>.md` | Archived entity pages, same as above. |
| `sessions/<key>.md` (optionally `#turn-N`) | Session view, optionally a specific turn. |
| `ingested/<id>/source.md` (optionally `#anchor`) | Ingested document, optionally a markdown header section. |
| Any other workspace-relative path | Read as-is. Useful escape hatch when the agent has the on-disk path. |

### 5.3 Tool description

The live description is the single source of truth in
`durin/agent/tools/memory_drill.py::_PARAMETERS["description"]`. It
covers the `uri` (single) and `uris` (batch, max 10) surfaces plus the
`complete` / `preview N/M` body qualifiers. Rather than re-pasting it
here (it drifts), read the constant in the source. Audit F15
(2026-05-28) and the H9 consolidation (2026-05-29) both touched this
text; the read-only line and the "related context" hint live in that
same string.

---

## 5b. `memory_forget` — index-safe deletion

`durin/agent/tools/memory_forget.py`. A `core`-scope tool (always
enabled). Removes a memory entry the agent no longer wants surfaced.

- **Parameters:** `uri` (required, `memory/<class>/<id>` — exactly what
  `memory_search` returns) and optional `reason` (recorded in the archive
  frontmatter; defaults to `"agent_forget"`).
- **Return:** `{"uri", "archived_to", "status": "forgotten"}` on success;
  `{"error": ...}` on failure.
- **Behaviour:** delegates to the shared `durin.memory.forget.forget_entry`
  helper (also behind the `durin memory forget` CLI). Archives the entry
  to `memory/archive/<class>/<id>.md` (reversible) and drops its FTS +
  vector index rows so it stops appearing in search. Refuses
  `memory/entities/...` (entity pages have their own absorb/revert
  lifecycle).
- **Why it exists:** without an in-band deletion tool the agent's only
  recourse was a raw shell `rm`, which orphaned the FTS + vector rows
  (the auto-repair couldn't reconstruct them). The `exec` tool now also
  refuses mutations under `memory/` (`shell.py::_guard_memory_mutation`),
  so `memory_forget` is the sanctioned deletion path. Vector cleanup uses
  the model-free `vector_index.delete_ids` (`forget.py:108-112`), so
  forgetting never loads the embedding model.
- **Canonical description:** [`06_prompts_and_instructions.md` §3.6](06_prompts_and_instructions.md) (sync-guarded).

---

## 6. Result rendering — structural markers

Per `03_search_pipeline.md` §12, each hit is rendered under a section marker.
There is also a SKILL section that leads the output (a matching procedure is a
playbook to execute, ordered before the facts to reconcile); §6.1-§6.4 below
cover the four fact/document markers. The exact format:

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

### 6.2 FRAGMENT (recent episodic + stable)

```
=== FRAGMENT: <path> (ts <ISO_timestamp>) ===

<headline + summary OR full body if cold>

```

Example:
```
=== FRAGMENT: memory/episodic/2026-05-26T10-12-uuid.md (ts 2026-05-26T10:12:00Z) ===

Marcelo mentioned moving to Argentina next month for personal reasons.

```

FRAGMENT covers `episodic` + `stable` entries, surfaced **recency-ordered**.
There is **no per-entity cursor** (N3, 2026-06-06): the earlier design folded an
entity's fragments into its page and used a `dream_processed_through` cursor to
graduate consolidated fragments out. The redesign does not consolidate fragments
into pages — they are a separate raw track (`/remember` facts, session
summaries) that coexists with the canonical page — so nothing graduates a
fragment out; the recency cap bounds the section and the LLM reconciles a
fragment against the canonical page at read time using the timestamps (doc 03
§8). (Note: the live section-intro string still reads "beyond the canonical
cursor" — a behaviour-neutral string the N3 sweep left; the cursor itself is
gone.)

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

### 6.4 INGESTED (references + raw ingested grep hits)

```
=== INGESTED: <ingest_id>/<chunk_or_file> ===

<chunk text>

```

Example:
```
=== INGESTED: 2026-05-26-paper-arxiv-2602.12345/source.md ===

...cross-encoder reranking improves recall@10 by 12-18% over bi-encoder-
only retrieval at the cost of 50-100ms additional latency per query...

```

**Ingest now writes references, not the old corpus split (audit A2).** A
document is kept **whole** as `memory/references/<slug>.md` (FTS-indexed as one
lexical unit) plus **token-aware ≤512-token vector chunks** keyed `<ref>#<idx>`
(§4.1). The legacy 1500-char `memory/corpus/` chunk-per-entry model is gone — a
fragment hit on a chunk resolves to its parent reference rather than standing
alone. The INGESTED bucket also still surfaces raw grep hits over the verbatim
`ingested/` copy.

Rendering nuance: the sectioned renderer's INGESTED bucket keys off
`type == "corpus"` (`durin/memory/sectioned_output.py::_SECTION_FOR_TYPE`).
Reference hits carry `class_name="reference"`, which the tool's
`_TYPE_FROM_CLASS` map does not translate, so they currently render under
FRAGMENT, not INGESTED. The `corpus` → INGESTED path remains only for legacy
corpus entries written before the references migration.

### 6.5 Ordering and empty sections

- Sections appear in fixed order: SKILL → CANONICAL → FRAGMENT → SESSION → INGESTED (`durin/memory/sectioned_output.py::_SECTION_ORDER`).
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
| **Web dashboard** | Settings → Memory | Backed by `webui/src/components/settings/MemorySettings.tsx`: (a) cross-encoder enable toggle + free-form model id input with datalist of suggested ids + "Test" button that loads + scores the value live (audit B12, 2026-05-28 — no closed enum, any sentence-transformers compatible id works); (b) dream controls under `memory.dream.*` — `enabled`, `cron`, `post_compaction`, `on_session_close`, `auto_absorb.enabled`, `min_seconds_between_runs`, `max_seconds_per_run`; (c) read-only summary of `CLASS_HALF_LIFE_DEFAULTS` (`durin/memory/decay.py`). |

Other memory.search.* settings are config-file-only (advanced) and not surfaced in UI in MVP.

### 7.1 Read-only webui surfaces (informational)

The web dashboard also consumes three read-only endpoints exposed by the memory subsystem for visualization. These are NOT agent-facing tools (the agent never invokes them); they are HTTP APIs the webui calls directly. Detailed API shape lives in webui docs.

| Surface | Source code | Purpose |
|---|---|---|
| **`get_entity_detail(uri)`** | `durin/memory/graph_api.py` | Returns an entity page's full content + recent git history (default last 20 commits) for the dashboard sidebar. Also returns `provenance`: per-field events flattened from the page's `provenance` block (one per relation/attribute), each carrying `author` (agent/dream/user), `when`, and the `source_ref` split into `session_stem` + `turn` so the panel can link the fact back to the session turn that created it. The serialized page now also exposes `author`, `created_at`, `updated_at`, and `relations`. |
| **`get_edge_detail(from_uri, to_uri)`** | `durin/memory/graph_api.py` | Returns the co-mention evidence between two entities (which sessions/entries mention both) |
| **`search_memory_api(query, ...)`** | `durin/memory/graph_api.py` | Webui equivalent of `memory_search`. Same pipeline; different return shape (paginated, with stable IDs for UI rendering) |
| **Graph canvas data** | `durin/memory/graph.py::build_memory_graph` | Builds `{nodes: [...], edges: [...]}` for an Obsidian-style canvas view. Entity nodes come from consolidated pages under `entities/`; entity-tag harvesting and co-mention edges are walked across the entry classes that carry `entities` (`episodic`, `stable`, `corpus`) — `pending` and `session_summary` are excluded. Refs tagged on entries but lacking a page render as phantom nodes. A page-less **relation target** is only promoted to a phantom node when ≥2 distinct sources point at it (policy (a)); a degree-1 dangling relation is not drawn — it stays on disk in the source page's frontmatter but adds no graph structure. The phantom detail panel hides the `Body` and `History` tabs (structurally always empty without a page). Also includes session nodes. Caps at 500 nodes / 2000 edges to keep the canvas usable. |

Read-only by design — no mutation through these surfaces. Mutations flow through the agent tools (§2-§5b) or direct `.md` editing.

---

## 8. Tool description sync requirement

The **canonical tool-description text lives in
[`06_prompts_and_instructions.md` §3](06_prompts_and_instructions.md)**, not in
this doc — doc 04 references it (§2.3, §3.3, §4.3, §5.3) and must not contradict
it. Doc 06 §3 covers all the live tools — §3.1 `memory_search`,
§3.3 `memory_ingest`, §3.4 `memory_drill`, §3.5 `memory_upsert_entity`,
§3.6 `memory_forget` — plus §3.2 `memory_store` (marked DISABLED, kept in sync).

That canonical text must appear verbatim in:

- Each tool's `.description` property (e.g. `durin/agent/tools/memory_search.py::MemorySearchTool.description`). The property delegates to `_PARAMETERS["description"]` so both fields stay identical — `.description` is what `Tool.to_schema()` emits as `function.description` in the OpenAI function-calling spec, i.e. what the LLM actually reads.
- `durin/templates/agent/identity.md` Memory sections (where relevant — see doc 06 §2).
- Tool schemas exposed to MCP / OpenAI Tools format.

Sync is enforced by `tests/memory/test_tool_description_sync.py`. Audit N6
(2026-06-06) extended that test to the live write/delete tools
(`memory_upsert_entity`, `memory_forget`) — previously it guarded only the
disabled `memory_store`, so the live descriptions could drift undetected.
Updates flow from doc 06 outward; never the other way. Divergence is a bug.

Audit C9 + B1 (2026-05-28) corrected this section's earlier reference to `memory_*.py::DESCRIPTION` constants that never existed.

---

## 9. Module-level decisions

All decisions are consistent with cross-corpus decisions in `00_overview.md` and decisions in docs 01, 02, 03.

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| 1 | Live memory tools | **Five:** `memory_search`, `memory_upsert_entity`, `memory_ingest`, `memory_drill`, `memory_forget`. `memory_store` is disabled (kept for reference). Single search tool with internal routing — aligned with mainstream (mem0, hermes, openclaw, cognee). | §1 |
| 2 | `memory_search` parameter shape | `query` (required) + optional `scope` + `level` + `keywords` + `limit` + `kinds`. No `mode` / `type` enum — auto-routing happens internally per `03_search_pipeline.md`. | §2.1 |
| 3 | Result format — sectioned with markers | Section-grouped output in `sectioned_rendered` (the per-row `rendered` field was retired in F4); agents read `sectioned_rendered` directly. Markers are SKILL/CANONICAL/FRAGMENT/SESSION/INGESTED — descriptive only, no valuative language. | §2.2, §6 |
| 4 | Tool description style | Declarative, not imperative. Embeds patterns proven by LoCoMo v2 (+3.9pp): multi-query for compound questions, cite by URI, don't answer cold. | §2.3, §2.4 |
| 5 | Write surface = `memory_upsert_entity` | Facts about a *thing* go through `memory_upsert_entity` (agent authors name/aliases/relations/prose; the dream extracts attributes). `memory_store` was removed from the toolset (§8a — `enabled()=False`); the `store_memory` function stays for internal callers. | §3, §3b |
| 5b | Disabled `memory_store` enum excludes `pending` and `session_summary` | When it was live, the agent-facing enum (`_AGENT_FACING_CLASSES`) offered only `stable`/`episodic`/`corpus`. Reasons: (a) `pending` is the compaction intake buffer — walker, indexer, and file_watcher all skip `memory/pending/**`, so exposing it would let the LLM write entries the rest of the system silently ignores; (b) `session_summary` is produced by the compactor, not by the agent. The tool is now disabled outright, so the enum is moot for the live surface but retained on the class. | §3b, `durin/agent/tools/memory_store.py::_AGENT_FACING_CLASSES` |
| 6 | `memory_ingest` storage = references (A2) | The document is kept **whole** as `memory/references/<slug>.md` (FTS = one unit) + token-aware **≤512-token** vector chunks keyed `<ref>#<idx>` (`reference.py::chunk_by_tokens`). The legacy 1500-char `memory/corpus/` chunk-per-entry split is gone. Re-ingest is idempotent on `(filename, content)` — renaming the file before re-ingest yields a different id. | §4.1 |
| 6b | `memory_ingest` scope = local files only | URL fetch and inline content branches deliberately not supported. `web_fetch` already handles URLs (with Jina/readability, SSRF protection, image detection); a fact about a *thing* goes through `memory_upsert_entity`. Avoiding duplication of those policies. See `design_rationale.md` for full rationale. | §4.1, §10 |
| 7 | `memory_drill` purpose | Read full body of memory items by reference. Read-only. Takes `uri` (single) **or** `uris` (batch, max 10) — H9 consolidation (2026-05-29) merged the old `memory_drill_batch` tool in. For related context use `memory_search`. | §5 |
| 8 | Tool description as source of truth | The **canonical text is in doc 06 §3**; code and identity.md must match it. Doc 04 references it and must not contradict. | §8 |
| 9 | Configuration surface | Cross-encoder opt-in + dream cadence controls exposed in config file + onboarding wizard + web dashboard. Other settings config-file-only. | §7 |

### Open

None at the module level.

---

## 10. Implementation status (current vs target)

| Aspect | Current state | Notes |
|---|---|---|
| `memory_search` parameters | ✅ `query` (req) + `scope` + `level` + `keywords` + `limit` (cap 50) + `kinds` | `keywords` wired to RRF dynamic boost; `kinds` post-filters skill vs fact at the tool boundary |
| `memory_search` return | ✅ `results` (`to_dict()`) + `sectioned_rendered` + conditional `recovered_from` / `recovery_duration_ms` | Per-row `rendered` retired in F4; recovery fields omitted on clean runs |
| Result rendering | ✅ SKILL/CANONICAL/FRAGMENT/SESSION/INGESTED markers + per-source cap | `durin/memory/sectioned_output.py` |
| `memory_upsert_entity` (write tool) | ✅ Active (`ref` req + `name` + `aliases` + `relations` + `body`) | Entity-centric write path; dream extracts attributes from prose. Replaced `memory_store` as the agent write surface |
| `memory_store` | Disabled (`enabled()=False`) | Kept for reference (§3b); `store_memory` function retained for internal callers |
| `memory_ingest` | ✅ Active (`path` only); writes references | Whole doc → `memory/references/<slug>.md` + ≤512-token vector chunks (A2). Legacy `corpus/` split removed. URL/inline branches deliberately omitted (decision 6b) |
| `memory_drill` | ✅ Active (`uri` single **or** `uris` batch, max 10) | `uris` batch merged the old `memory_drill_batch` tool (H9, 2026-05-29) |
| `memory_forget` | ✅ Active (`uri` req + `reason`) | Index-safe deletion (§5b) |
| Tool descriptions | ✅ Canonical in doc 06 §3; sync'd to code + identity.md | Guarded by `test_tool_description_sync.py` (extended to live tools in N6) |
| Cross-encoder UI surface | ✅ Onboarding wizard + dashboard | `webui/src/components/settings/MemorySettings.tsx` |

---

## 11. Appendix — Operator CLI commands (informational)

The agent invokes the five live tools in §2-§5b. Separately, the **operator** has CLI commands for maintenance and inspection. These are NOT agent-facing; they are run from a terminal by the human running durin. Consolidated here so readers don't hunt for them across docs.

| Command | Purpose | Doc reference |
|---|---|---|
| `durin memory reindex [--target lancedb|fts|all]` | Wipe `.durin/index/` and rebuild from `.md` files | `02_indexing.md` §7.1, `09` Phase 2 |
| `durin memory dream [--entity <uri>]` | Manually trigger a Dream consolidation pass, optionally filtered to one entity | `05_dream_cold_path.md` §2 |
| `durin memory absorb [--auto|--interactive]` | Run absorb-judge over alias-overlap candidates and merge approved pairs | `05_dream_cold_path.md` §8 |
| **Not implemented**: `durin archive show <uri>` / `durin archive list` | Three existing surfaces cover archive recovery: `memory_search(scope='archive')` for agent-visible semantic recovery, `durin memory expand <entity>` for per-entity rendering, and `cat memory/archive/<class>/<id>.md` / `find memory/archive -name '*.md'` for direct shell access. A dedicated CLI command duplicates these without a unique use case. Decided against — see `design_rationale.md`. | `design_rationale.md` |
| `durin memory health [restore --component <name>]` | Inspect health-check cron state; manually retry restoration for a paused component | `03_search_pipeline.md` §14.4 |
| `durin memory history <uri> [--since <date>]` | Git log for an entity's `.md` file. Shows Dream consolidation history. | `00_overview.md` §10 #4 (versioning) |
| `durin memory forget <uri>` | Archive an individual memory entry + drop its index rows (`reason="user_forget"`). Shares the `forget_entry` helper with the agent's `memory_forget` tool (§5b) so CLI and tool stay index-consistent. | `durin/cli/memory_cmd.py::cmd_forget` |

Future commands (deferred — see `design_rationale.md`):

- `durin memory export ...` — structured dump
- `durin memory import ...` — load from another installation or competing system

Each CLI command emits the same telemetry events the in-process callers would (so audit logs are unified).

---

## 12. Cross-references

- Data classes and entity URIs: `01_data_and_entities.md`.
- Indexing details (LanceDB schema, FTS5 dual table, archive exclusion): `02_indexing.md`.
- Search pipeline (intent routing, RRF, recovery, sectioning): `03_search_pipeline.md`.
- Identity.md Memory section and onboarding wizard text: `06_prompts_and_instructions.md` (pending).
- Telemetry events from tool calls: `07_telemetry_and_observability.md` (pending).
