# Phase 2 Memory — Design Synthesis (post Hermes + OpenClaw review)

> **Decision document.** Compares the original Phase 2 plan (`03_memory_design.md`)
> against two implemented reference systems (Hermes, OpenClaw) and proposes
> three synthesis paths for review.
>
> **This is NOT a roadmap.** No implementation should follow from this
> document until Marcelo has reviewed point-by-point and chosen a horizon.
> The roadmap comes after the horizon decision.

---

## 0. Context

Phase 2 memory has been on the roadmap since the May 2026 prune, blocked
on three pre-reqs which have since shipped:

- ✅ Real provider-usage token accounting (`usage_prompt_tokens` anchor)
- ✅ Cache visibility (`cache.usage` telemetry event)
- ✅ Skill progressive-disclosure infra (`disable_model_invocation` flag)
- ✅ Session / meta split — LLM-derived projections live in
  `<key>.meta.json::derived`, session.jsonl carries only identity
  state (see §0a below)

## 0a. Design decisions confirmed before Phase 2 starts

Two decisions taken during the design discussion (May 2026) that
constrain how Phase 2 must be built. Documented here so the
implementation phases below land on the right architecture.

### Decision 1 — Memory is 6 utility classes, not one

"Memory" in everyday discussion bundles several distinct utilities. The
implementation phases below already separate some of them (Phase 4 is
clearly clase E — procedural skills), but Phase 1's "markdown
categories" risk conflating clases A, B, C, D into one storage path
because they're all markdown files. They shouldn't be — each has a
different access pattern and a different cost profile.

The six classes (with their access pattern):

| Class | Example | When READ | When WRITTEN |
|---|---|---|---|
| **A** Identity-stable | "user prefiere terse, no emojis" | Every turn, stable layer of system prompt (cache-friendly) | Rare, manual or dream |
| **B** Working / episodic | "yesterday we discussed compaction" | On-demand recall OR volatile layer | Each turn (post) |
| **C** Corrections / guardrails | "don't suggest pytest fixtures; user uses unittest" | Every turn, stable layer (small, high-value) | When user corrects |
| **D** Queryable corpus | Archived tool outputs, code patterns | Only on-demand via `memory_search` tool | When user/agent flags as worth keeping |
| **E** Procedural skills | "when committing, message format is X" | Lazy-load when intent detected | When agent finds stable pattern |
| **F** Prospective | "follow up next Tuesday" | Trigger-based (cron / heartbeat) | When pending item created |

**Layout implication for Phase 1**: instead of `memory/{user,project,feedback,reference}.md`,
use `memory/{stable,episodic,corpus,pending}/`. Skills (clase E) already
live separately in `skills/`. Why this matters:

- **A + C** in `stable/` → always-loaded, small, high-value. Cache stays
  warm even as B/D update.
- **B** in `episodic/` → volatile layer, decays over time.
- **D** in `corpus/` + LanceDB index → NEVER in the prompt by default;
  retrieved via tool. Lets the corpus grow unboundedly without per-turn
  cost.
- **F** in `pending/` → triggered, not prompt-loaded.

The 3-tier prompt cache stability (validated 93-98% hit rate in
production smoke testing) only works if stable/volatile content is
correctly separated. Splitting memory by access pattern keeps that
invariant healthy as memory grows.

### Decision 2 — Session.jsonl is content; .meta.json is derived projection

A principle confirmed in May 2026 and implemented as a refactor before
Phase 1 starts:

- `<key>.jsonl` is **source of truth**: messages exchanged + identity
  state (mode, plan path, todos, channel ownership, title). Replayable
  — if everything else were lost, the conversation can be reconstructed
  from this file.
- `<key>.meta.json` is **derived projection**: compaction summary
  today, tool-call timeline, future embeddings or narrative summary.
  Regenerable — if lost, can be rebuilt by re-processing the jsonl.

**Test mental simple**: "if I deleted this file, could I reconstruct it
from the other?" → if yes, it's derived (`.meta.json`). If no, it's
source of truth (`.jsonl`).

**Implications for Phase 2 memory**:

- Any **session-derived memory** (embeddings of past turns, extracted
  learnings, narrative summary, scoring metadata) writes to
  `<key>.meta.json::derived` — not to session.jsonl, not to a new
  separate file.
- The **memory pipeline** (background_review, curator, dreaming) reads
  `<key>.jsonl` as source content. It never reads from `.meta.json` to
  derive new memory — that would be auto-referencing prior LLM output.
- `Session.metadata` in memory continues to merge both files at load,
  so consumer code keeps reading one flat dict. The split is a
  persistence-layer concern only.

**Implementation**: `SessionManager._DERIVED_METADATA_KEYS` is a
frozenset of keys that get split. Today only `_last_summary`. As Phase
2 introduces `session_embedding`, `extracted_keywords`, etc., they go
in this set from day one.

The original Phase 2 plan (`docs/03_memory_design.md`, May 2026) was
informed primarily by reading Hermes early on. After implementing Phase
1 + observing how production systems actually solve cross-session
memory, we have richer evidence to refine the design. This doc
captures that evidence and proposes three implementation paths.

**Sources for this analysis:**
- `docs/03_memory_design.md` — original design (graph + step nodes)
- `~/git_personal/hermes-agent/` — read with focus on memory providers, curator, background review, skill management
- `~/git_personal/openclaw/extensions/active-memory/` and `extensions/memory-core/` — read with focus on dreaming, LanceDB integration, memory sub-agent

The reports are summarised honestly below — including where the user's
prior recollection of OpenClaw turned out wrong (no MySQL, no in-process
GGUF; details in §3).

---

## 0b. Connection points — hook inventory

The discussion in §0a defined the **six utility classes** and the
**source-of-truth / derived split** in abstract terms. This section
nails them down to actual lifecycle stages of the agent loop and to
concrete code locations. It's the bridge from "what memory IS" to
"where in the codebase each memory operation actually fires."

Two reasons this matters:

1. Several classes already have READ paths wired today (A via
   `ContextBuilder._build_stable_layer`, E via `SkillsLoader`, F via
   `CronService`). Phase 2 should reuse these, not reinvent.
2. The WRITE paths for B, C, D are mostly new — and they need to fire
   at specific lifecycle stages (post-turn for B, tool-driven for D,
   etc.). Without an explicit inventory we risk wiring the wrong hook
   or wiring it in the wrong stage.

### Agent lifecycle stages

```
┌────────────────────────────────────────────────────────────────┐
│ Stage 1 — Pre-turn consolidation                                │
│   consolidator.maybe_consolidate_by_tokens(session)              │
│   ── advances last_consolidated cursor; writes summary           │
│      to history.jsonl + <key>.meta.json::derived._last_summary  │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ Stage 2 — Pre-turn context build                                │
│   ContextBuilder.build_messages → 3-tier system prompt          │
│   ┌──────────────────────────────────────────────┐              │
│   │ STABLE: identity + bootstrap + skills        │ → A, C, E    │
│   │ CONTEXT: mode suffix                         │              │
│   │ VOLATILE: memory + history + summary         │ → B          │
│   └──────────────────────────────────────────────┘              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ Stage 3 — Inside the runner loop (N iterations)                 │
│   AgentRunner.run                                               │
│   • LLM call                                                    │
│   • Tool calls                                                  │
│     ├─ memory_search(query)        → READ corpus (D)            │
│     ├─ memory_store(content)       → WRITE corpus (D)           │
│     └─ on-demand skill load        → READ skill (E)             │
│   • Mid-turn guards                                             │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ Stage 4 — Post-turn                                             │
│   _save_turn + _schedule_background                             │
│   ├─ background_review fork       → WRITE B (and maybe C)       │
│   ├─ tool_call meta events       → sidecar                      │
│   └─ async consolidator if due                                  │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ Stage 5 — Idle / inactivity                                     │
│   Curator (Hermes-style)                                        │
│   ── walks agent_created entries in B/C/D                       │
│   ── promotes / archives / deletes via provenance + scoring     │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ Stage 6 — Scheduled / triggered                                 │
│   CronService + HeartbeatService                                │
│   ├─ Dream cron job (Phase 3) → ranking + promotion B→A/C, D    │
│   └─ Prospective triggers (F)  → inject as user message         │
└────────────────────────────────────────────────────────────────┘
```

### Class × stage × code location

| Class | READ at stage | WRITE at stage | Code (today / Phase 2) |
|---|---|---|---|
| **A** Identity stable | Stage 2 (stable layer) | Stage 5/6 (Dream cron) | **Today**: `MemoryStore.get_memory_context()` reads `MEMORY.md` + `SOUL.md` + `USER.md`. Only Dream writes.<br>**Phase 2**: new split `memory/stable/{IDENTITY,CORRECTIONS,PROJECT}.md`. Same read path; same write authorship (Dream + explicit user). |
| **B** Working / episodic | Stage 2 (volatile layer) OR Stage 3 (memory_search) | Stage 4 (post-turn background_review hook) | **Today**: doesn't exist. `session.jsonl` is the informal proxy.<br>**Phase 2**: `memory/episodic/recent-<window>.md`. Read in `ContextBuilder._build_volatile_layer`. Write via a new hook in `AgentLoop._dispatch_message`'s finally block: spawn a sub-agent post-turn that decides what to keep. |
| **C** Corrections | Stage 2 (stable layer, small) | Stage 4 (background_review detecting a correction pattern) or explicit user signal | **Today**: mixed inside `MEMORY.md`.<br>**Phase 2**: separate `memory/stable/CORRECTIONS.md`. Write from the same background_review as B, branched when the LLM detects "user corrected me about X" semantics. |
| **D** Queryable corpus | Stage 3 ONLY (tool invocation) | Stage 3 (tool invocation) | **Today**: doesn't exist. Zero connection points.<br>**Phase 2**: tools `memory_search` + `memory_store`. NEVER in the system prompt. Read = tool call. Write = tool call. LanceDB index for fast query in Phase 2b. |
| **E** Procedural skills | Stage 2 (stable layer catalog summary) + Stage 3 (lazy-load full content) | Stage 3 (`skill_manage` tool, Phase 4) | **Today**: `SkillsLoader.build_skills_summary()` lists the catalog in the stable layer; `load_skills_for_context([name])` loads a skill when the model invokes it. **Already wired.**<br>**Phase 4**: `skill_manage` tool adds create / edit / patch. |
| **F** Prospective | Stage 6 trigger fires → injected as user message on next turn | Stage 3 (`cron` tool) | **Today**: `CronService` + `cron` tool support time-based triggers. Delivery via `_deliver_to_channel`.<br>**Phase 2/5**: add entity-triggers ("when topic X comes up") and condition-triggers ("when file Y changes"). |

### State today: what's wired vs what's missing

**Wired and complete** (Phase 2 reuses):

- A (identity) → `MemoryStore.get_memory_context` → stable layer of 3-tier prompt.
- E (skills) → catalog in stable + lazy load on-demand at tool invocation.
- F (prospective, time-based only) → `CronService`.

**Wired but conflated** (Phase 2 will untangle):

- C (corrections) lives inside `MEMORY.md` with A today. Same file
  path, same load. The cost: when Dream touches `MEMORY.md` for any
  reason it invalidates the stable-layer cache. If C lived in its own
  file and Dream only modified it on a real correction event, the
  cache hit rate stays higher across normal turns.

**Missing entirely** (Phase 2 builds):

- B (episodic):
  - **Read hook**: a new branch in `ContextBuilder._build_volatile_layer`
    that loads `memory/episodic/recent-<window>.md`.
  - **Write hook**: a new method called from
    `AgentLoop._dispatch_message`'s finally block —
    `await self._background_review(session, last_turn_messages)`.
    Spawns a sub-agent (using the existing `SubagentManager`) with a
    prompt asking "anything from this turn worth keeping?" Runs in
    background, never blocks the user-facing response.

- D (corpus):
  - **New tools** registered in `ToolRegistry`: `memory_search(query)`
    + `memory_store(content, tags)`.
  - **Index** in Phase 2b: LanceDB sidecar at
    `memory/corpus/.index.lance`. The index NEVER loads into the
    system prompt; it's read only when the model invokes
    `memory_search`. Phase 2a can ship with LLM-driven grep over the
    markdown files before adding the vector index.
  - **No new runner hook** — D is 100% tool-driven.

### Provenance — the cross-class thread

Every write path needs to record WHO authored the entry. Mirroring
Hermes' `ContextVar` pattern:

```python
_MEMORY_AUTHOR = ContextVar("memory_author", default="user_authored")

# When background_review writes to B / C / D:
token = _MEMORY_AUTHOR.set("agent_created")
try:
    await write_to_memory(...)
finally:
    _MEMORY_AUTHOR.reset(token)
```

Each memory file (and each corpus entry) carries an
`author: agent_created | user_authored` frontmatter field. **The
curator (Stage 5) and Dream (Stage 6) can only touch `agent_created`
entries.** This is the safety mechanism that lets the agent
self-maintain its memory without ever overwriting something the user
edited by hand.

This pattern is also what makes the
`SessionManager._DERIVED_METADATA_KEYS` split from §0a Decision 2 work
correctly when memory subsystem fields are added (e.g.
`session_embedding`, `narrative_summary`): they're agent_created by
construction, so they go to the sidecar's `derived` block without
touching anything the user authored.

### New hooks Phase 2 needs to add

A summary of the additions, indexed by the phase they belong to. Each
hook is small (≤ 100 lines including tests) and each is independently
shippable.

| Hook | Stage | Purpose | Phase |
|---|---|---|---|
| `ContextBuilder._build_volatile_layer` loads `memory/episodic/recent-*.md` | 2 | Read clase B | Phase 1 |
| `AgentLoop._post_turn_background_review` | 4 | Write clases B + C | Phase 1 |
| `Curator.run()` (cron + inactivity-triggered) | 5 | Cleanup `agent_created` entries in B/C/D | Phase 1 |
| Tools `memory_search` + `memory_store` | 3 | Read/Write clase D | Phase 1 (LLM-grep) + Phase 2b (LanceDB) |
| `Dream.consolidate_and_promote()` extension | 6 | Promote B → A/C/D with multi-factor scoring | Phase 3 |
| `skill_manage` tool | 3 | Write clase E (agent-authored skills) | Phase 4 |
| Entity-trigger + condition-trigger in `CronService` | 6 | Extend clase F beyond time | Phase 5 |
| `_MEMORY_AUTHOR` ContextVar + frontmatter `author:` field | cross | Provenance for every write | Phase 1 — foundation of all of the above |

### What does NOT change

- **Runner / consolidator / 3-tier prompt**: infrastructure is in
  place. Phase 2 does not touch the loop architecture, only adds
  side-channels for memory reads / writes.
- **Session / meta split** (§0a Decision 2): already implemented.
  Future memory writes that are derived (embeddings, narrative
  summaries) go automatically to the sidecar's `derived` block via
  `_DERIVED_METADATA_KEYS`.
- **Telemetry**: the schema catalog (`durin/telemetry/schema.py`) is
  set up to absorb new events without restructuring. Phase 2 only
  adds TypedDicts for `memory.recall`, `memory.store`, `curator.run`,
  `dream.promote`.

---

## 0c. Consolidated architecture (May 2026, post external review)

> **Canonical reference.** After comparing hermes-agent, openclaw,
> cognee, mempalace, and hindsight against Marcelo's clarified vision
> (sessions as source of truth; ingested documents as a second source;
> daily dream as the derivation engine; markdown + anchors for
> provenance; minimal per-turn token cost), the design consolidated to
> the shape below. **This section supersedes the design exploration in
> §1–§5** — those sections are preserved as the trail of "how we got
> here" but should be read as historical context, not active design.

### 0c.1 Three sources of truth

The system has three kinds of canonical artifacts. Each is immutable
once written and fully replayable.

| Kind | Where | Origin |
|---|---|---|
| **Sessions** | `sessions/<key>.jsonl` | Conversations agent↔user. One file per session. Append-only. |
| **Ingested docs** | `ingested/<id>/source.<ext>` | Artifacts the user explicitly hands the system via `memory_ingest(path)`. Frozen at ingest time. |
| **Memory entries** | `memory/<class>/<id>.md` | Derived knowledge — learnings, conclusions, preferences. Created by dream or by `memory_store` tool call. **Mutable**: the user may edit them by hand. |

Memory entries are the only "fuente de verdad" the user is expected to
hand-edit. Sessions and ingested docs are never edited by hand. The
provenance system (§0c.4) distinguishes `agent_created` from
`user_authored` entries so the curator and dream only auto-manage the
former.

### 0c.2 Layout on disk

```
~/.durin/
├── sessions/
│   ├── <key>.jsonl                  ← canonical (replayable turn log)
│   ├── <key>.meta.json              ← derived: summary + tags (entities, topics)
│   └── <key>.md                     ← derived: navigable view with #turn-N anchors
│
├── ingested/<id>/
│   ├── source.<ext>                 ← canonical (frozen artifact)
│   ├── source.md                    ← derived (if source isn't already markdown)
│   └── meta.json                    ← derived: summary + entities + relations
│
├── memory/
│   ├── stable/<id>.md               ← classes A + C (identity, corrections)
│   ├── episodic/<id>.md             ← class B (working / recent)
│   ├── corpus/<id>.md               ← class D (queryable corpus)
│   └── pending/<id>.md              ← class F (prospective items)
│
└── dream/
    └── cursor.json                  ← what dream processed and up to when
```

Three structural observations:

- The session/meta split (§0a Decision 2) generalises: every canonical
  source has a sibling `.meta.json` for derived projections and a
  sibling `.md` for human-navigable views.
- Memory entries are markdown files in subdirectories matching the
  utility classes from §0a Decision 1. The class is encoded in the
  directory name, not in frontmatter.
- Dream's progress lives in its own cursor file, not in each session's
  meta.json. Decouples session lifecycle from dream lifecycle: resetting
  dream doesn't dirty session metadata; deleting a session doesn't
  break dream's bookkeeping.

### 0c.3 Lifecycle — what happens on each event

Six events drive every state change. Listed in order of frequency.

| Event | Trigger | Who runs it | Output |
|---|---|---|---|
| **Turn** | User or assistant message | Existing session writer | Append to `<key>.jsonl` |
| **Compaction** | Token threshold inside the session | Main conversation model | Summary in `meta.json::derived._last_summary` (existing). NEW: tags (`entities`, `topics`) in `meta.json::derived`. Regen of `<key>.md`. |
| **Session close** | Inactivity timeout | Deterministic formatter | Force `<key>.md` if compaction never fired during the session. |
| **`memory_ingest(path)`** | User invokes the tool | Main conversation model (synchronous) | Copy source to `ingested/<id>/source.*`. Generate `source.md` if the source isn't markdown. Produce summary + entities + relations in `meta.json::derived`. |
| **`memory_store(content)`** | Agent calls the tool, typically because user asked to remember something | Main model | Direct write to `memory/<class>/<id>.md` with full frontmatter (§0c.5). |
| **Dream** | Cron, default once per day (configurable) | Cheap model (Haiku 4.5 or local Ollama) | Read sessions and docs since `cursor.json`. Reorganise. Derive conclusions. Create or update memory entries. Refresh hot layer. Advance cursor. |

**Division of labour**: compaction does local work (within one
session); dream does global work (across sessions and docs).

**Choice of model per event** is deliberate:

- Compaction uses the same model that's running the conversation. It's
  infrequent, the prompt is already loaded, and tag extraction adds
  ~10% to the prompt — no extra LLM call.
- Document ingestion uses the main model synchronously because the user
  asked for it and is waiting on the result. The cost is declared and
  scoped to that one action.
- Dream uses a cheap model because it runs unattended over potentially
  many candidates per night.
- `memory_store` uses the main model because it's a single inline tool
  call whose result the agent might use later in the same turn.

### 0c.4 Provenance via markdown links

Every derived artifact links to its sources using standard markdown
links pointing to stable anchors in the `.md` views of canonical
sources.

Anchor conventions:

- **Sessions** — `<key>.md#turn-N` where N is the 1-indexed turn
  position. When a session is compacted, consolidated turns receive
  aggregate anchors `#consolidated-M` so older links stay resolvable.
- **Ingested docs** — native markdown headers in
  `ingested/<id>/source.md`. If the canonical source isn't markdown,
  the derivation step produces `source.md` with headers reflecting the
  original document structure.
- **Memory entries** — each is its own document, addressed by file
  path (`memory/<class>/<id>.md`) for whole-file links.

Why this matters in practice:

- The user opens any `.md` in any markdown viewer, clicks a link in
  `source_refs`, and jumps to the exact turn or section that produced
  the learning. No special tooling required.
- The drill-down API (§0c.6) consumes the same URI scheme.
- Regenerating `.md` views from canonicals is deterministic, so anchor
  stability is preserved across reformatter changes.

Memory entries also carry an `author:` frontmatter field
(`agent_created` | `user_authored`), populated via a `ContextVar`
(`_MEMORY_AUTHOR`) at write time. The curator and dream only
auto-manage `agent_created` entries — anything the user authored or
edited by hand is left alone.

### 0c.5 Memory entry frontmatter (multi-resolution)

Each memory entry carries three resolutions in a single file:

```yaml
---
id: mem-001
headline: "Usuario prefiere terse, sin emojis"          # ~10 words → hot layer
summary: "Confirmado S1, refinado S3 tras corrección"   # ~50 words → search/warm
source_refs:
  - "[turn 42](../sessions/abc.md#turn-42)"
  - "[seccion 3.1](../ingested/doc-7/source.md#api-conventions)"
related:
  - "[refina](mem-001-prev)"
entities: [usuario:marcelo, proyecto:durin]
author: agent_created
valid_from: 2026-05-20
---
(body: ~200-500 words — full content → search/cold or memory_drill)
```

Resolution semantics:

- `headline` (~10 words) — the hot layer pulls these in bulk.
- `summary` (~50 words) — returned by `memory_search(level="warm")`.
- `body` (~200-500 words) — returned by `memory_search(level="cold")`
  or by `memory_drill`.

`source_refs` uses markdown links. `related` uses bare ids when
pointing to other memory entries, or markdown links otherwise.

### 0c.6 Search and drill-down API

Two tools, scoped by category and resolution level.

```python
memory_search(query, scope="all", level="warm")
  scope: "undreamed" | "dreamed" | "all" | "sessions" | "ingested"
  level: "warm" | "cold"

  # undreamed → grep over <key>.md filtered by tags in meta.json
  # dreamed   → read over memory/<class>/*.md
  #             (+ vector if Phase 2 active, + BM25 if Phase 2c enabled)

memory_drill(uri)
  # uri examples:
  #   "sessions/abc.md#turn-42"
  #   "ingested/doc-7/source.md#api-conventions"
  #   "memory/stable/mem-001"
  # Returns ONLY the section addressed by the anchor (plus minimal
  # context envelope, e.g. parent header).
```

Default agent path: `kg_query` → `memory_search(level="warm")` →
`memory_drill`. Cheapest first; only drill deeper when the warm result
is insufficient. `kg_query` lives in Phase 3 (see §0c.9).

### 0c.7 Hot layer — refreshed by dream

What loads into the prompt **without any tool call**:

| Component | Size | Source |
|---|---|---|
| Identity essentials | ~200 tokens | `memory/stable/IDENTITY.md` |
| Top headlines | ~500 tokens | top-K memory entries by score |
| Entity name list | ~200 tokens | distinct entities across active memory |

Refreshed by **dream**, not by compaction or per-turn writes. The hot
layer is therefore invariant across an entire day, preserving the
stable layer of the 3-tier system prompt and keeping cache hit rates
near 100% on the upstream provider.

Between dreams the hot layer is read-only. If the user makes a
correction during the day that the agent must remember **before** the
next dream, the agent calls `memory_store` which writes directly to
`memory/<class>/<id>.md`. The next `memory_search` will surface it,
but it won't enter the hot layer until dream picks it up.

### 0c.8 Relationship to the six utility classes (§0a Decision 1)

The classes A–F describe **access pattern** — when and how a memory
entry enters the prompt or is retrieved. The consolidated architecture
adds storage structure but does not replace the taxonomy:

- A (identity-stable) + C (corrections) → `memory/stable/`, in hot layer
- B (working / episodic) → `memory/episodic/`, in hot-layer rotation
- D (queryable corpus) → `memory/corpus/`, never hot, only via `memory_search`
- E (procedural skills) → `skills/` (managed separately, Phase 4)
- F (prospective) → `memory/pending/`, trigger-injected

Same file format and lifecycle across A, B, C, D, F — only the
directory and access pattern differ.

### 0c.9 Phase mapping

| Phase | Scope | Estimate |
|---|---|---|
| **1** | `<key>.md` derivation + tags during compaction + `ingested/` source path + `memory_ingest` + `memory_store` + `memory_search` (grep over markdown + tag filter) + `memory_drill` + 6-class directory layout + `_MEMORY_AUTHOR` provenance | 2–3 weeks |
| **2** | LanceDB index over memory entry summaries. Vector retrieval inside `memory_search(level="warm")`. | 2 weeks |
| **2c** (opt-in) | TEMPR-style multi-strategy: BM25 + temporal + RRF as user-toggleable config knobs | 1 week if activated |
| **3** | Dream daily cron + multi-factor scoring + freshness trends + hot-layer refresh + curator for `agent_created` cleanup. SQLite KG (entities + triples with `valid_from`) for `kg_query`. | 1–2 weeks |
| **4** | Dynamic skills — `skill_manage` tool, agent-built skills with lifecycle | 2 weeks |
| **5** (optional) | Prospective memory beyond time triggers (entity + condition triggers) | 2 weeks |

What changes versus the §5 Option C exploration:

- Phase 1 is simpler than originally proposed: **no per-turn
  `background_review` fork**. The existing session consolidator
  handles session-local compaction work; dream handles cross-session
  derivation. The "cognify pipeline" effectively lives inside dream.
- Multi-resolution (`headline` / `summary` / `body`) and provenance
  (markdown links to anchors) are concrete from Phase 1 via frontmatter.
- The knowledge graph lives in Phase 3 and is opt-in. Phase 1
  retrieval is grep + tag filter; Phase 2 adds vector search.
- Hot layer is refreshed daily by dream, not per-turn. Cache stability
  is the explicit design goal.

---

## 1. The original plan (doc 03 summarised)

> Full text: `docs/03_memory_design.md`. This is a condensed restatement,
> not a substitute.

### Core mental model

A **graph of step nodes** representing the agent's activity, plus
projections of that graph into the model's working context. Biologically
inspired (working memory, episodic memory, semantic memory layered).

```
Step nodes ──┐
             ├──> Dynamic projection ──> System prompt
Live goal ──┤                             (filtered, ranked,
Pending items┤                              token-budgeted)
Recent steps ┤
Milestones ──┘
```

### Key building blocks

- **Step node**: One atomic action (tool call, observation, decision). Schema includes `id`, `parent_id`, `kind` (one of action / observation / decision / milestone), `summary`, `entities` (referenced objects), `timestamp`, `outcome`.
- **Live goal**: The current objective. Surfaces in every turn.
- **Pending items** (prospective memory): Things the agent intends to do later. Surface when triggers fire (time, entity, condition).
- **Recent steps**: FIFO queue of last N step nodes for short-term continuity.
- **Milestones**: Accumulated summary nodes that consolidate older history into compressed representations.
- **Dynamic projection**: A per-turn computation that selects which step nodes / milestones / pending items to surface based on the current query, budget, and graph structure.

### Storage / persistence

The doc proposes graph persistence as nodes + edges, with timestamps and
provenance. No specific store chosen (left open).

### Strengths of the original plan

1. **Structured semantic representation** — entities, decisions, and observations are first-class citizens. Retrieval can be entity-graph-driven, not just text-similarity-driven.
2. **Prospective memory** — explicit handling of "things to do later" is unique. Neither Hermes nor OpenClaw has this.
3. **Milestone compression** — accumulated summaries provide tiered history (recent detail, older summary).

### Weaknesses now visible (with the benefit of hindsight)

1. **Heavy upfront design** — graph schema with N node types + projection logic is multi-week work before producing any retrieval value. Both Hermes and OpenClaw show simpler primitives are sufficient for V1.
2. **No concrete storage choice** — leaving "graph store" abstract makes implementation drift inevitable.
3. **No active-learning loop** — the plan is read-side heavy (projection on demand). Both Hermes and OpenClaw show that a write-side feedback loop (background_review / auto-capture) is what produces the most value-per-day on actual usage data.
4. **No operational safety** — the original plan has no timeout / circuit breaker discussion for the retrieval path, which is a real production concern (memory subsystem failures must not break the main loop).

---

## 2. Hermes — what they actually built

> Source: `/Users/marcelo/git_personal/hermes-agent/`, focus on
> `plugins/memory/`, `agent/curator.py`, `agent/background_review.py`,
> `tools/skill_provenance.py`, `tools/skill_manager_tool.py`,
> `agent/system_prompt.py`.

### Memory providers

Hermes ships **8 pluggable providers**, **at most one active per session**:

| Provider | Storage | Retrieval | Notes |
|---|---|---|---|
| **Honcho** | Honcho cloud | AI-native Q&A + semantic search + peer cards | Cross-session user modeling |
| **Hindsight** | Cloud or local | Knowledge graph + fuzzy text + LLM-driven search | Entity resolution + multi-strategy retrieval |
| **Mem0** | Mem0 Platform API | Server-side LLM fact extraction + reranking + dedup | Circuit breaker (5 fails → 2min cooldown) |
| **Holographic** | SQLite local | HRR compositional + entity resolution + trust scoring | Local-only fact store |
| **ByteRover** | Local + cloud sync | Tiered fuzzy → LLM | CLI-driven |
| **OpenViking** | Volcengine | Filesystem-style hierarchy with tiered context loading (L0~100, L1~2k, L2 full) | Auto-extraction in 6 categories |
| **RetainDB** | Cloud + SQLite write-behind queue | Semantic + dialectic synthesis + SOUL.md persona | Crash-safe async ingest |
| **Supermemory** | Cloud | Hybrid/semantic/document modes | Session-end conversation ingest |

**Lesson**: by making memory an interchangeable plugin, Hermes avoids
the "which store do we pick?" question. Different backends suit
different deployments. The cost is tool-schema bloat (each provider
defines its own tools), addressed by the single-active rule.

### Active-learning loop (the part Marcelo flagged as desirable)

**Two cooperating background mechanisms:**

```
Per-turn (background thread):
  spawn_background_review_thread(snapshot)
    │   forks the agent with the parent's runtime
    │   (provider, model, cached system prompt → reuses prefix cache)
    ├─ _MEMORY_REVIEW_PROMPT: "anything the user revealed about
    │     themselves worth saving?"
    └─ _SKILL_REVIEW_PROMPT: "any workflow/technique/correction
          to capture or patch?"
          Preference order (from the prompt):
            1. Update an already-loaded skill
            2. Update existing umbrella skill
            3. Add support file (references/, templates/, scripts/)
            4. Create new umbrella class-level skill
  → writes go straight to skill/memory stores
  → main prompt cache untouched

Inactivity-triggered (≥7 days idle):
  maybe_run_curator()
    │   forks agent with tool whitelist = skill_manage only
    ├─ Auto-transitions: active → stale (30d) → archived (90d)
    └─ Touches ONLY agent_created=true skills
       Pinned skills bypass; archive is recoverable (no deletes)
       State persisted in .curator_state JSON
```

### ContextVar provenance — the key safety mechanism

```python
# tools/skill_provenance.py
_write_origin = ContextVar("write_origin", default=None)

# In run_agent.py, before any tool loop:
token = _write_origin.set("background_review")  # or "assistant_tool"

# In skill_manager_tool.py create():
if get_current_write_origin() == "background_review":
    skill_usage.record(name, agent_created=True)
```

**Why this matters**: ContextVar survives async boundaries and thread
pool workers within the same logical request. So the curator can
distinguish "skill I (the agent) wrote during background_review" from
"skill the user wrote by hand" — even when many tools run concurrently.
The curator only auto-manages the first kind.

### 3-tier system prompt (cache-friendly)

```
Tier 1 (stable):   identity + tool guidance + skills index + env hints
                   → cached at session start, invalidated only on
                     context compression
Tier 2 (context):  system_message from caller + AGENTS.md/.cursorrules
                   from cwd
Tier 3 (volatile): memory snapshot + USER.md + timestamp + session id

Joined with \n\n. Stored on agent._cached_system_prompt.
```

Effect: rebuilds only on compress. Keeps the upstream provider's prefix
cache warm across turns — measurable as a higher cache hit ratio in
their `cache.usage`-equivalent telemetry.

### Self-built skills (`tools/skill_manager_tool.py`)

```
skill_manage(action="create"|"edit"|"patch"|"delete",
             name=..., category=..., content=...)

Storage: ~/.hermes/skills/<category>/<name>/SKILL.md
         + optional references/, templates/, scripts/, assets/

Schema: YAML frontmatter (name, description, platforms, conditions)
        + markdown body. Validation: name regex, 100k char limit.
```

Skills built by the agent are flagged via `skill_usage.agent_created`
so the curator can auto-manage them. Skills authored by the user are
untouched.

### Concurrent tool execution

`agent/tool_executor.py` — `_should_parallelize_tool_batch()` walks
tool metadata: read-only tools (list, view, search) parallelise; write
tools (terminal, delete, skill_manage) serialise. Uses
`ThreadPoolExecutor` with up to 8 workers + per-thread interrupt
signaling.

### What Hermes does NOT have

- **No vector index, no embeddings** (memory is provider-delegated; some providers internally embed, some don't)
- **No scheduled consolidation** (curator is inactivity-triggered, not cron)
- **No multi-factor scoring** for memory promotion
- **No memory sub-agent** as a separate pre-step (memory retrieval happens inline via tool calls)

---

## 3. OpenClaw — what they actually built

> Source: `/Users/marcelo/git_personal/openclaw/`, focus on
> `extensions/active-memory/`, `extensions/memory-core/`,
> `extensions/memory-lancedb/`.
> **The user's prior recollection had three errors corrected below.**

### Corrections to prior recollection

| Marcelo's recollection | Verified reality |
|---|---|
| ❌ "MySQL with vectors" | **LanceDB** (embedded vector DB, file-based local, optional S3/GCS). Single table per agent: `{id, text, vector[1536], importance, category, createdAt}` |
| ⚠️ "Local GGUF embedding model" | Embeddings are **pluggable HTTP providers** (OpenAI default, alternatives: LM Studio / Ollama / Deepinfra / Voyage / Bedrock / Google / Mistral). LM Studio + Ollama internally load GGUF but OpenClaw calls them over HTTP — no in-process model loader |
| ✅ "Activation opt-in" | Confirmed. Plugin `active-memory` opt-in via config; session-level toggle + chat-type filters + chat-id allow/deny |
| ✅ "Daily dream / reorganisation" | Confirmed. Called "dreaming". Cron-driven (default `0 2 * * *`). Sophisticated multi-factor scoring (see below) |

### LanceDB storage

```
~/.openclaw/memory/lancedb/<agent>/  (default; cloud paths supported)
└── memories (single table)
    ├── id: UUID
    ├── text: string (≤500–1000 chars)
    ├── vector: float[1536]   (dim = embedding model output)
    ├── importance: float 0..1
    ├── category: enum (preference | fact | decision | entity | other)
    └── createdAt: unix ms
```

No relations, no secondary indices beyond the LanceDB vector index. The
schema's brutally simple — the smarts live in the ranking and write
paths, not the storage shape.

### Pluggable embedding providers

```yaml
plugins:
  memory-lancedb:
    embedding:
      provider: openai       # or lmstudio | ollama | voyage | bedrock | ...
      model: text-embedding-3-small
      dimensions: 1536       # auto-resolved if omitted
```

Same dispatch pattern as our own provider system. Worth noting because
durin's own `aux_models.audio` / `aux_models.vision` pattern parallels
this exactly — we could add `aux_models.embedding` with zero
conceptual novelty.

### Memory sub-agent + circuit breaker (the operational-safety piece)

```
Before main agent turn:
  spawn memory sub-agent
    │  tools: memory_recall only (or memory_search/memory_get for
    │         other providers)
    │  timeout: 15s default
    │  context: last N turns (configurable: message/recent/full modes)
    └─ inject result as <relevant-memories>...</relevant-memories>
       (XML, escaped, marked untrusted) into system prompt

Safety:
  - 3 consecutive timeouts → circuit breaker opens for 60s
  - In-memory result cache for 15s (dedupes repeated invocations)
  - If timeout mid-stream → partial result returned, no crash
```

**Effect**: the main loop never blocks on memory subsystem failures.
This is operational hygiene durin's current memory plan (doc 03)
doesn't address.

### Auto-capture (regex triggers + category detection)

```
Lifecycle hooks (onUserMessage, onAssistantMessage):
  for each message:
    if shouldCapture(text):
        # ~20 regex patterns (multilingual):
        #   "prefer", "remember", "siempre", "always", "I want", ...
        category = autoDetectCategory(text)
        # heuristics: "remember X" → fact; "I prefer Y" → preference;
        #             "decided to Z" → decision; "<entity>" → entity
        if len(text) < 500:
            embedAndStore(text, category)
```

Removes the need for the model to explicitly call a memory tool.
Captures user statements as side-effects of the conversation flow.

### Dreaming (the genuinely novel piece)

Cron-driven (default `0 2 * * *`). Spawns isolated sub-session.

**Multi-factor scoring** (`memory-core/src/dreaming.ts`,
`rankShortTermPromotionCandidates`):

For each candidate memory in short-term storage:

| Factor | What it measures |
|---|---|
| **Frequency** | How many recall events surfaced this memory |
| **Relevance** | Average similarity score across recall events |
| **Diversity** | Count of unique queries that surfaced it |
| **Recency** | Exponential decay (half-life days, configurable) |
| **Consolidation** | Age × usage (older + frequently used = high) |
| **Conceptual** | LLM-driven custom scoring (asks model "does this matter?") |

Sum (weighted) → top N (default 10) get promoted to durable `MEMORY.md`.
Promoted memories are removed from short-term logs.

**Why this works**: dreaming runs against **recall metadata** (which
memory was returned, when, for what query, at what similarity score) —
not just raw embeddings. So the consolidation is informed by HOW the
agent has actually been using the memory, not just what's in it. This
is the part doc 03 doesn't describe.

**Narrative phase** (optional, detached): a separate sub-agent
generates a "dream diary" summary of what was promoted. Stored in
`MEMORY_DREAMING_REPORT_*.md`. Pure side-effect; not used by retrieval.

### What OpenClaw does NOT have

- **No agent-built skills** (no equivalent of Hermes's `skill_manage`)
- **No provenance distinction** between agent-written and user-written memories (everything's just "memories")
- **No 3-tier system prompt structure** for cache friendliness (rebuilds each turn)
- **No prospective memory** (no equivalent of doc 03's "pending items")

---

## 4. Side-by-side

| Dimension | Original plan (doc 03) | Hermes | OpenClaw |
|---|---|---|---|
| **Storage primitive** | Graph + step nodes | Markdown files (per provider variants) | LanceDB vector table + MEMORY.md |
| **Retrieval** | Dynamic projection over graph | Provider-specific; usually semantic/text | Vector top-k (no hybrid) |
| **Write path** | Implicit (graph populated as agent runs) | background_review fork + explicit memory_save | auto-capture regex + explicit memory_store |
| **Consolidation** | Milestone accumulation | curator (inactivity-triggered, archives stale) | dreaming (cron, multi-factor scoring) |
| **Provenance** | Not specified | ContextVar-based, distinguishes agent vs user writes | Not present |
| **Active-learning loop** | Not present | background_review every turn + curator on inactivity | auto-capture per message + dreaming on schedule |
| **Operational safety** | Not addressed | Process-isolated fork; tool whitelist | Memory sub-agent with timeout + circuit breaker + caching |
| **Prospective memory** | Yes (pending items with triggers) | No | No |
| **Agent-built skills** | Not addressed | Yes — skill_manage create/edit/patch/delete | No |
| **System-prompt caching** | Not addressed | 3-tier with cache invalidation control | Not addressed |
| **Embedding strategy** | Not specified | Provider-delegated (some embed, some don't) | Pluggable provider (HTTP-based) |

The original plan is **conceptually richer** (entities, prospective
memory, graph structure) but **lighter on operational concerns** that
both reference systems have invested in.

---

## 5. Three synthesis options

> **Superseded by §0c.** This section is the design exploration that
> produced the consolidated architecture. Read §0c for the active
> design; this section explains the reasoning paths considered.

### Option A — Markdown-first minimalist (Hermes-shaped)

**Scope:**
- Storage: filesystem markdown only. Per-category subdirectories like Hermes.
- Write paths: explicit tools (`memory_store`, `skill_manage`) + background_review fork after each turn (Hermes pattern).
- Read paths: LLM-driven file selection (OpenClaude-style — give the model the file index, let it pick).
- Provenance: ContextVar-based, distinguishing agent_created vs user_authored.
- Consolidation: curator (inactivity-triggered) for agent_created skills.
- No vector index, no embeddings.

**Investment:** ~2 weeks.

**Strengths:**
- Zero new infrastructure dependencies (no vector DB, no embedding service).
- Files are human-editable — the user can fix bad memories by hand.
- Provenance system unblocks safe agent-driven knowledge accumulation.

**Weaknesses:**
- LLM-driven file selection scales poorly past ~50 files (the index becomes most of the prompt).
- No multi-factor consolidation; relies on inactivity-triggered cleanup which is reactive, not generative.
- No prospective memory.

### Option B — Full stack (OpenClaw-shaped)

**Scope:**
- Storage: LanceDB vector index + MEMORY.md durable file.
- Write paths: auto-capture regex triggers + explicit `memory_store` tool.
- Read paths: vector recall via memory sub-agent (with timeout + circuit breaker).
- Embedding: pluggable provider (likely default to a local Ollama embedding model since user runs Ollama already).
- Consolidation: dreaming cron with multi-factor scoring.
- No agent-built skills (would need to graft Hermes's `skill_manage` separately).

**Investment:** ~4–6 weeks.

**Strengths:**
- Genuinely scales to thousands of memories.
- Multi-factor dreaming is the most sophisticated consolidation pattern of the three systems.
- Memory sub-agent isolates failures from the main loop.

**Weaknesses:**
- New infrastructure: LanceDB + embedding provider config.
- Auto-capture vs explicit-only is a UX decision the user hasn't made yet.
- No provenance distinction between agent-driven and user-driven memories.
- No prospective memory.

### Option C — Hybrid phased (proposed)

Three internal phases. Each delivers visible value; can stop at any
phase boundary without leaving the system half-built.

**Phase 1 — Markdown + provenance + background_review** (~2 weeks)
- Filesystem markdown memory (categories: user, project, feedback, reference — same shape as the existing auto-memory pattern in `MEMORY.md`).
- ContextVar provenance (`agent_created` vs `user_authored`).
- background_review fork after each turn — split into two sub-steps internally: (a) `extract_candidates` identifies signals worth keeping (preferences, decisions, corrections, project facts); (b) `cognify_to_memory` normalises + dedupes + writes to filesystem with `agent_created=true`. Each sub-step is independently testable, and the boundary lets us run a cheap model for extraction and a smarter one for cognify when worthwhile. Inspired by cognee's `extract → cognify → improve` pipeline structure.
- curator (inactivity-triggered) for `agent_created` cleanup.
- LLM-driven file selection for retrieval.
- Tools: `memory_store`, `memory_search` (LLM-driven), `skill_manage` (Hermes-style create/edit/patch).
- **Value delivered**: active-learning loop. Agent grows its own knowledge across sessions. Cache friendly via 3-tier prompt.

**Phase 2 — LanceDB + memory sub-agent + circuit breaker** (~2 weeks)
- Add LanceDB vector index alongside (NOT replacing) markdown. Markdown remains source of truth + human-editable.
- Embedding provider: configurable, default = local Ollama (free, already installed). Plug into our existing `aux_models` pattern.
- Memory sub-agent as the pre-turn step: vector recall → top-k → injected as `<relevant-memories>`.
- Timeout + circuit breaker + 15s cache (operational safety).
- **Value delivered**: scales past ~50 memories. Main loop isolated from memory subsystem failures.

**Phase 2c — TEMPR-style multi-strategy retrieval (user-configurable, ~1 week if activated)**

Optional refinement layered on top of Phase 2b. **Off by default — user
opt-in via config**, not gated on internal metrics. Inspired by
Hindsight's TEMPR retrieval. Rationale: vector-only is sufficient for
typical early corpus sizes; the user enables additional strategies when
their workload or corpus shape calls for it.

Config knob (`durin/config.json` or equivalent):

```yaml
memory:
  retrieval:
    strategies:
      vector: true        # always on (Phase 2b)
      bm25: false         # opt-in: lexical keyword match
      temporal: false     # opt-in: time-window weighting
      keyword_llm: false  # opt-in: LLM query rewriting (costly)
    fusion: reciprocal_rank   # used when ≥2 strategies active
```

What each strategy contributes:

- **`bm25`** — catches exact-symbol queries vector misses (`foo_bar`,
  paths, IDs, command names). New dep (`bm25s` pure-Python or
  `tantivy` Rust bindings); parallel index alongside the LanceDB
  store. ~50 KB extra storage per memory.
- **`temporal`** — time-window weighting for "what did we discuss
  yesterday?" style queries. No new dep; cheap.
- **`keyword_llm`** — LLM-driven query rewriting before vector search.
  Adds one small LLM call per recall. Only enable if other strategies
  are documented to miss recurringly.

When ≥2 strategies are active, results merge via **Reciprocal Rank
Fusion** — standard algorithm, no extra LLM call.

The user can flip these toggles independently after observing in
`memory.recall` telemetry which queries their setup misses. Defaults
stay off to avoid charging users for capability they may never need.

**Phase 3 — Dreaming with multi-factor scoring (cross-cutting over memory + skills)** (~1–2 weeks)
- Recall metadata logging (which memory or skill, when, for which query, what score).
- Dreaming cron: rank short-term candidates by frequency / relevance / diversity / recency / consolidation / conceptual.
- **Cross-cutting promotion path**: the same scoring + ranking applies to BOTH memory entries (OpenClaw pattern) AND agent-created skills (Hermes pattern). One scheduled process touches both subsystems via the provenance flags introduced in Phase 1. See §5b below for why this matters.
- Promotion / archival / patching: top-N memories promoted to durable layer; agent-created skills that became stale get archived or patched in place; pruning of unused short-term entries in both.
- **Freshness trends as a consolidation output**: each entry touched by Dream gains a `freshness` label — `stable` / `strengthening` / `weakening` / `stale` — derived from the trajectory of its recall metadata across runs (increasing recall count + similarity → strengthening; flat → stable; decreasing → weakening; zero recalls in N runs → stale). Inspired by Hindsight. Surfaces in `memory.recall` telemetry; informs both the curator (auto-archive `stale` agent_created entries) and the user (visible signal of which memories are earning their keep).
- Optional narrative phase (LLM-generated diary).
- **Value delivered**: consolidation informed by actual usage across the agent's whole "self" (declarative memory + procedural skills), not separate processes per subsystem.

**Phase 4 — Dynamic skills (agent-built skills with full lifecycle)** (~2 weeks)
- Tools for the agent to author/refine skills mid-session (`skill_manage create/edit/patch/delete`, Hermes pattern).
- Provenance via the same ContextVar mechanism from Phase 1 — `agent_created` vs `user_authored` so the dreaming process from Phase 3 only auto-manages the agent's own.
- Skill schema: YAML frontmatter (`name`, `description`, `platforms`, `disable_model_invocation` — already exists in durin) + markdown body + optional `references/`, `templates/`, `scripts/` subdirs (Hermes layout).
- Skills index integration into the system prompt's stable tier (cache-friendly).
- **Value delivered**: agent learns procedural knowledge as a side-effect of doing work, not just declarative facts about the user.
- **Why after Phase 3, not before**: Phase 3 produces the consolidation mechanism that makes dynamic skills sustainable — without it, agent-authored skills accumulate forever with no maintenance loop. The user explicitly flagged this sequencing.

**Optional Phase 5 — Prospective memory** (~2 weeks)
The one thing from the original plan that neither Hermes nor OpenClaw
has and that doc 03 was right to emphasise: pending items with triggers
(time, entity, condition). Could be a follow-up once Phases 1–4 have
demonstrated the foundation is healthy.

**Why phased over A or B straight:**
- Each phase ships value alone — the system isn't useless at the end of Phase 1, and Phase 2 isn't blocked on Phase 3.
- Decision points after each phase: if Phase 1 gives 80% of the user-visible value, Phase 2 may not be worth the infrastructure cost.
- Phases 2 and 3 are independent — could be reordered if vector retrieval turns out less important than consolidation (or vice versa).

---

## 5b. Cross-cutting concern — consolidation spans memory AND skills

A subtle point worth surfacing explicitly: the two reference systems
split the consolidation problem differently:

- **Hermes** consolidates **skills** (curator: archive stale agent-built
  skills, background_review: patch / extend existing skills as workflow
  evolves). Hermes does *not* consolidate memories — that's left to each
  memory provider's own internal logic.
- **OpenClaw** consolidates **memories** (dreaming: rank short-term
  entries, promote top-N to durable). OpenClaw does *not* have skills
  at all.

For durin, **both subsystems exist and both need consolidation**. A
single consolidation process should operate cross-cuttingly over both —
not two parallel cron jobs with separate scoring logic.

This is why **Option C's Phase 3 is described as "cross-cutting"**: the
scoring (frequency / relevance / diversity / recency / consolidation /
conceptual) applies the same way to a memory candidate as to a skill
candidate. The decision the process makes per candidate is one of:

| Candidate | Outcome |
|---|---|
| Memory, high score, durable already | leave |
| Memory, high score, still short-term | promote to durable |
| Memory, low score, agent_created, age > threshold | archive |
| Memory, low score, user_authored | leave (never touched) |
| Skill, frequently invoked, agent_created | patch (incorporate new signals) |
| Skill, never invoked, agent_created, age > threshold | archive |
| Skill, frequently invoked, user_authored | leave (signals captured separately) |
| Skill, never invoked, user_authored | leave (user's choice) |

**Sequencing implication**: Phase 3 must be designed (not necessarily
fully implemented) before Phase 4 ships, because Phase 4 introduces
dynamic skills whose lifecycle depends on Phase 3's consolidation. The
inverse — implementing dynamic skills first and then bolting
consolidation on later — risks a backlog of agent-authored junk skills
that the user has to clean by hand. (User flagged this explicitly:
"todavía no tocamos el sistema de skills dinámicas, pero creo que
podemos hacer eso luego de tener memoria".)

**Memory comes first in the implementation order**, but the
consolidation infra in Phase 3 is built **knowing that Phase 4 skills
will plug into the same scoring**. Concretely: Phase 3's data model
should treat `memory` and `skill` as two variants of a `consolidatable`
record type, not two unrelated stores.

---

## 5c. Resource cost per phase

> **Superseded by §0c.9 (phase mapping) and §0c.3 (model choice per
> event).** This section's per-turn cost analysis predates the
> consolidation that moved derivative work from per-turn
> `background_review` to once-a-day dream. The numbers below are
> historical; the active picture is that Phases 1 + 2 add **zero**
> extra LLM calls per turn — only dream (daily) and user-triggered
> `memory_ingest` cost extra calls.

Concrete operational footprint so the horizon decision is informed by
actual cost, not just feature lists. Numbers are per-turn unless noted.

| Phase | Extra LLM calls / turn | Extra latency (user-facing) | Storage | Extra RAM |
|---|---|---|---|---|
| **1** Markdown + background_review | +1 (async, non-blocking) | 0 | KB/memory | ~0 |
| **2** LanceDB + memory sub-agent | +1 (pre-turn, blocks until result or timeout) | up to `timeout` (default 15 s, cached 15 s) | ~6 KB/memory (1536-dim embedding) + markdown | ~500 MB if local embedding model loaded; 0 if HTTP |
| **2c** TEMPR strategies (opt-in) | 0 per added strategy, except `keyword_llm` which is +1 | < 100 ms (index reads are µs) | ~50 KB/memory for BM25; nothing for temporal | ~0 |
| **3** Dreaming cron | 0 per turn (cron-driven) | 0 | Same as 1+2 | RAM spikes only during the dream run |
| **4** Dynamic skills | 0 per turn (tool-driven) | 0 | KB/skill | 0 |
| **5** Prospective memory | 0 per turn (trigger-driven) | 0 | KB/item | 0 |

**Combined per-turn cost (Phases 1 + 2 active)**: ~3 LLM calls per turn
instead of 1. Up to ~3x model cost per turn at face value, with the
following mitigations available:

- **Auxiliary calls use a cheap model.** `background_review` and the
  `memory_sub_agent` run on Haiku 4.5 or a local model; Sonnet/Opus
  stays on the main turn only. This alone cuts the delta from ~3x to
  ~1.3x.
- **Throttle `background_review`.** Skip on trivial turns (no tool
  calls, < N tokens of response). Estimated reduction: ~50% of the
  per-turn +1.
- **Cache `memory_sub_agent` results 15 s** (OpenClaw pattern). Avoids
  redundant recalls on consecutive related turns.
- **Circuit breaker on `memory_sub_agent`.** 3 consecutive failures →
  60 s offline. Failures never cascade into the main loop.

**Local-friendly path**: if Ollama is running locally, embedding cost
is $0 and ~10 ms per query. If the user prefers cloud (e.g.,
`text-embedding-3-small` at $0.02 per 1M tokens), embedding cost is
effectively free at typical conversation volumes.

**Storage worst case** (100 memories, both indexes active):

- Markdown source: ~50 KB
- LanceDB embeddings: ~600 KB
- BM25 index (if 2c enabled): ~5 MB
- Total: ~6 MB. Negligible.

**Bottom line**: the cost lives in the model bill, not in latency or
disk. The single biggest lever is **which model runs the auxiliary
calls**. Mitigated, the per-turn cost delta vs today is in the ~1.3x
range. Unmitigated (auxiliaries on the main model), it's ~3x.

---

## 6. Open questions for review

Before any horizon is picked, these are the points worth weighing:

### Architecture questions

1. **Do we need vector retrieval at all in V1?** OpenClaude proves you don't, with markdown + LLM-driven file selection. Counter-argument: agents accumulate ~10–100 memories per active week; past 200 the file-listing prompt becomes ineffective. When does that threshold hit for daily-driver use?

2. **Auto-capture: opt-in or default?** OpenClaw defaults it on. Hermes doesn't have it (relies on background_review fork only). Auto-capture is invisible to the model — saves tool calls but also captures noise. Worth piloting opt-in first?

3. **Memory sub-agent (pre-turn) vs inline tool calls?** OpenClaw runs a separate sub-agent BEFORE each turn. Hermes lets the main agent call memory tools inline. Pre-turn = consistent, slower; inline = on-demand, less coverage. Which fits durin's typical session pattern better?

4. **Prospective memory: V1 or later?** Doc 03's pending-items-with-triggers is genuinely novel vs both reference systems. Worth shipping in the first cut, or wait until the basic memory layer is proven?

5. **Skills as memory or separate?** Hermes uses `skill_manage` as a tool that writes to a skills directory; conceptually they're separate from "memories" (which are user facts). OpenClaw doesn't have skills at all. Should durin unify them or keep them apart? (My instinct: keep apart at the storage level — memories under `memory/`, skills under `skills/` — but **unified at the consolidation level**: same dreaming/curator process scores both. This is what §5b describes.)

5b. **Consolidation scope.** If dreaming/curator operates over both memories AND skills (§5b), should it ALSO operate over the meta timeline events (`type=plan`, `type=tool_call`) durin already persists? The current meta timeline is append-only history with no pruning. Worth deciding whether memory consolidation extends there or stays scoped.

### Operational questions

6. **Storage backend for vectors** (Phase 2 of Option C). LanceDB is the OpenClaw choice — embedded, no daemon. Alternatives: SQLite + sqlite-vec extension; Qdrant local; ChromaDB. LanceDB has the lightest infra; chroma is more popular; sqlite-vec is the most portable. Worth deciding before implementation, not during.

7. **Default embedding model**. We have Ollama installed already. Embedding models available: `nomic-embed-text` (general, 768 dims, fast), `mxbai-embed-large` (1024 dims, slower, better English), `bge-m3` (1024 dims, multilingual). Or stay HTTP-only with OpenAI/Voyage. Local-default vs cloud-default is a recurring design choice in durin (we've defaulted local where possible).

8. **How does memory interact with session metadata?** durin already persists `<session>.meta.json` with `type=tool_call` and `type=plan` events. Should memory events appear there too? Probably yes — same timeline = easier debugging.

9. **Provenance scope**. Hermes's ContextVar pattern distinguishes `assistant_tool` from `background_review`. Do we need more granularity (per-skill-category, per-tool, per-channel)? Probably no for V1; flagging the question.

10. **Curator vs dreaming — same purpose, different triggers**. Hermes runs curator on inactivity (≥7 days). OpenClaw runs dreaming on cron (default daily). Different mental models: curator = "agent looks back when it has free time"; dreaming = "scheduled background hygiene". Which fits durin's deployment patterns better?

### Boundary questions

11. **Memory across workspaces or per-workspace?** durin sessions are workspace-scoped today. Should memory follow that, or be cross-workspace by default? Most users want some memory cross-workspace (user preferences) and some per-workspace (project facts).

12. **Memory + plan-mode interaction**. The plan-mode hardening earlier this month established that in plan mode the agent is read-only. Should memory writes also be blocked in plan mode? (Argument for yes: plan mode is "no side effects". Argument for no: capturing user preferences during planning is harmless.)

13. **Privacy / opt-out granularity**. Per-session toggle (OpenClaw) is the minimum. Per-channel? Per-message ("don't remember this")? Worth deciding the surface before users start asking.

---

## 7. What this doc does NOT do

- **No implementation roadmap.** Roadmap follows from horizon choice.
- **No commitments.** Every choice in §5 and §6 is open.
- **No code.** No PRs, no scaffolding, no infrastructure.
- **No deletion of `docs/03_memory_design.md`.** That doc is the historical "what we thought before evidence" record and stays in `docs/` (not archived) for now. If we pick a path that diverges meaningfully, we'll either rewrite 03 or move it to `archive/` with a pointer back.

---

## 8. Next step

Marcelo reads this point-by-point. Comments / corrections go into a new
bitácora entry (`02_bitacora.md`). Once horizon is chosen (Option A, B,
C, or some variant), the corresponding implementation roadmap is added
to `01_roadmap.md` as a new horizon section with phase boundaries +
acceptance criteria.

**No work proceeds before the horizon decision.**

---

## Last updated: 2026-05-20 (consolidated §0c added)
