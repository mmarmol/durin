---
title: Data types and entity model
version: 0.1-draft
status: under construction
last_updated: 2026-05-27
audience: humans and LLMs implementing or modifying this system
depends_on: 00_overview.md
related: 02_indexing.md, 05_dream_cold_path.md
---

# Data types and entity model

This document specifies, in implementable detail, what data the memory system stores, where it lives, what shape each piece has, and how it transitions through its lifecycle. It is the foundation for every other module in this corpus.

**Reading convention:** sections marked `[CURRENT]` describe what exists in code today (verified). Sections marked `[V2]` describe the target state proposed in this corpus. Where the two differ, both are shown so the migration is explicit.

---

## 1. Storage layout

All memory artifacts live inside a single per-installation workspace (default: `~/.durin/workspace/`). Each workspace is self-contained: copying the workspace folder copies the agent's memory.

**Multi-user note.** A workspace is shared across all users who interact with the agent through any channel (Telegram, Discord, Slack, web, CLI, etc.). There is **no per-user isolation of memory**. Each interacting user — including the installation owner — is modeled as a `person:<name>` entity, indistinguishable from other people the agent has met. Sessions in `sessions/` carry the originating user's identity (`effective_user` / `user_id` per channel), but the entity graph treats all participants as first-class.

**Cross-channel identity (current best-effort).** The same person interacting via Telegram, Slack, and a direct CLI conversation typically arrives with three different channel-level user IDs (Telegram user_id, Slack user_id, hostname). By default, the agent creates separate `person:<name>` entities per the names it observes (e.g., `person:marcelo_telegram` and `person:marcelo_slack` if it never gets told they are the same). The reconciliation flow is **manual + LLM-assisted**: when the agent or user notices the duplicate, `durin memory absorb` runs the absorb-judge LLM over the pair; if confidence is high, they merge (per §8 of `05_dream_cold_path.md`). Until merged, they exist as distinct entities. This is the trade-off documented under **R4** in `08_scope_and_discarded.md` §3 — no universal cross-system identity solver exists; manual reconciliation is accepted as the limit of the current design.

```
<workspace>/
├── sessions/                           Raw conversation transcripts
│   └── <session_id>/
│       ├── <session_id>.jsonl          Event stream (turns, tool calls, results)
│       ├── <session_id>.meta.json      Title, timestamps, derived._last_summary
│       └── ...
│
├── ingested/                           Raw external documents
│   └── <ingest_id>/
│       ├── source.{pdf,html,txt,...}   Original file
│       └── metadata.json               Origin, ingest_time, mime_type
│
└── memory/                             All structured memory
    ├── corpus/<id>.md                  Chunks from ingested + agent snapshots
    ├── episodic/<id>.md                Short atomic observations
    ├── stable/<id>.md                  Stable, durable notes
    ├── pending/<id>.md                 Intake buffer (pre-classification)
    ├── archive/                        [V2] Consolidated artifacts kept for recovery.
    │   └── episodic/<id>.md             Excluded from ALL default search paths
    │                                    (vector, lexical, grep, walk+parse).
    │                                    Reachable only via explicit opt-in.
    └── entities/
        └── <type>/<slug>.md            Typed canonical synthesis
```

Reasoning for split between `sessions/`, `ingested/`, and `memory/`:

- `sessions/` and `ingested/` are **immutable evidence**. The agent never modifies them. They are append-only or write-once.
- `memory/` is **mutable synthesis**. Created and modified by the agent, by Dream, or by the user.

This split lets the indexers, Dream, and archival routines operate on `memory/` without worrying about evidence integrity.

---

## 2. Data classes

The system has **nine** data classes. Audit E25 (2026-05-28) added
`session_summary` to this table — A10 (2026-05-28) promoted session
summaries from JSON sidecars into a first-class memory class at
`memory/session_summary/`.

| Class | Path | Mutability | Created by | Indexed for search? | Consumed by Dream? |
|---|---|---|---|---|---|
| **Session** | `sessions/<id>/` | Append-only during session, then read-only | AgentLoop (automatic, scoped per interlocutor unless `unified_session=true`) | Full text grep | No (referenced only as source_refs) |
| **Session summary** | `memory/session_summary/<sanitized_key>.md` | Replaced when consolidator re-summarises | Consolidator (`_persist_last_summary`, audit A10) | Vector + lexical (class `session_summary`) | No — used as retrieval context, not consumed as raw input |
| **Ingested** | `ingested/<id>/` | Immutable | User (UI) or agent (tool) | Not directly; chunks via `corpus/` | No (chunks → corpus is what gets read) |
| **Corpus** | `memory/corpus/<id>.md` | Replaced on re-ingest | Ingestion pipeline, agent | Vector + lexical | **Counts as signal** (threshold trigger §2.2 doc 05) but NOT consumed into entity pages — ingested docs are already canonical-ish |
| **Episodic** | `memory/episodic/<id>.md` | Append-only typically | Agent via `memory_store` | Vector + lexical | **Yes, primary input.** Post-cursor entries are consumed, applied as PATCH ops, then archived |
| **Stable** | `memory/stable/<id>.md` | Semi-mutable (editable) | Agent, user | Vector + lexical | Referenced as context but **never consumed or archived** by Dream (user-marked durable) |
| **Pending** | `memory/pending/<id>.md` | Short-lived | Intake pipeline | Not indexed | No (intermediate buffer) |
| **Entity** | `memory/entities/<type>/<slug>.md` | Mutable (Dream + user) | Dream consolidator, user | Vector + lexical | **Yes, target.** Dream's PATCH ops write to this class. |
| **Archive** | `memory/archive/<class>/<id>.md` | Frozen | Dream after consolidation | **Excluded from all search paths by default** (vector index, FTS5/BM25, raw grep, walk+parse). Reachable only via explicit recovery flag. | No (terminal state) |

### 2.1 Why each class exists

- **Session**: full conversation history. Source of truth for "what was said". The agent must never lose this — it's the audit trail.
- **Ingested**: external documents brought in by the user. Source of truth for "what was given". Used as evidence when citing.
- **Corpus**: searchable chunks derived from `ingested/`. The retrieval unit for long documents.
- **Episodic**: atomic observations extracted by the agent. Raw material for Dream.
- **Stable**: facts the agent or user has explicitly marked as durable. Like episodic but "promoted" — has more weight.
- **Pending**: transitional buffer. Items waiting to be classified or processed. Operationally important; not user-visible.
- **Entity**: synthesized canonical knowledge. The graph of "what we know" about people, projects, bugs, deals, etc.
- **Archive** [V2]: episodic (and possibly others) that Dream has consolidated into entities. Kept for recoverability; excluded from default search.

---

## 3. Schema specifications

### 3.1 Session — current

Path: `sessions/<session_id>/<session_id>.jsonl` + `<session_id>.meta.json`

The `.jsonl` is a stream of events (one JSON object per line). The `.meta.json` carries metadata.

`meta.json` essential fields:

```json
{
  "title": "...",
  "created_at": "ISO timestamp",
  "updated_at": "ISO timestamp",
  "derived": {
    "_last_summary": {
      "text": "rolling summary generated by compaction",
      "updated_at": "ISO timestamp",
      "version": <int>
    },
    "_last_tags": ["tag1", "tag2"]
  }
}
```

Sessions are **not parsed as markdown** by the memory system. They are searched via raw grep over `.jsonl` files. In v2, the `_last_summary.text` is vectorized into LanceDB with `uri=session:<id>` (see §6 in `02_indexing.md` once written).

**Session turn anchors are stable.** When `session_md.py` renders a `.jsonl` to a deterministic markdown view, each turn gets an immutable `## turn-N` anchor. Numbering NEVER changes despite later consolidation or summary updates — `source_refs` like `session:<id>/turn-42` always point at the same content. This stability is the contract that makes provenance references durable across time.

### 3.2 Ingested — current

Path: `ingested/<ingest_id>/`

Contains:
- `source.<ext>`: the original file (PDF, HTML, TXT, etc.).
- `metadata.json`: origin URL, ingest timestamp, mime type, user-provided tags.
- Optional intermediate artifacts (extracted text, OCR results).

Ingested itself is not indexed. Its content becomes searchable via `memory/corpus/` (see §3.4).

### 3.3 Memory entries — episodic, stable, corpus, pending

All four classes share the same schema (`durin/memory/schema.py::MemoryEntry`):

```yaml
---
id: <string, unique per class>
headline: <string, one-line title>
summary: <string, short paragraph, optional>
source_refs: [<string>, ...]      # references to sessions/ingested or other entries
related: [<string>, ...]          # related entry IDs
entities: [<entity_uri>, ...]     # e.g. ["person:marcelo", "project:durin"]
author: "user_authored" | "agent_created"   # see §4.6 for protection rule
valid_from: <YYYY-MM-DD>          # optional, for time-bound facts
---

<markdown body — free form>
```

**Constraints (enforced by `MemoryEntry` Pydantic model):**

- `id` is required.
- `headline` is required, single line.
- `entities` must match `<type>:<value>` where `type` is lowercase `[a-z][a-z0-9_]*`. The vocabulary of types is **open**; only the shape is enforced.
- `extra="forbid"` — no unknown top-level fields allowed.
- `model_config = ConfigDict(str_strip_whitespace=False)` — preserve whitespace in strings (CJK-safe).

### 3.4 Entity page — current [CURRENT]

Path: `memory/entities/<type>/<slug>.md`

Minimum required frontmatter (`durin/memory/entity_page.py::EntityPage`):

```yaml
---
type: <lowercase, [a-z][a-z0-9_]*>
name: <display name>
aliases: [<list of strings>]
dream_processed_through: <timestamp or null>     # cursor
created_at: <ISO timestamp>
updated_at: <ISO timestamp>
---

<markdown body — free form>
```

Known top-level fields (parsed explicitly): `type`, `name`, `aliases`, `dream_processed_through`, `created_at`, `updated_at`, `attributes`, `relations`, `provenance`, `author`. Audit E26 (2026-05-28) brought this list in sync with `_KNOWN_FIELDS` in `entity_page.py`: v2 fields (`attributes`/`relations`/`provenance`) shipped in earlier work, and `author` shipped with audit E19 (2026-05-28) for user-authored protection.

**Anything else is preserved verbatim** in `entry.extra` (round-trip safe). Today Dream uses this to add emergent fields like `identifiers`, `related`, `dream_failure_count`, `dream_quarantine`, etc. without parser changes.

### 3.5 Entity page — v2 target

Path: same.

v2 schema **extends** the current one (backward-compatible with v1; v1 pages parse with `attributes={}` and `relations=[]`):

```yaml
---
type: person
name: Marcelo
aliases: [Marcelo Marmol, 马塞洛]
dream_processed_through: <timestamp or null>
created_at: <ISO timestamp>
updated_at: <ISO timestamp>

# v2 additions:
attributes:
  email: marcelo@mxhero.com
  phone: "+34..."
  current_residence: Spain
relations:
  - to: person:susana
    type: spouse
    since: 2010
  - to: project:durin
    type: maintains
    intensity: high
    since: 2024-01
provenance:
  attributes:
    email:
      source_ref: episodic/2026-05-23T10-12.md
      extracted_at: 2026-05-23T10-30Z
  relations:
    - index: 0                                   # spouse → susana
      source_ref: episodic/2026-01-15T19-00.md
      extracted_at: 2026-01-15T20-00Z
identifiers: ...                                 # other emergent fields preserved
---

<markdown body — free form prose maintained by Dream>
```

**v2 fields explained:**

- `attributes`: dict of primitive facts. Free-form keys (no closed catalog). Values can be scalar or nested dicts (for stateful attributes — see §4.3).
- `relations`: list of objects. Each has `to` (entity URI), `type` (relation kind), and free-form metadata (`since`, `intensity`, `role`, etc.).
- `provenance`: traceability dict. `attributes.<key>.source_ref` says which entry created/updated the attribute. `relations[i].source_ref` similarly per relation index.

**Read-side constraints:**

- v1 pages (without `attributes`/`relations`) must continue parsing — treat as `attributes={}`, `relations=[]`.
- Unknown emergent fields in frontmatter still preserved in `extra` (round-trip).

**Write-side constraints:**

- `attributes` must be a dict. Keys must be strings.
- Each `relations` item must have `to` matching `<type>:<slug>` and `type` as a non-empty string.
- `provenance.attributes.<key>` references must resolve to existing files at write-time (Dream validates).
- YAML round-trip must preserve CJK, URLs, quotes (existing tests cover this).

### 3.6 Archive — v2

Path: `memory/archive/<class>/<id>.md`

When Dream consolidates an episodic into an entity page, the episodic file is **moved** (not copied or deleted) to `memory/archive/episodic/<id>.md`. The frontmatter gains an `archived_at` timestamp and an `archived_into` reference:

```yaml
---
id: <original id>
headline: <original headline>
# ... all original fields preserved ...
archived_at: <ISO timestamp>
archived_into: person:marcelo                    # the entity URI it was consolidated into
---

<original body preserved>
```

**`memory/archive/` is invisible to all default retrieval paths.** This is non-negotiable for the system to behave correctly after Dream consolidates an entry; the whole point of archiving is to remove the entry from competing with the canonical synthesis in search results.

Concretely, archive must be excluded from:

| Path | How it's excluded |
|---|---|
| Vector index (LanceDB) | Indexer skips `memory/archive/**`. When Dream archives an entry, the corresponding LanceDB row is deleted. |
| Lexical index (FTS5/BM25) | Indexer skips `memory/archive/**` on rebuild. Row deleted on archive. |
| Raw grep / walk+parse on disk | The shared `walk_memory(workspace)` helper used by `search_undreamed`, fallback grep, and any future scanner must exclude `archive/` from its file enumeration. There is exactly one such helper; it is the chokepoint. |
| Read-side helpers (entity_ranker, alias bootstrap, etc.) | Same — they consume the workspace walker output; if it excludes archive, they do too. |

Access to archived content requires an **explicit opt-in**:

| Surface | Mechanism |
|---|---|
| Diagnostic search | `memory_search(..., scope='archive')` walks `memory/archive/` on demand, parses on the fly, returns results. No parallel index for archive in MVP. **Shipped audit F2 (2026-05-28)**: enum accepts `'archive'`; substring match over headline+summary+body+name+aliases of every archived `.md`; no re-rank / cross-encoder. |
| CLI recovery | `durin memory expand <entity>` renders the canonical page plus its archived predecessors; `cat memory/archive/<class>/<id>.md` and `find memory/archive -name '*.md'` cover direct lookup and enumeration. Audit G2 (2026-05-28) explicitly **decided against** adding dedicated `durin archive show / list` commands — the three existing surfaces already cover recovery without a unique use case for a fourth. See `08_scope_and_discarded.md` §2.12. |
| Direct file access | The user opens `memory/archive/<class>/<id>.md` in any editor; nothing prevents this. |

**Recovery is rare by design.** Walking the archive folder on demand is acceptable latency for the expected frequency (debugging, audit, occasional rollback). If frequent archive queries emerge in real use, a metadata table (e.g., SQLite with uri/headline/archived_into/archived_at) can be added without changing the storage layout. The MVP does not include this.

No path in the system should accidentally include archive content. If a developer adds a new walker, scanner, or indexer, it must use the shared workspace walker (which excludes archive) or explicitly justify why archive is included.

---

## 4. Entity model details (v2)

### 4.1 URIs

All entities have a canonical URI in the form `<type>:<slug>` where:

- `type` matches `[a-z][a-z0-9_]*`. Vocabulary is **open**; the agent and Dream can create new types as needed.
- `slug` is lowercase, hyphen-separated, ASCII-folded from `name`. For non-Latin names, a transliterated slug is used (see §4.5).

Examples:
- `person:marcelo`
- `bug:auth_middleware_leak`
- `project:durin`
- `commit:abc123def`
- `file:src_auth_middleware_ts`

### 4.1.1 Suggested starter types

The vocabulary is open, but Dream needs a starting set so it doesn't invent arbitrary types from the first observation. The following 8 types form the **suggested starter set** (from `docs/18_entity_centric_plan.md` §4, grounded in cognitive memory literature — Tulving tripartite, CoALA, Conway, Rosch prototype theory):

| Type | Tulving mapping | Cross-profession examples |
|---|---|---|
| `person` | Semantic | coworker, client, professor, family member |
| `place` | Semantic | office, market, campus, home |
| `project` | Semantic | software project, marketing campaign, thesis, house move |
| `topic` | Semantic | embeddings, B2B funnels, machine learning, minimalism |
| `event` | Episodic | outage, demo, exam, birthday |
| `artifact` | Semantic | file, slide deck, textbook, passport |
| `stance` | Semantic | preference, opinion, belief, position |
| `practice` | Procedural | skill, routine, method, habit |

**What is NOT a primary type** (these emerge as derived, not as their own entities):

| Concept | Where it lives |
|---|---|
| "Learning" or "lesson" | Consolidation of `topic` or update of `practice` (reflection pattern à la Generative Agents) |
| "Error" | An `event` with negative valence, or a corrected `stance` |
| "Decision" | A point-in-time `event` with an associated `stance` |
| `file`, `symbol` | Fall under `artifact`, or referenced from another entity's frontmatter without their own page |
| Concrete `tool` | The tool-as-object is an `artifact`; the method of using it is a `practice` |

**Open vocabulary still applies.** If Dream proposes a new type recurrently (e.g., `bug` in a coder workspace, `deal` in a sales workspace), the type joins the canonical list in code without schema change. The distinction "recognized type vs emergent type" lives in code (a small allowlist used for slug-collision prevention and UI hints), not in the schema.

Dream's prompt (`06_prompts_and_instructions.md` §4) carries this list as part of its context so new entity decisions default to a familiar type when possible.

### 4.2 Attributes — design rules

Attributes are free-form primitive facts. To prevent drift over time, Dream applies these rules at write-time:

| Rule | Mechanism |
|---|---|
| **Reuse existing keys when meaning matches** | Dream prompt includes the entity's existing schema. LLM must reuse known keys before inventing new ones. |
| **Justify new keys** | If a new key is needed, Dream emits it with a brief rationale in the commit message. |
| **No closed catalog** | New keys are allowed across entities. Drift control is per-entity, not global. |
| **Values can be scalar or nested** | `email: "x@y.com"` (scalar) or `status: {value: open, since: 2026-05-01}` (nested, for stateful facts — see §4.3). |

### 4.3 Stateful attributes (temporal states)

Some attributes change over time and history matters. The shape supports two variants:

**Static value (default):**
```yaml
attributes:
  email: marcelo@mxhero.com
```

**Stateful value (history-preserving):**
```yaml
attributes:
  status:
    current: closed
    history:
      - value: open
        from: 2026-05-01
        to: 2026-05-15
      - value: closed
        from: 2026-05-15
```

**Selection rule (deterministic, no LLM judgment involved):**

An attribute is **stateful** if and only if its key name matches one of these patterns:

| Pattern | Examples |
|---|---|
| Exact match: `status` | `attributes.status` |
| Exact match: `phase` | `attributes.phase` |
| Exact match: `state` | `attributes.state` |
| Prefix `current_` | `attributes.current_residence`, `attributes.current_employer`, `attributes.current_role` |

All other attribute keys are **static** (overwriting on update, no history). When Dream sets a stateful attribute and detects a change, it appends to `history` and updates `current`. When it sets a static attribute and detects a change, it overwrites the previous value (the prior value remains accessible via git history of the entity page).

The patterns are applied by Dream during consolidation per **demonstration** in the prompt's few-shot examples (`durin/templates/dream/examples/`, especially `01_new_entity.md`, `02_update_attribute.md`, `04_handle_conflict.md`, `06_no_changes.md`). Audit E27 (2026-05-28) corrected an earlier claim that the patterns were articulated in `rules.md` — grep against `rules.md` and `consolidator.md` returns no mention of "stateful" or the explicit selection rule; the LLM learns the convention by example.

No `STATEFUL_ATTRIBUTE_PATTERNS` constant exists in code — earlier drafts of this section referenced one that was never extracted. Decision: keep the rule in the LLM-facing prompt corpus (where it lives today) rather than mirror it as a Python regex set. If the rule ever needs to gate non-LLM code paths, extract the constant then. (Audit C1, 2026-05-28.)

### 4.4 Relations — design rules

Relations are first-class graph edges from one entity to another. Rules:

| Rule | Mechanism |
|---|---|
| **First-class only if information-bearing** | A relation must add information beyond mention. Mere "appeared in session X" is NOT a relation. |
| **Targets must have URIs** | `to` field references another entity. If the target doesn't exist, Dream creates a placeholder (`auto_created: true` in extra). |
| **Free-form metadata** | Each relation can carry `since`, `intensity`, `role`, etc. No enforced schema beyond `to` and `type`. |
| **Per-entity cap** | **Soft cap 50 + hard cap 200, enforced at Dream apply time** (audit B-19, 2026-05-29; supersedes the deferred state from audit C2 2026-05-28). `durin.memory.entity_relation_cap.check_relation_cap` runs after `_apply_ops_to_page` succeeds: crossing the soft cap fires `memory.entity_relation_cap_warned` (apply proceeds); crossing the hard cap fires `memory.entity_relation_cap_rejected` and rolls back the patch with `DreamApplyFailureKind.VALIDATION`. The LLM sees the current count as `current relation count: N` in the consolidator prompt (§4.2 of doc 06) and Rule 9 of `rules.md` instructs it to budget against the cap before fanning out new `/relations/-` ops. |

**Pure mentions are NOT relations.** "Marcelo was mentioned in session abc" is covered by:
1. The vector index (sessions are vectorized via `_last_summary` in v2; episodic via body).
2. Dynamic search at query time.

This avoids hub explosion (one entity becoming connected to hundreds of sessions).

### 4.5 Slug normalization

The slug for an entity URI is derived from `name`:

1. Unicode NFC normalize.
2. Transliterate non-Latin scripts to ASCII via `unidecode` (e.g., 马塞洛 → `Ma Sai Luo` → `ma_sai_luo`). The earlier draft of this step described a pinyin-with-tones intermediate (`马塞洛 → mǎsàiluò → masailuo`); the code uses `unidecode` directly — no tone-marked intermediate exists. Corrected in audit C3 (2026-05-28).
3. Lowercase.
4. Replace whitespace and punctuation with single underscores.
5. Strip leading/trailing underscores.
6. Truncate to 64 chars.

Examples:
- "Marcelo Mármol" → `marcelo_marmol`
- "AcmeCorp Q4 Renewal" → `acmecorp_q4_renewal`
- "auth middleware leak (high sev)" → `auth_middleware_leak_high_sev`

If two distinct entities produce the same slug, a numeric suffix is added (`marcelo_marmol_2`). Dream's entity dedup pass (`05_dream_cold_path.md`) handles cases where two pages should actually be merged.

**Alias index bootstrap from episodic** (`G3.e`, 2026-05-25): `AliasIndex.build()` (`durin/memory/aliases_index.py`) walks `memory/entities/**/*.md` first to populate aliases from canonical entity pages — that's the primary source. After that, it also walks `memory/episodic/**/*.md` and derives minimal aliases from each episodic's `entities:` frontmatter field. Episodic-derived aliases have **lower precedence** than canonical-derived (a canonical wins on conflict).

Why the episodic bootstrap exists: in a cold workspace (Dream hasn't yet created canonical entity pages but the agent is already storing observations via `memory_store`), the alias index would otherwise be empty and the entity-aware ranker (§8 doc 03) would be inoperative. Bootstrapping from episodic ensures the ranker activates early, before consolidation has happened.

### 4.6 Provenance

Provenance tracks which `source_ref` produced each attribute or relation. This is critical for:
- Auditability ("how do we know Marcelo lives in Spain?")
- Recovery (if Dream consolidated wrong, the original episodic is in `archive/` referenced via provenance).
- Updates (when re-evaluating an attribute, Dream knows the original source).

**Structure:**

```yaml
provenance:
  attributes:
    <attr_key>:
      source_ref: <relative path or URI>
      extracted_at: <ISO timestamp>
  relations:
    - index: <integer, position in relations list>
      source_ref: <relative path or URI>
      extracted_at: <ISO timestamp>
```

`source_ref` may point to:
- An episodic entry: `episodic/2026-05-23T10-12.md`
- An archived episodic: `archive/episodic/2026-05-23T10-12.md`
- A session turn: `session:<session_id>/turn-42`
- An ingested artifact: `ingested/<ingest_id>/source.pdf`

Relation provenance uses `index` to refer to the position in the `relations` list rather than the relation content (which may change). When Dream reorders relations, indices are updated.

#### 4.6.1 Authorship classification (separate from source_ref provenance)

Beyond tracking which entry produced a fact, the system tracks **who wrote each entry** — the agent or a human user. This authorship is independent of `source_ref` and serves a different purpose: **protection from auto-modification**.

**Two authorship values** (`Author` literal in `durin/memory/provenance.py`):

| Value | Meaning |
|---|---|
| `agent_created` | The agent (via `memory_store` / `memory_ingest` / Dream / curator) wrote this entry. Dream and curator may auto-manage it: update, consolidate into an entity page, archive, etc. |
| `user_authored` | A human wrote this entry directly (manual `.md` edit, or via a UI surface marked as user-driven). Dream and curator **never** modify it. |

**Default: `user_authored`** if nothing sets the context — i.e., the system assumes user authorship unless agent-driven code explicitly marks otherwise. This is the safe default: doing nothing leaves user content protected.

**Mechanism: ContextVar.** `_MEMORY_AUTHOR` is a Python `ContextVar` that propagates across `await` and `asyncio.create_task` within a logical request, but stays isolated between concurrent tasks. Agent-driven code paths enter `author_scope("agent_created")` before writing:

```python
from durin.memory.provenance import author_scope

# Agent-driven path:
with author_scope("agent_created"):
    await write_memory_entry(...)

# User-driven path: no scope needed; defaults to "user_authored".
```

When the memory entry is persisted, `MemoryEntry.author` is set from `current_author()` and saved in the frontmatter (visible in the `.md` file).

**Protection rule (enforced by Dream and curator code paths):**

> Dream and the curator **never** modify, archive, or consume entries with `author: user_authored`. They may *read* them as context (e.g., the existing schema of an entity page the user edited) but they never overwrite or move them.

The rationale: a user who edited a `.md` file explicitly stated this content matters. Auto-managing it would destroy the user's stated intent. If the user wants Dream to take over an entry they wrote, they can change the `author:` field manually to `agent_created`.

**Where the rule lives in code** (audit E19, 2026-05-28):

- **Memory entries** (episodic / stable / corpus): the filter is in `cli/memory_cmd.py::_discover_pending_consolidations` (line ~150) — entries with `author: user_authored` never enter the batch the Dream consolidator receives. Pre-E19 this doc claimed the filter was inside `dream.py::DreamConsolidator.apply()`; corrected to the actual location.
- **Entity pages**: `dream_runner.py::_maybe_auto_absorb` checks `page_a.author` and `page_b.author` and emits `memory.absorb.skipped` with `reason="user_authored"` when either side is hand-written. Pre-E19 this protection didn't exist — `EntityPage` had no `author` field, so the §4.6.1 promise was arch-unsupported for entity pages even though it was documented. E19 added the field (defaults to `user_authored` for safety; Dream and absorption set `agent_created` when they write a page) and wired the runner check.

**Note on the discrepancy in the prior schema field** (corrected 2026-05-27): an earlier draft of doc 01 listed `"agent_authored"` and `"dream"` as values; the code only has `"user_authored"` and `"agent_created"`. The schema field declaration in §3.3 has been corrected to match.

### 4.7 Negative facts (no explicit polarity)

The system does **not** model negation as a first-class structure (no `polarity` field, no `not_attributes` namespace, no generic `avoids` relation type). This aligns with mainstream systems — neither mem0, Letta, nor Graphiti has a dedicated negative-fact construct. Graphiti uses temporal validity (`invalid_at`) for "this used to be true and isn't anymore"; mem0 stores negatives as plain text inside fact strings; none has structured negation.

durin follows the same path. Three patterns cover the cases that come up in practice, in order of preference:

| Pattern | When to use | Example |
|---|---|---|
| **Positive equivalent fact** | The negation has a natural positive reframing. Dream is responsible for the reframing. | "doesn't eat meat" → `dietary: vegetarian`. "doesn't accept late submissions" → `submission_policy: strict_deadlines`. |
| **Prose in the body** | No natural positive equivalent exists. The negation lives as a sentence in the entity body. Vector search finds it. | Body contains: "Marcelo dislikes cilantro and complains about it in restaurants." |
| **Temporal validity on a positive attribute** | The attribute was true and stopped being true. | `dietary: { current: omnivore, history: [{value: vegetarian, valid_from: 2020-01, valid_until: 2024-06}] }` |

Dream's prompt instructs the LLM to apply this preference order. The decision is taken at consolidation time, not at retrieval time.

**Trade-off accepted:** the system cannot answer structural queries like "list all entities that do NOT have attribute X". Such queries fall back to vector search over body prose. This matches the trade-off accepted by every mainstream system surveyed.

---

## 5. Lifecycle

This section describes the state transitions for each data class.

### 5.1 Session lifecycle

```
[start of session]
   │
   ▼
sessions/<id>/<id>.jsonl created, meta.json initialized
   │
   ▼
(append events as agent runs)
   │
   ▼
compaction triggers (turn count threshold, context size, etc.)
   │
   ▼
meta.json::derived._last_summary updated
   │
   ▼
[V2] LanceDB re-embeds session:<id> with the new _last_summary
   │
   ▼
[end of session]
   │
   ▼
read-only forever (except metadata fields like title)
```

### 5.2 Ingested lifecycle

```
[user/agent ingest action]
   │
   ▼
ingested/<id>/ created, source file written, metadata.json initialized
   │
   ▼
Ingestion pipeline parses source → emits chunks
   │
   ▼
Each chunk → memory/corpus/<chunk_id>.md (linked to ingested via source_refs)
   │
   ▼
LanceDB + FTS5 index each corpus entry
   │
   ▼
[end] ingested/ frozen; corpus entries are the searchable representation
```

### 5.3 Episodic lifecycle (v2)

```
[memory_store call OR agent extraction]
   │
   ▼
memory/episodic/<ts>.md created
   │
   ▼
re-embed-on-write: LanceDB + FTS5 indexed immediately
   │
   ▼
Threshold trigger: if entity X accumulated N entries, dispatch Dream
   │
   ▼
Dream daemon (locked, throttled):
  - reads post-cursor episodic for entity X
  - consolidates into entity page (updates attributes/relations/body)
  - emits JSON Patch
  - advances cursor (dream_processed_through)
   │
   ▼
Episodic moved to memory/archive/episodic/<id>.md
   │
   ▼
LanceDB + FTS5 remove the original episodic entry
   │
   ▼
Episodic is now invisible to ALL default search paths:
   - vector index: row deleted
   - lexical index: row deleted
   - raw grep / walk+parse: archive/ is excluded from the workspace walker
   - any other reader using the shared workspace walker
Reachable only via explicit recovery (scope=archive flag, CLI command, or direct file access)
```

### 5.4 Stable lifecycle

Similar to episodic but **never auto-archived under any condition**. Stable entries are "promoted" — the user or agent has explicitly marked them as durable. Dream may reference them when consolidating entities, but does not consume, archive, or modify them. Auto-archiving stable would destroy the explicit-durability intent that distinguishes stable from episodic.

The user can edit a stable `.md` directly; the file watcher detects the mtime change and re-derives the index. To remove or supersede a stable entry, the user does so manually (delete or edit the file).

### 5.5 Corpus lifecycle

```
[ingestion produces chunk OR agent calls memory_ingest with raw content]
   │
   ▼
memory/corpus/<id>.md created with body = chunk text
   │
   ▼
LanceDB + FTS5 indexed immediately
   │
   ▼
[corpus entries are searchable references, generally not consumed by Dream]
   │
   ▼
If source ingested is re-ingested → old corpus chunks DELETED (not archived),
                                    new chunks created
```

**Note on re-ingest:** corpus chunks are deleted on re-ingest, not archived. Reasoning: `memory/.git/` already preserves every prior version of every chunk via git history. A parallel `archive/corpus/` folder would be redundant. If the user needs to inspect a prior version of a chunk, they use `git log -p -- memory/corpus/<id>.md` like any other markdown file in the workspace.

### 5.6 Entity lifecycle

```
[first time an entity URI is referenced]
   │
   ▼
Auto-created placeholder: memory/entities/<type>/<slug>.md
  - frontmatter: type, name, aliases=[]
  - extra: auto_created: true
  - body: empty
   │
   ▼
LanceDB + FTS5 indexed
   │
   ▼
[Dream processes episodic referencing this entity]
   │
   ▼
Dream emits JSON Patch:
  - adds attributes, relations
  - extends body
  - updates aliases if new names detected
  - advances dream_processed_through
   │
   ▼
Re-embed: LanceDB + FTS5 re-index the entity page
   │
   ▼
[Dream's entity dedup pass may merge two entities if alias overlap]
   │
   ▼
If absorbed: the absorbed page is moved to archive/entities/<type>/<slug>.md
            with archived_into: <canonical_uri>
```

---

## 6. Re-indexing triggers

Whenever an entity page or memory entry is written, deleted, or moved, the indices must reflect the change. Triggers:

| Trigger | Action |
|---|---|
| `memory_store` creates an entry | Re-embed entry; insert into LanceDB + FTS5 |
| `memory_ingest` creates corpus | Same |
| Dream apply writes entity page | Re-embed entity; update LanceDB + FTS5 row |
| Dream archives episodic | Remove from LanceDB + FTS5; insert in archive index (separate, optional) |
| User edits `.md` manually | File watcher detects mtime change; re-derive that row |
| `durin reindex` command | Wipe `.durin/index/` and rebuild from all `.md` files |

The "user edits manually" path is what makes "markdown is source of truth" real. If the user opens an entity page and edits, the index catches up automatically.

---

## 7. Naming and paths summary

| Artifact | Path pattern | Example |
|---|---|---|
| Session | `sessions/<session_id>/<session_id>.jsonl` | `sessions/c155274d.../c155274d....jsonl` |
| Session metadata | `sessions/<session_id>/<session_id>.meta.json` | `sessions/c155274d.../c155274d....meta.json` |
| Ingested | `ingested/<ingest_id>/source.<ext>` | `ingested/2026-05-26-paper/source.pdf` |
| Corpus | `memory/corpus/<id>.md` | `memory/corpus/2026-05-26-paper-chunk-3.md` |
| Episodic | `memory/episodic/<ts>.md` | `memory/episodic/2026-05-23T10-12-uuid.md` |
| Stable | `memory/stable/<id>.md` | `memory/stable/2026-05-23-marcelo-prefs.md` |
| Pending | `memory/pending/<id>.md` | `memory/pending/2026-05-23-untyped.md` |
| Entity | `memory/entities/<type>/<slug>.md` | `memory/entities/person/marcelo.md` |
| Archived episodic | `memory/archive/episodic/<id>.md` | `memory/archive/episodic/2026-05-23T10-12-uuid.md` |
| Archived entity | `memory/archive/entities/<type>/<slug>.md` | `memory/archive/entities/person/marce.md` |

---

## 8. Backward compatibility

When v2 schema rolls out, the system must:

1. **Read v1 entity pages** (without `attributes`, `relations`, `provenance`) as if those fields were empty. No migration required at read time.
2. **Write v1 pages as v2 only when Dream touches them.** Pages Dream hasn't touched stay v1 until they're updated. The `extra` round-trip preserves any field the parser doesn't recognize.
3. **Dream upgrades v1 → v2 lazily.** First time Dream processes an entity, it adds `attributes`, `relations`, `provenance` (initially empty if no info extracted yet).
4. **The `extra` dict in `EntityPage` continues to accept any frontmatter field.** Migration is non-destructive.

---

## 9. Constraints on YAML safety

YAML round-trip is critical because Dream and the user both edit entity pages. Constraints (covered by existing tests):

| Concern | Constraint |
|---|---|
| CJK in names/aliases/values | Must round-trip byte-identical |
| URLs in values | Must round-trip (no escape mangling) |
| Quoted strings with special chars | Preserve quoting style |
| Lists of objects (relations) | Preserve order and key order within objects |
| Trailing whitespace / blank lines | Preserved in body, not stripped |
| Date values | Parsed as `date` objects but written as `YYYY-MM-DD` strings |

When Dream emits a JSON Patch that modifies frontmatter, the apply pipeline:
1. Reads current `.md` → parses frontmatter to dict.
2. Applies JSON Patch operations.
3. Validates the result against the schema.
4. Writes back using a YAML serializer configured for safety (Block style, default_flow_style=False, allow_unicode=True).

A backup copy (`.md.bak`) is written before each apply. If the post-write validation fails (re-parse + sanity check), the backup is restored.

---

## 10. Module-level decisions

All decisions originally open at the module level have been resolved (2026-05-27). They are recorded here for traceability; their effects are already reflected in §3, §4, §5, and §6.

| # | Decision | Resolution | Applied in |
|---|---|---|---|
| **1** | Static vs stateful attributes — who decides | **Pattern-based on attribute key name.** Attributes whose key matches `status`, `phase`, `state`, `current_*` are stateful (history-preserving); all others are static. Deterministic, no closed catalog, testable. Pattern set may grow as new patterns appear. | §4.3 |
| **2** | Relations per-entity cap behavior | **Soft cap 50: warn only** (telemetry `memory.entity_relation_cap_warned`, apply proceeds). **Hard cap 200: reject** the patch wholesale (telemetry `memory.entity_relation_cap_rejected`, `DreamApplyFailureKind.VALIDATION`, rollback). LLM-facing surface is `current relation count` slot in the consolidator prompt + Rule 9 in `rules.md`. Shipped in audit B-19 (2026-05-29). | §4.4 |
| **3** | Slug collision strategy | **Numeric suffix** (`marcelo_marmol_2`). Real dedup of distinct entities sharing a slug is handled downstream by Dream's absorb-judge, not at the slug level. | §4.5 |
| **4** | Archive recovery surface | **Walk on demand** (no parallel archive index in MVP). Three surfaces: (a) `memory_search(..., scope='archive')` for agent-visible semantic recovery (audit F2, 2026-05-28); (b) `durin memory expand <entity>` for per-entity rendering of canonical + archived predecessors; (c) `cat memory/archive/<class>/<id>.md` + `find memory/archive -name '*.md'` for direct shell access. Audit G2 (2026-05-28) explicitly decided against adding a dedicated `durin archive show / list` command — see `08_scope_and_discarded.md` §2.12. If frequent archive queries emerge, revisit with a metadata table. | §3.6 |
| **5** | Negative facts (e.g., "Marcelo no come carne") | **No explicit polarity mechanism.** Three patterns, ordered by preference: (1) **positive equivalent fact** when one exists naturally ("no eats meat" → `dietary: vegetarian`); (2) **prose in the body** when no positive equivalent fits; (3) **temporal validity** via `valid_from`/`valid_until` for attributes that ended. Aligns with mem0/Letta (text only) and Graphiti (no negation, just temporal validity). No mainstream system models negation as a first-class structure — durin doesn't either. | §4.7 (new) |
| **6** | Archive lifecycle for stable and corpus | **Archive applies only to episodic** consolidated by Dream. **Stable is never archived** (its existence is an explicit user/agent statement of durability; auto-archiving would destroy intent). **Corpus is deleted (not archived) on re-ingest** — git history already preserves all prior versions of corpus chunks; a parallel archive folder for corpus would be redundant. | §5.4, §5.5 |

---

## 10b. Versioning via git history (cross-corpus decision #4)

`memory/.git/` is an active git repository. Every Dream commit (and every user manual edit, if committed) is a version of the workspace. This is the **only** versioning mechanism — no parallel system is introduced, and **no dedicated tool is exposed to the agent**.

### Who uses git history and how

| Consumer | How they access it |
|---|---|
| **Dream pipeline** | Reads `git log --since=... -- <entity_path>` internally when preparing the consolidation prompt. Recent commits are inlined as a `recent_history` block in the LLM input. This is pipeline code, not an MCP tool. |
| **User (now)** | Any git CLI works: `git -C ~/.durin/workspace/memory log -p -- entities/person/marcelo.md`. No additional surface is needed — the repository is already there. |
| **User (post-MVP)** | Web UI renders log + diff viewer. Lives outside this corpus (frontend/UI layer); the data is read directly from `memory/.git/`. |
| **Agent** | **No direct access.** The agent does not query git history through a tool. Whatever historical signal it needs, Dream has already incorporated into the canonical entity body or its commit messages, which are then visible through normal `memory_search`. |

### What Dream gets (pipeline detail)

When Dream prepares to consolidate an entity, its prompt builder inlines a `recent_history` section with the last N commits for that entity's `.md`. Concretely:

```
recent_history (last N=5 commits for entities/person/marcelo.md):
  - 2026-05-26 by dream: "consolidate 7 episodic: add lives_in attribute, update aliases"
  - 2026-05-20 by user: "manual edit: corrected spouse name spelling"
  - ...
```

This:
- Prevents Dream from "forgetting" attributes recently added.
- Lets the LLM see if a piece of info was added and then removed (signal of prior rejection).
- Provides natural anti-drift: the LLM sees its own previous output before regenerating.

### Constraints

- `memory/.git/` remains on local disk. Pushing to a remote is allowed but not required.
- Each Dream apply creates exactly one commit (no batched commits across entities).
- Commit messages follow a structured format (specified in `05_dream_cold_path.md`) so `git log` is grep-able for debugging.
- User manual edits to `.md` files are picked up by the file watcher and committed by the indexer with `author: user` (responsibility of the indexer — see `02_indexing.md`).

### Out of scope

- An MCP `memory_history` tool — not needed. Dream uses git directly; user uses CLI.
- A separate "versions" table in any index — git is the version store.
- Time-travel queries via the search pipeline — search always operates on HEAD.
- Branching for "what if" exploration — single linear history per workspace.
- Web UI render — post-MVP, lives in the UI layer, not this corpus.

---

## 11. Implementation status (current vs target)

Audit E26 (2026-05-28) rebuilt this table — most rows were stale
since Phase 1.9 / Phase 3 work shipped.

| Aspect | Status | Where |
|---|---|---|
| Sessions structure | ✅ Active | `durin/runtime/session.py` |
| Session summaries as a class | ✅ Active (audit A10) | `durin/memory/session_summary_store.py` |
| Ingested structure | ✅ Active | `durin/memory/ingestion.py` |
| Memory entries (episodic/stable/corpus) | ✅ `MemoryEntry` Pydantic | `durin/memory/schema.py` |
| Entity page schema (v2: attributes/relations/provenance + `author`) | ✅ Shipped. Audit E19 added `author` field for user-authored protection. | `durin/memory/entity_page.py::EntityPage` |
| Archive folder | ✅ Active. Dream apply moves files; walker skips archive by default. | `durin/memory/archive.py` + `durin/memory/dream_archive_consumed.py` |
| URI naming | ✅ `<type>:<value>` validated | `durin/memory/entities.py::is_valid_entity_ref` |
| Slug normalization | ✅ Centralised | `durin/memory/entities.py::normalize_slug` (and `EntityPage.slug_from_path`) |
| Provenance tracking | ✅ Active per PATCH op (Phase 1.9). Collected during apply and persisted in the entity page. | `durin/memory/dream_apply.py` |
| Round-trip safety | ✅ Tested | `tests/memory/test_entity_page.py` |
| Versioning + git history exposure | ✅ Dream prompt builder reads `git log` of the entity page over the last 30 days as context. User accesses via any git CLI or webui. | `durin/memory/dream_git_history.py` |

When this module is locked, the migration tasks above will move into `09_implementation_roadmap.md`.

---

## 12. Cross-references

- Indexing of these data types: `02_indexing.md` (pending)
- How the search pipeline uses these structures: `03_search_pipeline.md` (pending)
- Tools that create/modify these: `04_agent_tools.md` (pending)
- Dream consolidation logic: `05_dream_cold_path.md` (pending)
- Prior exploratory discussion: `../29_exploracion_datos_y_relaciones.md`
