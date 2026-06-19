# Durin — Operational Architecture

> Reference for Durin's internals: what each system does and how it fits together.
> **Keep updated** when modifying core modules.
>
> For the *direction*, *discarded approaches*, and *pending work*, see [roadmap.md](../roadmap.md).

This is the top-level index. Component-level architecture lives alongside this
file (plus the `memory/` and `skills/` subfolders) to keep each surface scannable:

| Document | Scope |
|---|---|
| [loop.md](loop.md) | Iteration flow, runner guards, hooks, agent modes, long tasks, sessions, providers, sandboxing |
| [memory/](memory/00_overview.md) | Entity-centric memory, dream consolidation, alias + vector indexes, retrieval, drill-down, absorption, memory graph |
| [skills/](skills/00_overview.md) | Skill discovery, three-tier surfacing, searchable skill pseudo-class, import + security floor |
| [observability.md](observability.md) | Telemetry, status, doctor, gateway daemon |
| [api.md](api.md) | Service core, unified ASGI gateway, OpenAPI contract, persisted auth |
| [ux.md](ux.md) | Interactive CLI, Textual TUI, secrets subsystem, design system, lifecycle commands, config layout, distribution |

---

## 1. Origin and relationship with Nanobot

Durin is a fork of Nanobot (lightweight agent framework). After the May 2026 prune, Durin is essentially Nanobot plus a small set of plumbing additions:

| Addition | What it provides |
|---|---|
| `providers/local_llama_provider.py` | Local LLM provider via `llama-cpp-python` |
| `telemetry/` | Generic JSONL logger + rate-limit telemetry |
| `durin_sdk.py` | Public SDK entry point (`Durin.from_config()`) |
| `memory/` | Entity-centric memory: typed entries, dream consolidator, alias + vector indexes, absorption — see [memory/00_overview.md](memory/00_overview.md) |
| `cli/memory_cmd.py` | `durin memory <subcommand>` for consolidation + drill-down |

What Durin no longer carries: a previous "smart layer" (posture vector, plan tier system, deliberation V3, phase-aware temperatures, hook factory) was empirically refuted across V3–V8 experiments and removed. See `roadmap.md` ("What we are explicitly NOT doing") and `git log` for the rationale.

The fork model is retained because the memory work needs tighter integration than a plugin API allows.

---

## 2. Module map

```
durin/
├── agent/
│   ├── loop.py            # AgentLoop — outer state machine, dispatch, sessions
│   ├── runner.py          # AgentRunner — inner LLM/tool loop (see loop.md)
│   ├── hook.py            # AgentHook + AgentHookContext + CompositeHook
│   ├── context.py         # ContextBuilder — system prompt + history + skills
│   ├── memory.py          # LEGACY MemoryStore + Dream over MEMORY.md / SOUL.md
│   ├── agent_mode.py      # Plan/Build/Explore permission-as-data modes
│   ├── progress_hook.py   # Streaming + tool-event progress + cache.usage event
│   ├── subagent.py        # Spawn parallel sub-agents + lifecycle status retention
│   ├── model_presets.py   # Named model + generation parameter sets
│   ├── skills.py          # Skill discovery, on-demand loading
│   ├── skill_registry.py / skill_resolve.py / skill_lifecycle.py   # Skills subsystem
│   ├── skill_acquire.py / skill_curation.py / skill_drift.py / skill_usage.py
│   ├── skills_import.py / skills_store.py / skills_surface.py / skills_frontmatter.py
│   └── tools/             # All tool implementations
│       ├── filesystem.py / search.py / shell.py / web.py / mcp.py / spawn.py
│       ├── cron.py / long_task.py / message.py / self.py / notebook.py
│       ├── plan_mode.py            # enter_plan_mode + exit_plan_mode
│       ├── todos.py                # todo_write (replace-list semantics)
│       ├── sleep.py                # bounded synchronous wait
│       ├── ask_user.py             # ask_user_question (yield-and-resume)
│       ├── session_search.py       # keyword/regex over session.messages
│       ├── subagent_lifecycle.py   # list / status / stop / output / monitor
│       ├── interpret_image.py      # vision aux-model bridge
│       ├── interpret_audio.py      # audio chat-multimodal aux-model bridge
│       ├── memory_ingest.py        # entity-centric: copy artifact, return content
│       ├── memory_upsert_entity.py # entity-centric: write/merge entity page + vector upsert
│       ├── memory_search.py        # entity-centric: vector + entity-aware rerank
│       ├── memory_drill.py         # entity-centric: resolve path#anchor
│       ├── memory_forget.py        # entity-centric: tombstone + deindex
│       ├── memory_store.py         # DISABLED in agent toolset (internal callers only)
│       ├── repo_overview.py / output_spill.py
│       └── context.py              # ToolContext + AuxProviderHandle
├── api/                   # HTTP/SSE/WebSocket transport layer
├── bus/                   # Internal message bus (InboundMessage, OutboundMessage)
├── channels/              # CLI, WebUI, Slack, Telegram, etc.
├── cli/                   # CLI entry, prompts, command dispatch
│   ├── commands.py        # Lifecycle (onboard, status, ...)
│   ├── config_cmd.py      # durin config get/set/edit
│   ├── doctor.py          # health checks
│   ├── memory_cmd.py      # durin memory dream/history/show/diff/.../absorb
│   ├── tui/               # Textual TUI app
│   └── ...
├── command/               # /commands router (/plan, /build, /mode, ...)
├── config/                # Config schemas, loader, validation
├── cron/                  # Scheduled task service
├── heartbeat/             # Background heartbeats and timers
├── memory/                # Entity-centric memory subsystem (see memory/00_overview.md)
│   ├── schema.py          # MemoryEntry pydantic model
│   ├── entities.py        # <type>:<value> validation + SUGGESTED_TYPES
│   ├── entity_page.py     # EntityPage parser (open-vocab frontmatter)
│   ├── storage.py         # split_frontmatter + save_entry + load_entry
│   ├── store.py           # store_memory (internal callers: compaction, ingest chunks)
│   ├── ingestion.py       # ingest_artifact
│   ├── memory_writer.py   # shared write path (tools + dream)
│   ├── forget.py / deletion.py     # tombstone + deindex
│   ├── drill.py / search.py        # drill-down URI resolve + grep fallback
│   ├── search_pipeline.py # run_search_pipeline — composes the stages below
│   ├── query_router.py    # query analysis / routing
│   ├── fts_index.py / lexical_search.py   # FTS5 lexical layer
│   ├── embedding.py       # FastembedProvider (lazy ONNX)
│   ├── vector_index.py    # LanceDB-backed VectorIndex
│   ├── rrf_fusion.py      # reciprocal-rank fusion
│   ├── entity_ranker.py   # RRF entity-aware reranker
│   ├── cross_encoder.py   # optional cross-encoder rerank (opt-in)
│   ├── indexer.py / index_meta.py  # index build + metadata
│   ├── file_watcher.py    # workspace change watcher
│   ├── health_check.py    # FTS/Lance/CE probes for `durin doctor`
│   ├── graph.py / graph_api.py     # memory graph (typed edges, phantom nodes)
│   ├── aliases_index.py   # AliasIndex (rebuild-only, lazy)
│   ├── aliases_cache.py   # process-wide shared cache
│   ├── always_on_dream.py / dream_passes.py    # dream cold path (5 passes: extract/derived_from/skill/refine/always_on)
│   ├── extract_dream.py / refine_dream.py / derived_from_dream.py   # dream pass stages
│   ├── reference.py        # references as first-class graph nodes (derived_from edges)
│   ├── consolidator_tags.py # parse summary/entities/topics from consolidator
│   ├── absorb_judge.py    # LLM-judge for auto-absorb
│   ├── absorption.py      # EntityAbsorption (merge + archive + deindex)
│   ├── skill_page.py      # skills as a searchable memory pseudo-class
│   ├── provenance.py      # _MEMORY_AUTHOR ContextVar
│   ├── paths.py           # workspace-scoped directory helpers
│   ├── session_md.py      # <key>.jsonl → <key>.md formatter
│   └── hot_layer.py       # identity + top headlines for stable prompt tier
│   # (representative — memory/ has ~50 modules; see memory/00_overview.md)
├── pairing/               # Account pairing flow
├── providers/             # LLM provider adapters (see loop.md §6)
├── security/              # Auth, permissions, network SSRF guard
│   └── secrets.py         # secret store + ${secret:} refs + redaction
├── session/               # Session storage + state helpers (see loop.md §5)
├── skills/                # Built-in skill markdown files
├── telemetry/             # Generic JSONL logger (see observability.md)
├── templates/             # Prompt templates
├── utils/                 # Helpers (no business logic)
│   └── git_repo.py        # GitRepo (dulwich) used by entity-centric memory
└── web/                   # Static web assets

scripts/
├── refresh_model_capabilities.py    # dev tool — regenerates capability snapshot
└── _vendor_sources.py               # Anthropic / Mistral / Gemini adapters
```

---

## 3. Iteration entry point

```mermaid
flowchart LR
    U["User input<br/>(CLI / TUI / Channel)"] --> CH["Channel adapter"]
    CH --> BUS["MessageBus<br/>InboundMessage"]
    BUS --> LOOP["AgentLoop._dispatch"]
    LOOP --> RUN["AgentRunner.run<br/>(see loop.md)"]
    RUN --> SESS["sessions/KEY.jsonl<br/>append-only"]
    RUN --> TOOLS["Tools<br/>(filesystem, memory, ...)"]
    TOOLS --> MEM["Memory subsystem<br/>(see memory/00_overview.md)"]
    RUN --> OUT["OutboundMessage"]
    OUT --> CH
```

Channel-agnostic. The CLI, TUI, web, Slack, Telegram, Matrix, WhatsApp, DingTalk and MoChat surfaces all funnel through the same `MessageBus` and `AgentLoop`. The only thing that differs between channels is the I/O layer; agent behaviour is identical.

---

## 4. Testing

```
tests/
├── agent/          # Loop, runner, context, hooks, modes, capability bridges
├── agent/tools/    # Per-tool tests
├── api/            # HTTP/SSE/WebSocket
├── bus/            # Message bus
├── channels/       # Channel adapters
├── cli/            # CLI rendering + TUI pilot tests
├── command/        # Commands (/plan, /build, /mode, ...)
├── config/         # Schema and loader
├── cron/           # Cron service + cron tool update action
├── integration/    # End-to-end: phase 6 memory outcomes, etc.
├── memory/         # Memory subsystem (schema, dream, vector, ranker, absorption, T1 wiring E2E)
├── providers/      # Provider adapters + capabilities resolver + snapshot
├── session/        # Session lifecycle, goal state, todo_state, session_meta
├── skills/         # Skill loading + disable_model_invocation gating
└── telemetry/      # Generic logger + schema catalog + cache.usage event
```

Current: **~5660 tests** (Python, `def test_` count) + **~140** (webui).

**Instance isolation:** an autouse `conftest` fixture runs every test in a
throwaway `DURIN_HOME` (a per-test temp dir), so the suite never touches
`~/.durin` — no collision with a running daily daemon, hermetic under any
ambient `DURIN_HOME`.

---

## 5. Home & instances (`DURIN_HOME`)

durin is **multi-instance**. An instance is a self-contained data root selected
by the `DURIN_HOME` env var (unset → `~/.durin`); see
[durin/config/home.py](../../durin/config/home.py). Everything that is instance
state is relative to that root: config (incl. ports), `secrets.json` (incl.
OAuth tokens via the secret store), the memory store + vector index, sessions,
workspace, and runtime (telemetry/logs/cron/media/webui).

- **Daily/release** = unset (`~/.durin`); **dev** = a second `DURIN_HOME`;
  **tests** = throwaway temp homes.
- Two gateways run side-by-side by giving each instance distinct ports in its
  config. Configured ports are authoritative — never moved automatically; a
  genuinely-taken port (another active listener) makes the daemon exit with
  clear guidance rather than crash or silently relocate
  ([durin/utils/net.py](../../durin/utils/net.py) `port_is_available`).
- The only things outside an instance root are immutable shared caches that are
  not instance state — the embedding model-weights cache (`~/.cache/huggingface`)
  and the package code/bundled webui.

---

## Last updated: 2026-06-19 (DURIN_HOME multi-instance model: per-test isolation, port auto-pick, instance contract)

> Doc history: this used to be a single 1000-line file. May 23, 2026 split it
> into per-component docs (siblings of this index, plus the `memory/` and
> `skills/` subfolders) with mermaid diagrams. The slim top-level keeps the
> module map and origin story; details moved.
>
> For the *why* behind each subsystem (what was tried, what was discarded),
> see `roadmap.md` and `git log`. This document only describes the current state.
