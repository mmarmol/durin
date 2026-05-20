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
- background_review fork after each turn → writes go to filesystem with `agent_created=true`.
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

**Phase 3 — Dreaming with multi-factor scoring (cross-cutting over memory + skills)** (~1–2 weeks)
- Recall metadata logging (which memory or skill, when, for which query, what score).
- Dreaming cron: rank short-term candidates by frequency / relevance / diversity / recency / consolidation / conceptual.
- **Cross-cutting promotion path**: the same scoring + ranking applies to BOTH memory entries (OpenClaw pattern) AND agent-created skills (Hermes pattern). One scheduled process touches both subsystems via the provenance flags introduced in Phase 1. See §5b below for why this matters.
- Promotion / archival / patching: top-N memories promoted to durable layer; agent-created skills that became stale get archived or patched in place; pruning of unused short-term entries in both.
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

## Last updated: 2026-05-20
