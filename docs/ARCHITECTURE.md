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
│  4. _build_request_kwargs — optional context_transform hook  │
│     mutates the message list right before the provider call  │
│  5. LLM request → response (usage.prompt_tokens captured     │
│     and stamped on the assistant message as the anchor for   │
│     future token estimates)                                  │
│  6. Parse response (tool_calls, content, reasoning)          │
│  7. If tool_calls:                                           │
│     a. hook.before_execute_tools(context)                    │
│     b. Topological batching (1B): consecutive concurrency-   │
│        safe tools run in parallel, mutations & exclusives    │
│        get singleton batches in original order               │
│     c. Per-call loop detection (1A): identical (name,args)   │
│        signature that already failed this turn is short-     │
│        circuited with a synthetic "BLOCKED" tool result      │
│     d. _run_tool_timed wraps every execution: stamps         │
│        tool_call_id + duration_ms on the event, used later   │
│        by _save_turn to write one tool_call meta event       │
│        per call (msg_index → session.messages position)      │
│     e. Append tool results to messages                       │
│  8. Reasoning-truncation recovery (2B): if finish_reason=    │
│     length AND content blank AND reasoning_content non-empty │
│     → inject specialized cue asking model to wrap up         │
│  9. hook.after_iteration(context):                           │
│     emits cache.usage telemetry event with prompt_tokens,    │
│     cached_tokens, completion_tokens, cache_ratio_pct        │
│ 10. If no tool_calls → final_content → break                │
└─────────────────────────────────────────────────────────────┘
```

The hook surface (`before_iteration`, `before_execute_tools`, `after_iteration`, plus streaming hooks) is intentionally generic. No hooks are bundled at present. New hooks (e.g. an `ExecutionTelemetryHook` for tracking iterations/tokens/tools) should attach via the standard `AgentHook` interface.

### Runner state-tracking + guards

The runner carries a small set of turn-scoped guards. All are defensive — they shape behaviour only when the model misbehaves or the environment fails.

**Loop detection** (`runner.py::_run_tool`). A turn-scoped set of `sha256(tool_name + sorted-args)` signatures for any tool call that returned a hard failure. A repeat hit short-circuits with a synthetic "BLOCKED" tool result. Reset between turns (environment state may have changed). Pytest-style failures (tool returned ok but the environment reported a test failure) are NOT recorded — test-driven loops keep working.

**Topological tool ordering** (`runner.py::_partition_tool_batches`). Walks the model's tool-call list in order and groups only CONSECUTIVE `concurrency_safe=True` tools into a parallel batch. Mutations + exclusive tools become singleton batches. Order is preserved; read-after-write semantics survive. Tools default to `read_only=False` — opt-in safety.

**Reasoning-phase truncation recovery** (`runner.py` length-handling branch). When `finish_reason="length"` and `content` is blank but `reasoning_content` is non-empty (the model hit `max_tokens` mid-deliberation), the runner appends the partial reasoning plus a specific cue asking the model to wrap up. Preserves the chain-of-thought instead of re-sending the same prompt.

**Idle-timeout circuit breaker** (`runner.py` top of iteration loop). Counts iterations whose response is `finish_reason="error" and error_kind="timeout"`. Forward-progress iterations (tool_calls or non-empty content) reset the counter. After `DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS` (default 1, → trips on 2nd consecutive) the run terminates with `stop_reason="circuit_breaker_idle_timeout"` and a `circuit_breaker.idle_timeout` event.

**Per-block tool-result validation** (`utils/tool_result_validation.py`, applied in `runner.py::_normalize_tool_result`). Caps each block before the aggregate path: text blocks > 100 KB truncated in place; `image_url` data-URLs > 5 MB replaced with a text placeholder; `input_audio` data > 10 MB likewise. HTTP/HTTPS image URLs (references, not payloads) pass through. Unknown block types pass through untouched.

**Re-sanitize after `context_transform`** (`runner.py::_build_request_kwargs`). After the optional `context_transform` hook returns, runs `drop_orphan_tool_results` + `backfill_missing_tool_results` once more, so a transform that dropped a message mid-pair doesn't ship an invalid `tool_use`/`tool_result` mismatch to the provider.

**Compaction grace window** (`runner.py::_await_with_compaction_grace`, wired in `loop.py` via `Consolidator.get_lock(session_key).locked()`). When the outer LLM wall-clock timeout (`DURIN_LLM_TIMEOUT_S`) is about to fire, the runner checks the optional `is_compacting` callback on `AgentRunSpec`. If True, the deadline is extended once by `DURIN_COMPACTION_GRACE_S` (default 30 s) and a `compaction.grace_extended` event is emitted. Grace is one-shot per request. Implemented with `asyncio.wait({task}, timeout=...)` so the task isn't cancelled at the base timeout.

**Per-model `parallel_tool_calls` gating** (`OpenAICompatProvider._resolve_parallel_tool_calls`). `agents.defaults.parallel_tool_calls` is a substring-keyed dict mapping model names → True/False. The provider injects `parallel_tool_calls` into the request kwargs only when there's a match AND `tools` is non-null. Emits `provider.parallel_tool_calls_injected` (`{model, value, match_needle}`) at most once per unique triple per process.

**Per-turn aggregate tool-result budget** (`runner.py::_enforce_turn_budget`). After all tool results for a turn are collected, the runner sums their sizes; if the total exceeds `DURIN_TURN_BUDGET_CHARS` (default 200 KB), the largest not-yet-persisted results are spilled to disk in priority order (largest first) until the aggregate fits. Already-persisted results (containing the `[tool output persisted]` marker) are skipped. Emits `turn_budget.enforced` when triggered. `DURIN_TURN_BUDGET_CHARS=0` disables.

**Heartbeat session mode** (`heartbeat/service.py::heartbeat_session_key`, wired in `cli/commands.py::on_heartbeat_execute`). By default the heartbeat reuses a single long-running session named `heartbeat` (trimmed by `keep_recent_messages` between ticks). With `heartbeat.isolatedSessions=true` each tick gets a fresh `heartbeat-<12-hex>` session that the executor deletes after the run — for stateless one-shot probes.

**Pre-emptive compaction trigger** (`agent/memory.py::Consolidator._preemptive_trigger_tokens`). Consolidator fires when `estimated_tokens > preemptive_compact_ratio * context_window` (default 0.5). Per-preset via `ModelPresetConfig.preemptive_compact_ratio` (a 1M-window model uses ~0.15; a 128K window uses ~0.5). Falls back to `AgentDefaults.preemptive_compact_ratio` when the preset doesn't override. Clamped above by the input budget. `consolidation_ratio` is relative to the trigger threshold (so each round leaves `trigger * consolidation_ratio` of context). Emits `compaction.preemptive_trigger` when the pre-emptive threshold fires below the hard budget ceiling.

**Mid-turn precheck signal** (`agent/runner.py::_mid_turn_precheck`). After each iteration's sanitize pipeline, estimates the message + tool token cost. If it exceeds the input budget, aborts the turn with `stop_reason="mid_turn_precheck_overflow"` and emits `mid_turn_precheck.overflow` BEFORE making the LLM call. Estimator exceptions are swallowed.

**Compaction lock aggregate timeout** (`agent/memory.py::Consolidator._lock_timeout_s`). Per-session compaction lock acquisition is bounded by `DURIN_COMPACTION_LOCK_TIMEOUT_S` (default 180 s). When the timeout expires, the call abandons the acquisition and emits `compaction.lock_timeout`. `0` disables the timeout. Acquire/release is wrapped in `try/finally` so the body releases on success and on raise.

**Tool-call argument repair** (`utils/tool_argument_repair.py::parse_tool_call_arguments`, wired in `openai_compat_provider` + `bedrock_provider`). Runs `html.unescape` (only when entity markers are present), strips up to 96 chars of leading garbage (allowlist regex) and up to 3 chars of trailing garbage, then hands the cleaned string to `json_repair.loads`. Bounded by a 64 KB buffer — larger inputs pass through unrepaired. Emits `tool_call.argument_repair` with the repair tokens applied and `parsed_ok`.

**Unknown-tool loop guard** (`agent/runner.py` at the top of the `should_execute_tools` branch). Counts calls per unknown tool name per turn. When any name's count exceeds `DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS` (default 2), the turn terminates with `stop_reason="unknown_tool_loop_guard"` and emits `unknown_tool.loop_guard`. The error surfaces the real tool names so callers can show a "did you mean X?" message. Only fires against registries that expose `tool_names` as a list / tuple / set / frozenset.

**History image / audio prune** (`utils/history_image_prune.py::prune_processed_history_images`, wired in `runner.py`'s sanitize pipeline). Identifies completed user→assistant turns, keeps the most recent `DURIN_HISTORY_IMAGE_PRESERVE_TURNS` (default 3) intact, and in older user/tool messages replaces `image_url` / `image` / `input_image` blocks with `[image data removed - already processed by model]` and `input_audio` blocks with the audio equivalent. Assistant messages are untouched. Idempotent. `preserve_turns` is clamped to ≥ 1. Emits `history_media.pruned` only when at least one block is removed.

**3-tier system prompt** (`agent/context.py::ContextBuilder._build_stable_layer` / `_build_context_layer` / `_build_volatile_layer`). The system prompt is joined from three layers with `\n\n---\n\n`: **stable** (identity → bootstrap files → active-skills content → skills catalog), **context** (active agent-mode prompt suffix), **volatile** (memory → recent history → archived session summary). The stable prefix is byte-identical across turns of one session for prompt-cache hits; the volatile layer is appended last and changes per turn.

**Post-compaction loop guard** (`utils/post_compaction_guard.py::PostCompactionLoopGuard`, owned by `Consolidator`, observed by `AgentRunner`). After a successful compaction round, the guard is armed for the next `DURIN_POST_COMPACTION_GUARD_WINDOW` (default 3) tool calls. When the SAME `(tool_name, args_hash, result_hash)` triple repeats `window_size` times within the window, the run aborts with `stop_reason="post_compaction_loop"` and emits `post_compaction_loop.tripped`. The guard is per-session and auto-disarms after the window passes without a trip.

---

## 3. Module Map

```
durin/
├── agent/
│   ├── loop.py            # AgentLoop — outer state machine, dispatch, sessions
│   │                      #   builds aux_providers + tool-call meta timeline
│   ├── runner.py          # AgentRunner — inner LLM/tool loop
│   │                      #   _build_request_kwargs honors context_transform
│   │                      #   _run_tool_timed stamps duration_ms + tool_call_id
│   ├── hook.py            # AgentHook + AgentHookContext + CompositeHook
│   ├── context.py         # ContextBuilder — system prompt + history + skills
│   ├── memory.py          # MemoryStore — markdown files + dream/consolidator
│   ├── agent_mode.py      # Plan/Build/Explore permission-as-data modes
│   ├── progress_hook.py   # Streaming + tool-event progress + cache.usage event
│   ├── subagent.py        # Spawn parallel sub-agents + lifecycle status retention
│   ├── model_presets.py   # Named model + generation parameter sets
│   ├── skills.py          # Skill discovery, on-demand loading,
│   │                      #   disable_model_invocation gating
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
│       ├── repo_overview.py / output_spill.py / image_generation.py
│       └── context.py              # ToolContext + AuxProviderHandle
├── api/                   # HTTP/SSE/WebSocket transport layer
├── bus/                   # Internal message bus (InboundMessage, OutboundMessage)
├── channels/              # CLI, WebUI, Slack, Telegram, etc.
├── cli/                   # CLI entry, prompts, command dispatch
├── command/               # /commands router (/plan, /build, /mode, …)
├── config/                # Config schemas, loader, validation
│                          #   AgentDefaults, ModelPresetConfig, AuxModelsConfig,
│                          #   ModelCapabilityOverride
├── cron/                  # Scheduled task service
├── heartbeat/             # Background heartbeats and timers
├── pairing/               # Account pairing flow
├── providers/             # LLM provider adapters (34 registered)
│   ├── factory.py + registry.py        # make_provider, ProviderSpec
│   ├── capabilities.py                 # ModelCapabilities + 4-layer resolver
│   ├── data/model_capabilities.json    # vendor-filtered consensus snapshot
│   │                                    # (community merge + opt-in vendor APIs,
│   │                                    #  schema v2, ~785+ models, regenerated
│   │                                    #  by scripts/refresh_model_capabilities.py)
│   ├── anthropic_provider.py / bedrock_provider.py / openai_codex_provider.py
│   ├── openai_compat_provider.py       # cache_control ephemeral for caching
│   ├── azure_openai_provider.py / github_copilot_provider.py
│   ├── local_llama_provider.py / fallback_provider.py
│   └── transcription.py / image_generation.py
├── security/              # Auth, permissions, network SSRF guard
│   └── secrets.py                         # secret store + ${secret:} refs + redaction
├── session/               # Session storage + state helpers
│   ├── manager.py + Session                # in-memory + jsonl persistence
│   ├── goal_state.py                       # sustained-goal runtime block
│   ├── todo_state.py                       # echo todos into runtime context
│   └── session_meta.py                     # <key>.meta.json sidecar
│                                            # — plan events + tool_call events
│                                            #   with msg_index pointers
├── skills/                # Built-in skill markdown files
├── telemetry/             # Generic JSONL logger
├── templates/             # Prompt templates
├── utils/                 # Helpers (no business logic)
└── web/                   # Static web assets

scripts/
├── refresh_model_capabilities.py    # dev tool — regenerates the consensus
│                                    # snapshot. Phase 1: community merge
│                                    # (LiteLLM + OpenRouter + models.dev,
│                                    # filtered by TRUSTED_VENDORS). Phase 2:
│                                    # opt-in vendor-API overlay — vendor data
│                                    # wins field-by-field over community.
└── _vendor_sources.py               # Anthropic / Mistral / Gemini adapters.
                                     # Each requires its API key env var;
                                     # absence is silent (community merge
                                     # remains the fallback).
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
- `bind_telemetry(logger)` / `current_telemetry()` / `reset_telemetry(token)` — ContextVar-based per-task binding so tools can resolve the active session's logger without explicit constructor wiring (parallels `bind_file_states`).

Wiring points:
- **Provider rate limits**: `provider.set_telemetry()` is called in `AgentLoop.from_config` and the provider emits `provider.rate_limit{,_exhausted}` events directly.
- **Per-task tool events**: `AgentLoop._dispatch_message` calls `bind_telemetry(get_session_logger(session_key))` before invoking the runner and `reset_telemetry(token)` in the finally block, mirroring the `file_states` binding. Tools resolve the bound logger via `current_telemetry()`.

Tool-level instrumentation:
- `read_file` emits `tool.read_file` events with `{path, offset, limit, total_lines, returned_lines, result_chars, kind, truncated, dedup}` on the successful text-read and dedup paths.
- `grep` emits `tool.grep` events with `{pattern_len, fixed_strings, case_insensitive, output_mode, limit, offset, glob_filter, type_filter, displayed, total_before_pagination, result_chars, truncated, size_truncated, skipped_binary, skipped_large}` on every non-error completion (including zero-match).
- `edit_file` emits `tool.edit_file` events with `{path, match_strategy, matches, outcome, old_text_chars, new_text_chars}` for every call. `outcome` ∈ `{edited, not_found, ambiguous}`. `match_strategy` ∈ `{exact, line_trimmed, line_trimmed_quote_normalized, quote_normalized, block_anchor, null}` — lets us measure how often each cascade layer earns its keep.
- `repo_overview` emits `tool.repo_overview` events with `{path, depth, ecosystems, package_manager, dependency_files_count, entrypoints_count, structure_lines, truncated, result_chars}`.
- `exec` emits `tool.exec.spill` events with `{spilled, original_chars, rendered_chars, spill_path, spill_error}` when output exceeds the cap and gets spilled to disk via `truncate_with_spill`.
- `ask_user_question` emits `ask_user.question_asked` with `{question_id, question_chars, option_count}` when the tool yields control to the user; the turn pauses until the user responds.
- `interpret_image` emits the trio `ask_vision.{start, error, end}`. `start` carries `{aux_model, image_bytes, mime, question_chars}`; `error` carries `{exception}` if the vision provider raises; `end` carries `{response_chars, had_content}` on the success path. Lets us measure aux-model uptime and response shape per call.
- `interpret_audio` emits the analogous `ask_audio.{start, error, end}` triple with `audio_bytes` / `format` in the start payload.
- `sleep` emits `sleep.start` (`{requested_s, actual_s, clamped, reason}`), then either `sleep.cancelled` or `sleep.end` (`{elapsed_s, reason}`). Lets us see how often the model genuinely sleeps vs. cancels mid-wait.
- No event is emitted on error paths for the core tool family (`read_file` / `edit_file` / `grep` / `repo_overview`) — by design, the goal is measuring information-loss patterns, not failure counts. Telemetry failures are silently swallowed so tool calls never break from a logging issue.

Telemetry never changes tool defaults — the events exist for visibility, not enforcement. Telemetry failures are silently swallowed inside `emit_tool_event` / `_FsTool._emit` so tool calls never break from a logging issue.

### Telemetry schema catalog

The complete set of event types + payload shapes is centralised in `durin/telemetry/schema.py`. Each event has a `TypedDict` declaring its required + optional fields; the `EVENTS` dict at the bottom maps every event type to its TypedDict. A meta-test in `tests/telemetry/test_schema_catalog.py` scans the source tree for `_emit("…")` / `.log("…")` / `emit_tool_event("…")` call sites and asserts the catalog is in sync in **both directions** — emitted-but-uncatalogued AND catalogued-but-unemitted entries fail the test.

Conventions baked into the schema:

- ``session_key: str | None`` — present on every event from a loop-control or session-scoped service; absent from tool events and the rate-limit pair (which fire from outside the per-task context).
- ``iteration: int`` — present on every event from inside the runner's per-turn loop. Lets dashboards correlate to the LLM turn that emitted it.
- Numeric units in field-name suffix: ``*_chars``, ``*_tokens``, ``*_bytes``, ``*_s`` (seconds), ``*_ms`` (milliseconds).
- ``snake_case`` everywhere; event type strings use ``namespace.action``.

### Tool I/O hygiene

**Read suggestion-on-miss.** `ReadFileTool` suggests close-named files in the same directory when the requested path doesn't exist. Shared helper `_file_not_found_msg` lives on `_FsTool` and is reused by `EditFileTool`. Uses `difflib.get_close_matches(cutoff=0.6, n=3)`.

**`repo_overview` tool** (`durin/agent/tools/repo_overview.py`). One-shot orientation: depth-bounded directory tree (default 3, capped at 200 entries) plus detected ecosystems (Python, Node.js, Go, Rust, Ruby, Java/Kotlin, PHP), package manager (npm/pnpm/yarn/bun/poetry/cargo/etc.), dependency files, common entrypoints. No embeddings, no AST — purely structural. Lets the model orient before grep/list_dir.

**Tool output spill** (`durin/agent/tools/output_spill.py`). When a tool produces output larger than its budget, the FULL content is written to `<workspace>/.durin/spills/<tool>_<ts>_<hash>.txt` and the model receives head+tail of the budget plus a reference (`read_file(path=...)`) to recover the omitted middle. Wired into `ExecTool` (10K-char threshold). Spill write failures fall back to plain head/tail truncation — never breaks the tool call.

**Block-anchor matcher in the edit cascade** (`filesystem.py::_find_block_anchor_matches`). Fifth fallback in `EditFileTool`'s replacer chain: when `old_text` has 3+ lines, match first and last lines exactly (after strip) and apply a similarity threshold on the middle (0.5 with a single candidate, 0.85 with multiple). Handles cases where the model knows the start and end of a block but the interior shifted (reformatting, added comments, whitespace changes). Cascade order: `exact → line_trimmed → line_trimmed_quote_normalized → quote_normalized → block_anchor`. The strategy used is reported in `tool.edit_file` telemetry.

### Permission-as-data agent modes

Plan / Build / Explore modes selectable per session. The active mode filters the tool surface at the LLM boundary.

**Core** (`durin/agent/agent_mode.py`). `AgentMode` is a frozen dataclass with `allowed: frozenset[str] | None`, `denied: frozenset[str]`, and optional `prompt_suffix`. Three built-ins:
- `build` — default, no restriction
- `plan` — read-only + `exit_plan_mode` only; the model investigates and surfaces a plan for user approval
- `explore` — read-only for sub-agents (no exit affordance)

Session state lives in `session.metadata`:
- `agent_mode` — currently active mode name
- `pre_plan_mode` — set when entering plan mode, restored on exit

**Tool filtering in the runner** (`runner.py::_active_tool_definitions`). The runner accepts an optional `mode_provider` callable in `AgentRunSpec`; when present, it's called per iteration and the resulting mode filters the tool definitions sent to the LLM. The registry's cached definitions stay valid — filtering is a per-call slice. When the model emits a cached tool name that's no longer allowed (rare), `_run_tool` short-circuits with a clear denial: *"Tool 'X' is not available in mode 'plan'..."*. `AgentLoop._dispatch_message` wires the provider to read from `session.metadata` at call time, so mid-run mode switches take effect at the very next iteration.

**LLM-facing tools** (`durin/agent/tools/plan_mode.py`):
- `enter_plan_mode(reason?)` — switches the session into plan mode (the model may invoke this voluntarily; typically the user activates plan mode via `/plan` instead)
- `exit_plan_mode(plan)` — **writes the plan to `<workspace>/.durin/plans/plan_<timestamp>.md`** and yields to the user for approval. Does NOT actually exit plan mode — the session remains in plan until the user runs `/build`. While in plan, the user can edit the plan file directly with any editor; `/build` picks up the file content as-edited.

**File-based plan storage**. Plans live in `<workspace>/.durin/plans/<session-slug>/plan_<timestamp>.md` — one subdirectory per session, one file per `exit_plan_mode` call. The session slug is the sanitized session key (`websocket:chat42` → `websocket_chat42`), so concurrent chats don't collide and the user can locate plans for a specific conversation. Storing on disk lets the plan survive compaction, supports edit-before-approve (user opens the .md and tweaks step 3), and keeps message history small (the plan content isn't repeated in tokens).

**Plan flow with compaction survival**:

| Phase | What happens to the plan |
|---|---|
| `/plan` activates | Mode = plan. Any prior `executing_plan_path` from a previous /build is cleared (new plan supersedes). |
| `exit_plan_mode(plan)` | Writes the plan to `<workspace>/.durin/plans/<session>/plan_<ts>.md` and sets `session.metadata["active_plan_path"]`. Session stays in plan. |
| `/build` approves | `active_plan_path` → `approved_plan_path` (one-shot for next-turn reminder) AND `executing_plan_path` (persistent for autocompact). Mode restored. |
| Next turn after /build | `ContextBuilder.build_messages` injects a one-shot system reminder: *"Approved plan ready at: <path>. Start with updating your todo list using the todo_write tool if applicable…"* — then pops `approved_plan_path`. |
| Autocompact archives messages | `autocompact._read_plan_carryover` reads `executing_plan_path` and splices the plan content into the summary block (cap: 6,000 chars). The plan survives compaction the same way Claude Code's `plan_file_reference` attachment does. |

The `_PLAN_DIR = ".durin/plans"` constant is in `plan_mode.py`. Add to `.gitignore` if you don't want plans tracked. The `executing_plan_path` key persists until a new `/plan` clears it, so a single approved plan keeps being re-injected through arbitrary numbers of compactions until the user starts a new plan.

**Slash commands** (`durin/command/builtin.py`):
- `/plan` — enter plan mode for the current session
- `/build` — exit plan mode (restores `pre_plan_mode`, defaults to `build`)
- `/mode [name]` — show the active mode, or set one explicitly

All three are registered in the `CommandRouter` and exposed via `builtin_command_palette()`, so they work in every channel automatically. Dispatch is universal; autocomplete UX varies by channel (see "Future improvements" below).

**Context builder integration** (`context.py::build_system_prompt`). When a mode has a non-empty `prompt_suffix`, it's appended to the system prompt. `build_messages` reads the active mode from `session_metadata[SESSION_MODE_KEY]` and threads it through.

**Telemetry**:
- `agent_mode.turn_start` — emitted at the start of each `AgentRunner.run()` call with the active mode
- `agent_mode.switch` — emitted on every mode change, with `{from, to, trigger}` (`trigger ∈ {slash_command, tool}`)
- `agent_mode.tool_denied` — emitted when a tool call is denied by the mode (`{tool, mode}`)
- `plan_mode.presented` — emitted when `exit_plan_mode` surfaces a plan (`{plan_chars, from_mode}`)

**Constraints by design**:
- No auto-resume on exit: the user must run `/build` to leave plan mode. The approval gate is universal across channels.
- No mode-specific model override.
- No native UI permission dialog — slash commands carry the approval intent.

### Per-channel slash-command UX

Mode dispatch (`/plan`, `/build`, `/mode`) works in every channel. Native autocomplete varies:

| Channel | Status today | Future improvement |
|---|---|---|
| **CLI** | Dispatch works (`/plan` is typed and dispatched). | Add a `prompt_toolkit` completer in `cli/commands.py` that reads `builtin_command_palette()` for autocomplete. ~10 LOC. |
| **WebUI** | Full autocomplete via `<SlashCommandPalette>` already wired to `/api/commands` — `/plan`, `/build`, `/mode` appear automatically because they're in `builtin_command_palette()`. | Optionally add a visual badge/pill in the composer header when mode != build. |
| **Telegram** | Dispatch works. The native `/` menu only lists the commands explicitly registered via `BotCommand(...)` in `channels/telegram.py`. | Add `BotCommand("plan", "Enter plan mode")`, `BotCommand("build", "Exit plan mode")`, `BotCommand("mode", "Show or set agent mode")` to the existing list — ~3 LOC. |
| **Slack / Matrix / WhatsApp / DingTalk / MoChat** | Dispatch works. Slash commands appear as plain messages, no native autocomplete (each channel's API differs). | Per-channel slash-command registration is optional; the dispatch already works. |

---

## 6. Long Tasks and Goal State

`durin/agent/tools/long_task.py` defines `LongTaskTool` (register an objective) and `CompleteGoalTool` (close it with a recap). Goal state is stored in `session.metadata[GOAL_STATE_KEY]` and mirrored into the runtime-context block each turn via `durin/session/goal_state.py`.

After the prune, `complete_goal` no longer consults any plan-tier verification gate. It only requires that a goal is currently active.

---

## 7. Sessions and Persistence

`durin/session/manager.py` handles session lifecycle. Two files per session:

| File | Content | Purpose |
|---|---|---|
| `<key>.jsonl` | Message history + identity metadata (mode, plan path, todos, channel, title) on line 0 | **Source of truth.** Replayable; messages append-only, never trimmed. |
| `<key>.meta.json` | Lifecycle event timeline + a ``derived`` block (LLM-produced projections of the conversation) | **Derived state.** Regenerable from `.jsonl` + `memory/history.jsonl`. Safe to delete and rebuild. |

The split rule: if losing the file means you can't reconstruct it from the other, it's source-of-truth (`.jsonl`). Otherwise it's derived (`.meta.json`). `MemoryStore` (`durin/agent/memory.py`) handles long-lived markdown memory files (`MEMORY.md`, etc.) consumed by `ContextBuilder` into the system prompt.

`Consolidator.maybe_consolidate_by_tokens` (in `durin/agent/memory.py`) advances a cursor (`last_consolidated`) when the prompt exceeds the budget — it generates a narrative summary, persists it to `history.jsonl` + writes the latest summary to `.meta.json::derived._last_summary`, and advances the cursor. **The raw `session.messages` list is never modified in-place**; only the cursor advances. The LLM sees `messages[last_consolidated:]` (capped by `max_messages`) + the summary.

`SessionManager._DERIVED_METADATA_KEYS` is the canonical set of `session.metadata` keys that route to the sidecar's `derived` block instead of line-0. Today only `_last_summary`; future additions (`session_embedding`, `narrative_summary`, etc.) go in this set so they don't pollute the source-of-truth file.

In-memory per-turn shaping (does not touch disk):
- `_microcompact` in `runner.py` replaces older tool-result content with `[<tool> result omitted from context]` placeholders on the copy sent to the LLM
- `_snip_history` in `runner.py` further trims the copy from the start when it still doesn't fit the context window

### Session meta sidecar — lifecycle index + derived state

Sessions are immutable but the message stream isn't enough to recover "significant events" from prior turns — plan submissions, plan approvals, mode transitions, etc. live in transient metadata that would otherwise be overwritten. The session meta sidecar is a structured index of these events plus the `derived` block for LLM-produced projections.

**Module**: `durin/session/session_meta.py`. **Storage**: one file per session, `<workspace>/sessions/<safe_key>.meta.json`, sitting next to the `<safe_key>.jsonl` it indexes.

**Shape**:

```json
{
  "session_key": "websocket:chat42",
  "events": [
    {
      "type": "plan",
      "id": "plan_20260519_143022_123",
      "title": "Refactor authentication module to use OAuth",
      "plan_path": ".durin/plans/websocket_chat42/plan_20260519_143022_123.md",
      "created_at": "2026-05-19T14:30:22.123",
      "approved_at": "2026-05-19T14:35:12.456",
      "closed_at": null,
      "msg_index": { "approved": 240, "closed": null },
      "outcome": "executing",
      "recorded_at": "2026-05-19T14:30:22.124"
    }
  ],
  "derived": {
    "_last_summary": {
      "text": "Compaction summary text…",
      "last_active": "2026-05-19T14:35:12.456"
    }
  }
}
```

Two top-level blocks:

- ``events`` — lifecycle index. `type` is the discriminator for extensibility — today `plan` and `tool_call`; the format accommodates future event kinds without schema changes.
- ``derived`` — LLM-produced projections of the session content (compaction summary today, future embeddings or narrative summary). Whatever `Session.metadata` keys are listed in `SessionManager._DERIVED_METADATA_KEYS` get persisted here instead of line-0 of the `.jsonl`. On load, `SessionManager._merge_derived_from_sidecar` merges them back into the in-memory metadata dict, so consumer code keeps reading one flat dict.

**Plan lifecycle** (current implementation):
- **`exit_plan_mode` tool**: appends a fresh plan event with `outcome=pending`, extracted `title` from the plan markdown's first heading, and the plan_path
- **`/build` slash command**: looks up the executing plan event by id (matching `plan_path.stem`) and transitions it to `outcome=executing`, recording `approved_at` and `msg_index.approved = len(session.messages)`
- **`/plan` slash command**: if there was a prior `executing_plan_path` in session metadata, closes its meta event with `outcome=superseded` and `msg_index.closed`

**Atomic writes**: every mutation reads → modifies → writes a `.tmp` → `os.replace`. No partial states on disk.

**Post-processing**: the memory subsystem can read the session.jsonl and the .meta.json side-by-side to know "what happened" (raw messages) plus "what mattered" (significant events). The `msg_index` ranges let it slice the raw messages by event scope (e.g. "show me everything the agent did between when plan_X was approved and when it was superseded").

**Why a single file per session** (vs sidecar per event):
- Memory subsystem reads ONE file to know everything significant in a session
- `cat`/`jq`/`grep` friendly for debugging
- Event count is small (planes y similares: pocos por session, no miles)
- Future event types just append a new entry with a different `type`

**What does NOT go here**:
- Per-turn telemetry (lives in `~/.cache/durin/telemetry/`)
- Plan contents (live in their own `.md` files, referenced by `plan_path`)
- Anything already in `session.jsonl`

---

## 8. Memory Subsystem (Phase 1)

`durin/memory/` provides the agent with cross-session learnings, ingested documents, and a navigable provenance trail. The canonical design lives in `docs/08_memory_phase2_proposal.md` §0c; Phase 1 (the foundation) lands the surface listed in §0c.9.

### 8.1 Three sources of truth

| Kind | Where | Role |
|---|---|---|
| Sessions | `<workspace>/sessions/<key>.jsonl` | Conversation turn log. Append-only. |
| Ingested docs | `<workspace>/ingested/<id>/source.<ext>` | External artifacts handed to `memory_ingest`. Frozen at ingest. |
| Memory entries | `<workspace>/memory/<class>/<id>.md` | Derived learnings — markdown + YAML frontmatter. User may edit by hand. |

The 6 utility classes from §0a Decision 1 map onto the directories `memory/stable/`, `memory/episodic/`, `memory/corpus/`, `memory/pending/` (the remaining two classes — procedural skills and the prospective time-trigger half — live in `skills/` and `cron/` respectively).

### 8.2 On-disk layout

```
<workspace>/
├── sessions/<key>.jsonl         # canonical
├── sessions/<key>.meta.json     # derived: summary + tags
├── sessions/<key>.md            # derived: navigable view with #turn-N anchors
├── ingested/<id>/source.*       # canonical
├── ingested/<id>/meta.json      # derived: summary + entities + relations
├── memory/<class>/<id>.md       # derived: memory entry, mutable
└── dream/cursor.json            # dream cron progress (populated in Phase 3)
```

### 8.3 Memory entry schema

`durin/memory/schema.py` defines a pydantic `MemoryEntry` with `extra="forbid"`. Frontmatter carries multi-resolution:

- `headline` (~10 words) — pulled in bulk into the hot layer.
- `summary` (~50 words) — returned by `memory_search(level="warm")`.
- `body` (~200-500 words) — returned by `memory_search(level="cold")` or by `memory_drill`.

Provenance lives in `author: agent_created | user_authored`, driven by the `_MEMORY_AUTHOR` ContextVar from `durin/memory/provenance.py`. Markdown links in `source_refs` point to specific session turns (`sessions/<key>.md#turn-N`) or document sections (`ingested/<id>/source.md#section`).

### 8.4 Modules

```
durin/memory/
├── provenance.py        # _MEMORY_AUTHOR ContextVar + author_scope
├── paths.py             # workspace-scoped directory helpers + MEMORY_CLASSES
├── schema.py            # MemoryEntry pydantic model
├── storage.py           # split_frontmatter + save_entry + load_entry
├── session_md.py        # <key>.jsonl → <key>.md formatter with #turn-N anchors
├── consolidator_tags.py # parse summary / entities / topics from consolidator response
├── ingestion.py         # ingest_artifact(workspace, source_path)
├── store.py             # store_memory(workspace, content, class_name, ...)
├── search.py            # grep over dreamed + undreamed sources
├── drill.py             # resolve markdown URI to the addressed section
└── hot_layer.py         # identity + top headlines + entity list for the stable prompt tier
```

### 8.5 Tools

| Tool | Path | Purpose |
|---|---|---|
| `memory_ingest` | `durin/agent/tools/memory_ingest.py` | Copy a markdown/text file to `ingested/<id>/` (content-hash idempotent) and return its content. |
| `memory_store` | `durin/agent/tools/memory_store.py` | Write a memory entry to `memory/<class>/<id>.md` with auto-headline; implicitly stamps `author=agent_created`. |
| `memory_search` | `durin/agent/tools/memory_search.py` | Grep over dreamed + undreamed sources. `scope` ∈ {all, dreamed, undreamed}, `level` ∈ {warm, cold}. `read_only=True`. |
| `memory_drill` | `durin/agent/tools/memory_drill.py` | Resolve `path.md#anchor` to the addressed section. `read_only=True`. |

### 8.6 Hooks into existing systems

- `SessionManager.save()` calls `regenerate_session_md(path)` after writing the `.jsonl` so the navigable `.md` view (with stable `#turn-N` anchors) is always current.
- `SessionManager._DERIVED_METADATA_KEYS` now includes `_last_tags`; per-session entity/topic tags emitted by the consolidator land in `<key>.meta.json::derived`.
- `Consolidator.archive()` returns `(summary, tags)` and `Consolidator._merge_session_tags` accumulates tags into `session.metadata["_last_tags"]` across compactions.
- `ContextBuilder._build_stable_layer` appends `read_hot_layer(workspace).render()` at the end of the stable prompt tier. Cache-friendly: the hot layer is read-only between dreams, designed to flip once a day under Phase 3.

### 8.7 Telemetry

`durin/telemetry/schema.py` adds three TypedDicts and EVENTS entries:

- `memory.recall` — one per `memory_search` call (`query`, `scope`, `level`, `result_count`).
- `memory.store` — one per successful `memory_store` (`entry_id`, `class_name`, `author`, `headline`).
- `memory.ingest` — one per successful `memory_ingest` (`entry_id`, `size_bytes`, `suffix`).

The schema-catalog meta-test in `tests/telemetry/test_schema_catalog.py` confirms emit sites and catalog stay in sync.

### 8.8 What Phase 1 does NOT do

- No dream cron — memory entries are created only via `memory_store` or by the user editing files. Cross-session derivation lands in Phase 3.
- No vector retrieval in Phase 1 — search was pure grep. Phase 2 layered LanceDB on the same public entrypoint (see §8.9).
- No knowledge graph — entities live as frontmatter lists; the SQLite KG with `valid_from` triples is Phase 3.
- No automated entity extraction beyond the consolidator's per-session tags.

### 8.9 Phase 2 additions — vector retrieval

Phase 2 adds embedding-driven retrieval on top of the same markdown source of truth.

**Modules**

| Module | Role |
|---|---|
| `durin/memory/embedding.py` | `EmbeddingProvider` ABC + `FastembedProvider` (ONNX, in-process, lazy load). Default model `intfloat/multilingual-e5-small` (471 MB, 384-dim, polite default). `BAAI/bge-m3` available for CJK-heavy users via `memory.embedding.model` override. |
| `durin/memory/vector_index.py` | `VectorIndex` wrapping a LanceDB table at `<workspace>/memory/.index.lance`. `upsert(entry, class, path)` for incremental writes; `rebuild_from_workspace()` for full rebuild; `search(query, top_k)` for nearest-neighbour. |

**Config**

`durin/config/schema.py` adds `memory.embedding.{provider, model, base_url, api_key, lazy_eviction}` under a new top-level `memory` section.

**Tool wiring**

- `MemoryStoreTool.execute` upserts the new entry into the vector index after the markdown write (best-effort — a vector failure logs a warning but never breaks the markdown source-of-truth write).
- `MemorySearchTool.execute` selects a strategy by `(scope, level)`:
  - `dreamed` + `warm` → vector only (`strategy=vector`).
  - `all` + `warm` → vector for memory entries + grep for sessions/ingested (`strategy=hybrid`).
  - any other shape (cold tier, undreamed-only, vector unavailable) → grep only (`strategy=grep`).
  - Vector failures fall back to grep silently.

**Telemetry events** (Phase 2)

- `memory.embedding.load` — emitted once per process when the model first loads. Carries `model`, `duration_ms`.
- `memory.embedding.embed` — emitted per batch with `model`, `batch_size`, `duration_ms`.
- `memory.recall.vector` — emitted when the vector path runs. Carries `query`, `scope`, `embedding_model`, `hit_count`, `duration_ms`. Separate from the aggregate `memory.recall` event so dashboards can split latency / hit count by strategy.

**Install footprint**

Vector retrieval is opt-in via `pip install durin[memory]` (adds `fastembed` and `lancedb`). Phase 1 (grep) keeps working without the extra. On first vector call the embedding model auto-downloads (~471 MB for the default model) into `~/.cache/fastembed/` and stays resident for the process lifetime — no idle eviction in V1 per the data-driven decision recorded in `docs/08_memory_phase2_proposal.md` §0d.2.

---

## 9. Sandboxing

Tool execution is sandboxed via `durin/agent/tools/sandbox.py`. Three backends:
- `bwrap` — Linux namespace sandbox (production)
- `docker` — Docker container (for benchmark-style isolation, see registration helpers)
- `testbed` — conda-env wrapper for running inside benchmark containers

The agent's exec tool routes through `wrap_command(sandbox, command, workspace, cwd)`.

---

## 10. Providers

`durin/providers/` ships adapters for Anthropic, OpenAI-compat (incl. Z.ai, OpenRouter, Azure, Ollama, LM Studio, Gemini, and 25+ others — see `registry.py`), Bedrock, GitHub Copilot, local llama-cpp, OpenAI Codex, and a fallback wrapper. `factory.make_provider(config)` resolves the active provider/model from config + presets.

### Capability metadata (capabilities.py + data/model_capabilities.json)

`get_model_capabilities(model, provider, overrides)` resolves a `ModelCapabilities` dataclass via a four-layer fallback:

1. **Explicit override** from `config.model_capabilities` — always wins. Use when adding a private/custom model the snapshot doesn't know about.
2. **Vendored consensus snapshot** at `providers/data/model_capabilities.json` (schema v2). Built by `scripts/refresh_model_capabilities.py` in two phases:
   - **Phase 1 — community merge** (LiteLLM + OpenRouter + models.dev). Filtered by a TRUSTED_VENDORS whitelist (anthropic/openai/google/zai/meta/mistral/deepseek/xai/qwen/moonshot/amazon/cohere/minimax/stepfun/ai21/ibm/01-ai/databricks/nvidia/voyage/perplexity/writer/cerebras). Aggregator providers (kilo, vercel, 302ai, etc.) are filtered out so they can't pollute capability flags (one of them once labeled a Zhipu model as audio-capable; filtering eliminated the noise). Booleans OR-merge; numerics MAX; this represents the consensus-best-guess from the community.
   - **Phase 2 — vendor-API overlay** (opt-in). When `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`, or `GEMINI_API_KEY` / `GOOGLE_API_KEY` are present, `scripts/_vendor_sources.py` hits the vendor's own `/models` endpoint and OVERWRITES community-merge values field-by-field — the vendor is authoritative for its own catalog. Vendor data is sparse: only fields the vendor explicitly asserts are applied, so the community merge fills in everything else. Each model record carries an `_authority` field (`"vendor"` or `"merge"`) and a `_vendor_sources` list naming exactly which vendor entries contributed. Vendor adapter failures (no key, network down, parse error) are silent — the script logs the skip reason in `vendor_sources.skipped` in the envelope and the community-merge phase remains the fallback. Currently rich-data vendors wired: Anthropic (`capabilities.{image_input, pdf_input, structured_outputs, thinking, …}`), Mistral (`capabilities.{vision, function_calling, …}` + aliases), Gemini (`inputTokenLimit` + `outputTokenLimit` + `supportedGenerationMethods` + `thinking`). Bedrock / Fireworks / Cohere have rich endpoints too but require extra dependencies (boto3 for Bedrock) or are lower priority; left for follow-up.
3. **Heuristic by model prefix** — last-resort recognition for custom/local models (`claude-*` → vision, `glm-*` → text-only, etc.).
4. **Pessimistic default** — all capabilities False; safe under-promise.

The returned dataclass carries a `source` field naming the layer that produced it; consumers that need authoritative data (e.g. capability bridges deciding when to expose themselves) gate behavior on `source in {"override", "snapshot"}`. Within the snapshot, the per-entry `_authority` flag distinguishes vendor-confirmed entries from community-only ones — useful for diagnostics and for a future flag that requires `_authority == "vendor"` before exposing a bridge tool.

### Capability bridges (aux models)

When the primary model lacks a modality (vision, audio) but the user has declared an `aux_model` in config, durin exposes a delegating tool that ships one-shot questions to the aux:

- `aux_models.vision` → `interpret_image(image_path, question)` — accepts PNG/JPEG/GIF/WEBP, base64-encodes, ships to the aux as an `image_url` content block, returns the aux's text answer.
- `aux_models.audio` → `interpret_audio(audio_path, question)` — accepts WAV/MP3/M4A/OGG/FLAC/WebM, ships as `input_audio` block. Chat-multimodal aux only (Gemini 2.5 Flash works; Whisper-style transcription-only models are a separate future `transcribe_audio` tool because their endpoint is `/v1/audio/transcriptions`, not chat completions).

Aux providers are built once at startup by `loop._build_aux_providers(config)` and handed to tools through `ToolContext.aux_providers`. Tools gate themselves via their `enabled(ctx)` classmethod — without an aux configured, the tool never appears in the model's tool list. Config-driven, not runtime-detected.

### Prompt caching

`_apply_cache_control` stamps Anthropic-style `cache_control: {type: ephemeral}` on system + last user content + last tool definition for providers with `supports_prompt_caching=True` in the registry (Anthropic, OpenRouter). For other providers using automatic prefix caching (Zhipu/MiniMax/DeepSeek/Qwen/Mistral/xAI/StepFun/Moonshot), no markers are needed — they cache transparently as long as the prefix is stable. The `cached_tokens` field is normalized across all providers (`prompt_tokens_details.cached_tokens`, `cached_tokens`, `prompt_cache_hit_tokens`, `cache_read_input_tokens` all map to the same key), and `AgentProgressHook.after_iteration` emits a structured `cache.usage` telemetry event per turn so the savings are observable.

### Token accounting

`build_assistant_message(..., prompt_tokens=...)` stamps the provider-reported `prompt_tokens` onto persisted assistant messages as `usage_prompt_tokens`. `latest_prompt_tokens_anchor(messages)` walks backward to find the most recent stamp; `estimate_prompt_tokens_chain` uses that as an authoritative baseline and tiktoken-estimates only the tail. Cuts systematic over-estimation on long sessions.

---

## 11. Interactive CLI (daily driver)

The interactive CLI lives in `durin/cli/`. It routes input through the
same `MessageBus` that all channels (Slack, Telegram, etc.) use, so
agent behaviour is identical between channels. The CLI-specific
ergonomics that make it usable as a daily driver:

### 11.1 Slash commands

`durin/command/builtin.py` registers the canonical set on `CommandRouter`.
The handler returns an `OutboundMessage`; the CLI loop renders it.

| Command | Purpose |
|---|---|
| `/new` | Stop the current task and start a fresh conversation. |
| `/stop`, `/restart`, `/status` | Process control + diagnostics. |
| `/model [preset]` | Show or switch the active model preset. |
| `/history [n]` | Print last N persisted messages. |
| `/goal <task>` | Mark a long-running goal. |
| `/plan`, `/build`, `/mode` | Agent-mode (plan / build / explore) control. |
| `/dream`, `/dream-log`, `/dream-restore` | Manual memory consolidation + history. |
| `/pairing` | Multi-device pairing flow. |
| `/sessions [filter]` | List saved sessions, sorted by updated_at. |
| `/resume <key>` | Switch the active chat to another saved session (in-place, no restart) via metadata directive `_switch_chat_id`. |
| `/compact [hint]` | Manually consolidate the unconsolidated tail of the current session. |
| `/copy` | Copy last assistant message to clipboard (`pbcopy` / `xclip` / `wl-copy` / `clip`). |
| `/name <name>` | Set / show session display name (`session.metadata['display_name']`). |
| `/hotkeys`, `/help`, `/quit` (alias `exit` / `:q`) | Discoverability. |

### 11.2 Editor ergonomics

`durin/cli/commands.py:_init_prompt_session` builds the `PromptSession`
with three optional capabilities, each gated by what the caller passes:

- `workspace` → `FileReferenceCompleter` (`durin/cli/completers.py`).
  Type `@` after whitespace to fuzzy-substring-match workspace files.
  Cached walk (max 1000 files) with sensible excludes
  (`.git`, `__pycache__`, `.venv`, `node_modules`, `.durin`, …).
- `presets_getter` → `ModelPresetCompleter` plus a Ctrl+L key binding
  that pre-fills the buffer with `/model ` to start a picker flow.
- `footer_getter` → `bottom_toolbar`-driven persistent footer
  (`durin/cli/footer.py`). On every redraw renders
  `session · model (preset) · ~tokens/window (%) · mem:N vec✓|✗`.
  Failures in the getter are swallowed so the prompt never blocks.

### 11.3 Drag-and-drop

`durin/cli/dragdrop.py` pre-processes user input before the bus publish:

1. Scan for absolute paths in the typed text (bash-style escaped
   spaces handled, `~` expansion supported).
2. For each existing file:
   - Image (.png/.jpg/.gif/.webp/.bmp/.svg) or audio (.mp3/.wav/.m4a/
     .flac/.ogg/.opus) → copy to `<workspace>/.media/<sha>.<ext>` (idempotent
     by content hash), replace the path in the text with the copy
     path, surface the workspace-relative path via `InboundMessage.media`
     so the existing multimodal pipeline picks it up.
   - Document (markdown / text / pdf) → leave the path untouched so
     the agent's `read_file` tool can resolve it directly.
3. Unsupported extensions, non-existent paths, and directories are
   left as-is.

### 11.4 Session switching mechanism

`/resume <key>` returns an `OutboundMessage` whose metadata carries
`_switch_chat_id`. `run_interactive`'s `_consume_outbound` watches
for that key and updates `cli_chat_id` via `nonlocal`. The next
`bus.publish_inbound` uses the new key, routing the next turn to the
selected session — no process restart needed. The persistent footer
sees the change on its next redraw because it closes over the same
`cli_chat_id` binding.

---

## 12. Textual TUI (opt-in, D5)

A second interactive surface lives under `durin/cli/tui/`. It runs
on top of the same `MessageBus` and `AgentLoop` as the legacy CLI;
the only thing that's different is the I/O layer. Launched via
`durin agent --tui`.

### 12.1 Layout

```
┌─ HeaderBar ────────────────────────────────────────────────────┐
│ durin · <workspace> · <model> (<preset>)                       │
├────────────────────────────────────────────────────────────────┤
│                                                                │
│  ChatView (scrollable, mouse-friendly)                         │
│    MessageBubble role=user      "the user message"             │
│    MessageBubble role=assistant "streamed assistant reply"     │
│    MessageBubble role=tool      "tool call result"             │
│    MessageBubble role=system    "command / system note"        │
│                                                                │
├────────────────────────────────────────────────────────────────┤
│ InputArea  (suggester: /commands  and  @files)                 │
├────────────────────────────────────────────────────────────────┤
│ FooterBar  session · model · ~tokens/window (%) · mem · vec   │
└────────────────────────────────────────────────────────────────┘
```

Each piece is a separate widget in `durin/cli/tui/widgets/`. Widget
CSS lives next to the widget; app-level CSS in
`durin/cli/tui/durin.tcss`.

### 12.2 Bus integration

`DurinApp.on_mount` spawns two background tasks:

- `agent_loop.run()` — inbound dispatcher.
- `_consume_outbound` — drains `bus.consume_outbound()`, maps
  metadata flags to widget operations:

  | metadata flag         | effect                                          |
  |---|---|
  | `_stream_delta`       | append to the open assistant bubble             |
  | `_stream_end`         | close the assistant bubble                      |
  | `_streamed`           | end-of-turn marker (no UI side-effect)          |
  | `_switch_chat_id`     | mutate `cli_chat_id` + refresh footer / header  |
  | `render_as="text"`    | render as a system bubble                       |
  | (other)               | render as an assistant bubble                   |

User submission goes through the same pipeline as the legacy CLI:
surrogate-sanitize → drag-and-drop pre-processor → publish
`InboundMessage(_wants_stream=True, media=[...])`.

### 12.3 Editor ergonomics (parity with D1)

- `SlashCommandSuggester` — `/` prefix surfaces a known command (palette source: `BUILTIN_COMMAND_SPECS`).
- `AtFileSuggester` — `@<prefix>` matches workspace files (same exclude rules as `FileReferenceCompleter`).
- `MultiModeSuggester` — dispatcher between the two.
- Drag-and-drop pre-processor reuses `durin.cli.dragdrop.process_dragged_paths`; images / audio land in `<workspace>/.media/<sha>.<ext>` and ride `InboundMessage.media`.

### 12.4 Key bindings

| Binding         | Action                                               |
|---|---|
| `Ctrl+Q` / `Ctrl+D` | quit                                             |
| `Escape`        | abort: calls `agent_loop._cancel_active_tasks` and clears the open assistant bubble |
| `Ctrl+T`        | toggle dark/light theme                              |
| `Ctrl+L`        | pre-fill input with `/model ` so the suggester surfaces presets (modal in a follow-up) |

### 12.5 What ships in D5 vs deferred

Shipped in D5: layout, streaming, slash commands, @file completion,
drag-and-drop, key bindings, persistent footer, surrogate sanitisation,
theme toggle, parity with the legacy CLI through the `MessageBus`.

**Deferred** to a follow-up (per `docs/10_textual_migration.md` §D5.5):
modal pickers for `/sessions` and `/model`. The base TUI works without
them — `/sessions` renders as a markdown list inside a system bubble,
Ctrl+L pre-fills `/model ` for the autocomplete.

---

## 13. Testing

```
tests/
├── agent/          # Loop, runner, context, hooks, modes, capability bridges,
│                   #   tool-call meta events, context_transform, anchor
├── agent/tools/    # Per-tool tests — todo_write, sleep, ask_user, session_search,
│                   #   subagent_lifecycle, interpret_image/audio, sleep, …
├── api/            # HTTP/SSE/WebSocket
├── bus/            # Message bus
├── channels/       # Channel adapters
├── cli/            # CLI rendering + truncate-direction tests
├── command/        # Commands (/plan, /build, /mode, …)
├── config/         # Schema and loader
├── cron/           # Cron service + cron tool update action
├── providers/      # Provider adapters + capabilities resolver + snapshot
├── session/        # Session lifecycle, goal state, todo_state, session_meta,
│                   #   tool_call meta events
├── skills/         # Skill loading + disable_model_invocation gating
└── telemetry/      # Generic logger + cache.usage event
```

Total: **~4,050 tests passing, 15 skipped** (Python) + **~134** (webui).

---

## 14. Lifecycle commands (D6)

`durin` ships dedicated commands for install/configure/upgrade/uninstall so
the operator never has to hand-edit JSON or guess where state lives.

| Command | Module | What it does |
|---|---|---|
| `durin onboard [--wizard]` | [durin/cli/commands.py](../durin/cli/commands.py) (`onboard`) | Creates `~/.durin/config.json` + workspace; `--wizard` runs the questionary form. |
| `durin status` | same | High-level: paths, model, which providers are configured. |
| `durin config path \| show \| get \| set \| edit` | [durin/cli/config_cmd.py](../durin/cli/config_cmd.py) | Read/write single keys via dotted paths. Secrets masked by default. |
| `durin upgrade [--check\|--migrate-only\|--ref]` | [durin/cli/upgrade.py](../durin/cli/upgrade.py) | Detects editable vs wheel install; pulls + reinstalls + replays config migration. |
| `durin uninstall [--purge --keep-* --workspace]` | [durin/cli/uninstall.py](../durin/cli/uninstall.py) | Enumerates state under `~/.durin` + `~/.cache/durin`, prompts, deletes. `--purge` self-uninstalls in a subprocess. |
| `durin doctor [--ping --fix --json]` | [durin/cli/doctor.py](../durin/cli/doctor.py) | Runs a battery of small checks (system / config / providers / tools / extras / state) and exits non-zero on any `fail`. |
| `durin provider login/logout`, `durin channels login/status` | [durin/cli/commands.py](../durin/cli/commands.py) | OAuth + channel-specific auth (kept separate because they aren't single-key edits). |

### Config edit pipeline

`durin config set <dotted.path> <value>` runs:

1. Read raw JSON from disk.
2. Validate via `Config.model_validate(...)`, then re-dump with `by_alias=True`
   to canonicalize alias form (camelCase). This guarantees the dict we mutate
   only ever has one set of keys (avoids parallel `apiKey: null` + `api_key:
   "sk-…"` pollution that pydantic's alias-first resolution would silently
   drop).
3. Normalize the user's dotted path (snake_case → camelCase per segment) so
   `providers.zhipu.api_key` lands at `providers.zhipu.apiKey`.
4. Apply the mutation, re-validate the whole tree. On `ValidationError`,
   leave the file untouched and print the pydantic message.
5. Save through `durin.config.loader.save_config`, which re-runs
   `_apply_ssrf_whitelist` and any pending schema migrations.

### State paths the uninstaller knows about

Grouped by `--keep-*` flag:

- **`--keep-config`**: `~/.durin/config.json`, `~/.durin/config.json.bak`, `~/.durin/pairing.json`
- **`--keep-workspace`**: `~/.durin/workspace/`
- **`--keep-cache`**: `~/.cache/durin/{telemetry,models,archive}/`
- Always cleaned (no opt-out flag): `~/.durin/{sessions,history,cron,media,bridge,webui,logs}/`
- Only when `--workspace <path>` is passed: `<path>/.durin/{plans,spills,tool-results}/`

The plan table renders absolute paths + recursive byte counts before
prompting, so the user sees the blast radius before consenting.

### Install-mode detection

`durin upgrade` inspects `Path(durin.__file__).parent.parent`. If that path
contains a `pyproject.toml`, the install is treated as **editable**:
`git pull --ff-only` (optionally preceded by `git checkout <ref>`) followed
by `pip install -e .`. Otherwise (running from `site-packages/`), it's a
**wheel** install and we run `pip install --upgrade durin`. `--check`
prints the detected mode + version and exits without touching pip.
`--migrate-only` skips the package step entirely and just re-saves the
config through the load/validate/dump pipeline, picking up any new schema
defaults.

### Doctor checks (D7)

`durin doctor` runs a flat list of independent check functions, each
returning a `CheckResult(name, status, message, fix?, category)` with
`status ∈ {ok, warn, fail}`. The orchestrator:

1. Collects results from every check.
2. Groups them by category for rendering.
3. Computes `worst = max(status)` and exits non-zero only when `worst ==
   fail`. `warn` results show up with a suggested fix but don't break the
   exit code, so the command is safe to wire into CI.

Checks currently run (all in [durin/cli/doctor.py](../durin/cli/doctor.py)):

- **system**: Python version (>= 3.11), durin version.
- **config**: `~/.durin/config.json` exists / parses as JSON / validates
  against the Pydantic schema; workspace exists + is writable; both
  `~/.durin` and `~/.cache/durin` are writable.
- **providers**: at least one provider is usable (api_key, OAuth token
  file, or `api_base` for local); the active model preset resolves.
- **tools**: `git` (warn if missing — needed for editable upgrades).
- **extras**: `fastembed`, `lancedb`, `mcp` import successfully (warn
  with the matching `pip install 'durin[extra]'` fix if not).
- **state**: `~/.cache/durin` byte count (warn at > 10 GB with a
  reference to `durin uninstall --keep-config --keep-workspace`).
- **providers (opt-in via `--ping`)**: HTTP GET against the configured
  provider's `api_base` with a 3-second timeout.

`durin doctor --fix` applies the safe subset of automated fixes: it
creates the workspace directory if missing and replays the config
migration (the same one `durin upgrade --migrate-only` runs). Anything
that involves an API key or destructive action is left for the user.

---

## 15. Distribution (D8)

The PyPI distribution is **`durin-agent`** (the bare `durin` name was
already taken by an unrelated robot-control project). The import package
stays `durin` and the CLI command stays `durin`; only the
`pip install` / `pipx install` argument changes.

Two artifacts ship per release, both produced by `python -m build`:

- `durin_agent-<version>.tar.gz` — source distribution
- `durin_agent-<version>-py3-none-any.whl` — pure-Python wheel

The wheel bundles the webui under `durin/web/dist/` via
[hatch_build.py](../hatch_build.py); editable installs skip this hook
since `cd webui && bun run dev` is the dev loop.

### Release pipeline

[.github/workflows/release.yml](../.github/workflows/release.yml) fires
on tags matching `v[0-9]+.[0-9]+.[0-9]+*`. The workflow:

1. **build** — checks out the repo, installs Python 3.11 + bun, asserts
   the tag matches `pyproject.toml`'s version, runs `python -m build`,
   uploads artifacts as an Actions artifact.
2. **github-release** — downloads the artifacts and creates a GitHub
   Release with auto-generated notes. Marked as `prerelease: true` when
   the tag carries an `aN`/`bN`/`rcN`/`devN` suffix (PEP 440).
3. **pypi-publish** — downloads the same artifacts and publishes them
   via `pypa/gh-action-pypi-publish` (OIDC trusted publishing — no API
   tokens stored in the repo). Marked `continue-on-error: true` so a
   misconfigured PyPI publisher doesn't block the GitHub Release.

Tag → release is the only path. There is no manual upload step.
Maintainer instructions live in [docs/RELEASING.md](RELEASING.md).

### CI pipeline

[.github/workflows/ci.yml](../.github/workflows/ci.yml) runs on every PR
and every push to `main`. It installs durin with `[dev]` + lightweight
extras and runs the full `pytest` suite with `--maxfail=5`.

---

## 16. Config layout — split files (D-config)

The config lives as **per-topic files** under `~/.durin/config.json.d/`
rather than one monolithic `config.json`:

```
~/.durin/
    config.json          # 1-line marker: {"_layout": "split"}
    config.json.d/       # per-topic files
        agents.json  providers.json  channels.json  memory.json
        gateway.json tools.json      api.json        install.json
    config.json.legacy   # backup of the pre-split monolith
```

- **Migration is automatic**: the first `load_config` on a legacy
  monolith splits it, backs the original up as `config.json.legacy`,
  and rewrites `config.json` as a marker. See
  [durin/config/loader.py](../durin/config/loader.py).
- **`save_config` writes only non-defaults** (`exclude_defaults=True`)
  then prunes noise: empty provider sections and disabled channels that
  match their shipped default are dropped. *Enabled* channels keep their
  full attribute set so every editable field stays discoverable.
- `read_persisted_config()` is the layout-agnostic reader used by
  tooling + tests.

## 17. Status vs Doctor

Two distinct surfaces, deliberately non-overlapping:

- **`durin status`** — a factual snapshot. Sectioned (Model / Providers
  / Channels / Gateway / Memory / Config), shows only what's configured
  (no dump of all 25 registry providers), passes no judgement. The
  `git status` of durin.
- **`durin doctor`** — health diagnostics. Every check is ok/warn/fail
  with an actionable fix; exit code flips on `fail`. The `flutter
  doctor` of durin.

## 18. Gateway daemon mode

`durin gateway` is a Typer sub-group. With no subcommand it honours
`config.gateway.daemon`: `false` → foreground, `true` → detach. Explicit
lifecycle: `gateway start | stop | restart | status | logs`. PID file at
`~/.durin/gateway.pid`, logs at `~/.durin/logs/gateway.log`. The webui
dashboard is auto-served when `config.gateway.webui_enabled` is true
(the websocket channel is enabled at runtime). `durin doctor` verifies
both the daemon and webui when config requests them.

## 19. Secrets subsystem

Full design: `docs/11_secrets_design.md`. API keys are no longer stored
inline in `config.json`. The store is `~/.durin/secrets.json` (mode
`0600`, outside the config tree). Each entry has two axes — `service`
(classification, non-unique) and `scope` (consumer authorization) —
plus account/description/origin.

Config fields hold a `${secret:NAME}` reference; `resolve_secret()`
turns it into plaintext lazily at the point of use, so the value never
re-enters the `Config` object, logs, or telemetry. Wired into
`Config.get_api_key()` and the provider factory.

`durin secret` manages the store; `durin secret migrate` moves
pre-existing plaintext provider keys in. The onboard wizard writes new
keys straight to the store as references.

Resolution is wired into providers, the web-search tool, and channel
construction — secrets work everywhere config expects a credential.

Phase 2: the agent runner redacts stored secret values out of every
tool result before it reaches the model (`SecretRedactor`); `ExecTool`
injects `exec`-scoped secrets into the subprocess env so scripts use
them without the agent ever seeing the values.

Agent tools (`agent/tools/secrets.py`): `list_secrets` lets the agent
discover available credentials (metadata only); `request_secret`
yields with the exact `durin secret set` command when the agent needs
one it lacks — the user runs it, the value never passes through the
agent. `durin doctor` flags dangling `${secret:}` references.

---

## Last updated: 2026-05-22 (secrets subsystem Phase 1+2)

> For the history of why each subsystem was added, what was replaced, and what was discarded along the way, see `docs/02_bitacora.md`. This document only describes the current state.
