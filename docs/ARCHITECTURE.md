# Durin — Operational Architecture

> Reference for Durin's internals: what each system does and how it fits together.
> **Keep updated** when modifying core modules.

For the *direction* and *discarded approaches*, see `01_roadmap.md` and `02_bitacora.md`.

---

## 1. Origin and Relationship with Nanobot

Durin is a fork of [Nanobot](vendor/nanobot/) (lightweight agent framework). After the May 2026 prune, Durin is essentially Nanobot plus a small set of plumbing additions:

| Addition | What it provides |
|---|---|
| `providers/local_llama_provider.py` | Local LLM provider via `llama-cpp-python` |
| `telemetry/` | Generic JSONL logger + rate-limit telemetry |
| `durin_sdk.py` | Public SDK entry point (`Durin.from_config()`) |

What Durin no longer carries: a previous "smart layer" (posture vector, plan tier system, deliberation V3, phase-aware temperatures, hook factory) was empirically refuted across V3–V8 experiments and removed. See `02_bitacora.md` for full rationale and `06_log_experiments.md` for raw data.

The fork model is retained because future memory work (per `03_memory_design.md`) is expected to require tighter integration than a plugin API allows.

---

## 2. Iteration Flow

```
┌─────────────────────────────────────────────────────────────┐
│                    AgentRunner.run()                          │
│  for iteration in range(max_iterations):  [default: 200]     │
│                                                              │
│  1. Context governance (microcompact, snip, budget)          │
│  2. Build AgentHookContext(iteration, messages)              │
│  3. hook.before_iteration(context)                           │
│  4. LLM request → response                                   │
│  5. Parse response (tool_calls, content, reasoning)          │
│  6. If tool_calls:                                           │
│     a. hook.before_execute_tools(context)                    │
│     b. Execute tools (sequential or concurrent)              │
│     c. Append tool results to messages                       │
│  7. hook.after_iteration(context)                            │
│  8. If no tool_calls → final_content → break                │
└─────────────────────────────────────────────────────────────┘
```

The hook surface (`before_iteration`, `before_execute_tools`, `after_iteration`, plus streaming hooks) is intentionally generic. No hooks are bundled at present. New hooks (e.g. an `ExecutionTelemetryHook` for tracking iterations/tokens/tools) should attach via the standard `AgentHook` interface.

---

## 3. Module Map

```
durin/
├── agent/
│   ├── loop.py            # AgentLoop — outer state machine, dispatch, sessions
│   ├── runner.py          # AgentRunner — inner LLM/tool loop
│   ├── hook.py            # AgentHook + AgentHookContext + CompositeHook
│   ├── context.py         # ContextBuilder — system prompt + history + skills
│   ├── memory.py          # MemoryStore — markdown files + dream/consolidator
│   ├── autocompact.py     # Auto-compaction at session boundaries
│   ├── progress_hook.py   # Streaming + tool-event progress reporting
│   ├── subagent.py        # Spawn parallel sub-agents
│   ├── model_presets.py   # Named model + generation parameter sets
│   ├── skills.py          # Skill discovery, on-demand loading
│   └── tools/             # All tool implementations (filesystem, exec, web, mcp, etc.)
├── api/                   # HTTP/SSE/WebSocket transport layer
├── bus/                   # Internal message bus (InboundMessage, OutboundMessage)
├── channels/              # CLI, WebUI, Slack, Telegram, etc.
├── cli/                   # CLI entry, prompts, command dispatch
├── command/               # /commands router
├── config/                # Config schemas, loader, validation
├── cron/                  # Scheduled task service
├── heartbeat/             # Background heartbeats and timers
├── pairing/               # Account pairing flow
├── providers/             # LLM provider adapters
├── security/              # Auth, secrets, permissions
├── session/               # Session storage + goal-state tracking
├── skills/                # Built-in skill markdown files
├── telemetry/             # Generic JSONL logger
├── templates/             # Prompt templates
├── utils/                 # Helpers (no business logic)
└── web/                   # Static web assets
```

---

## 4. Hooks System

`durin/agent/hook.py` defines the generic interface every hook implements:

```python
class AgentHook:
    async def before_iteration(self, context: AgentHookContext) -> None
    async def before_execute_tools(self, context: AgentHookContext) -> None
    async def after_iteration(self, context: AgentHookContext) -> None
    async def on_stream(...) / on_stream_end(...) / emit_reasoning(...)
    def finalize_content(self, context, content) -> str | None
```

`AgentHookContext` exposes `iteration`, `messages`, `response`, `usage`, `tool_calls`, `tool_results`, `tool_events`, `streamed_content`, `final_content`, `stop_reason`, `error`. Mutating `messages` is the supported way to inject system messages mid-turn (used by Nanobot's `AutoCompact` and `Consolidator` hooks).

`CompositeHook` fans out to a list of hooks with per-hook exception isolation, so a faulty third-party hook can't crash the loop.

No hooks are wired in by default after the prune. Future memory work and task-aware context selection will attach hooks here.

---

## 5. Telemetry

`durin/telemetry/logger.py` provides:
- `TelemetryLogger` — append-only JSONL writer with event-count cap
- `log(event_type, data)` — generic event emission
- `log_rate_limit(...)` / `log_rate_limit_exhausted(...)` — provider rate-limit signal
- `get_session_logger(session_key)` — date-suffixed per-session log files in `~/.cache/durin/telemetry/`

The logger is connected to the provider layer (`provider.set_telemetry()` in `AgentLoop.from_config`) for rate-limit events. No other module emits telemetry by default. New trackers should call `logger.log(...)` with their own event type.

---

## 6. Long Tasks and Goal State

`durin/agent/tools/long_task.py` defines `LongTaskTool` (register an objective) and `CompleteGoalTool` (close it with a recap). Goal state is stored in `session.metadata[GOAL_STATE_KEY]` and mirrored into the runtime-context block each turn via `durin/session/goal_state.py`.

After the prune, `complete_goal` no longer consults any plan-tier verification gate. It only requires that a goal is currently active.

---

## 7. Sessions and Persistence

`durin/session/manager.py` handles session lifecycle. Each session is a JSON file containing message history, metadata, and goal state. `MemoryStore` (`durin/agent/memory.py`) handles long-lived markdown memory files (`MEMORY.md`, etc.) consumed by `ContextBuilder` into the system prompt.

`AutoCompact` (`durin/agent/autocompact.py`) and `Consolidator` (in `durin/agent/memory.py`) summarize old turns to keep context within budget.

---

## 8. Sandboxing

Tool execution is sandboxed via `durin/agent/tools/sandbox.py`. Three backends:
- `bwrap` — Linux namespace sandbox (production)
- `docker` — Docker container (for benchmark-style isolation, see registration helpers)
- `testbed` — conda-env wrapper for running inside benchmark containers

The agent's exec tool routes through `wrap_command(sandbox, command, workspace, cwd)`.

---

## 9. Providers

`durin/providers/` ships adapters for Anthropic, OpenAI-compat (incl. Z.ai, OpenRouter, Azure), Bedrock, GitHub Copilot, local llama-cpp, OpenAI Codex, and a fallback wrapper. `factory.make_provider(config)` resolves the active provider/model from config + presets.

---

## 10. Testing

```
tests/
├── agent/          # Loop, runner, context, autocompact, tools
├── api/            # HTTP/SSE/WebSocket
├── bus/            # Message bus
├── channels/       # Channel adapters
├── cli/            # CLI rendering
├── command/        # Commands
├── config/         # Schema and loader
├── providers/      # Provider adapters
├── session/        # Session lifecycle and goal state
├── skills/         # Skill loading
└── telemetry/      # Generic logger (no posture/deliberation tests after prune)
```

Total: **3,052 tests passing, 15 skipped** after the prune.

---

## Last updated: 2026-05-18
