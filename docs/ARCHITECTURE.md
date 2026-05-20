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

### Phase 1 hardening (May 2026)

After validating with V3–V9d that cognitive scaffolding adds little to no value on frontier-reasoning models (see `02_bitacora.md`), three infrastructure changes were added to the loop. These target the boundary between the model and the environment — the only place where execution-loop interventions still show empirical value:

**1A — Hash-based loop detection** (`runner.py::_run_tool`).
Frontier models occasionally fixate: they emit the same `(tool_name, arguments)` tuple in consecutive turns even after the prior call hard-failed. The fix is pure state tracking — a turn-scoped set of failed signatures (`sha256` of `tool_name + json.dumps(args, sort_keys=True)`). On a repeat hit we short-circuit with a synthetic "BLOCKED" tool result asking the model to take a different path. Per-turn scope only — we never block across turns because environment state may have changed. Pytest-style failures (tool succeeded but environment reported failure) are NOT recorded as failed signatures, so test-driven iteration loops continue to work.

**1B — Topological tool ordering** (`runner.py::_partition_tool_batches`).
The model can emit mixed tool calls like `[read_a, write_b, read_c]`. We never reorder; we walk the list and group only CONSECUTIVE `concurrency_safe` tools into a parallel batch. Mutations and exclusive tools get their own singleton batches. This prevents `[edit_file, run_tests]` race conditions while preserving the read-before-write / read-after-write semantics the model depends on. Tools default to `read_only=False` — opt-in safety.

**2B — Reasoning-phase truncation recovery** (`runner.py` length-handling branch).
Reasoning models (glm-5.1, o-series, Claude thinking) can hit `max_tokens` while still inside their `reasoning_content` deliberation, producing `finish_reason="length"` with empty `content` but a large reasoning blob. The default empty-retry path is harmful here (re-sends the same prompt). We detect this specific signature and append the partial reasoning plus a specialized cue asking the model to wrap up quickly and emit the final answer or tool calls. Preserves the chain-of-thought, avoids wasting the tokens already spent.

**2C — Idle-timeout circuit breaker** (`runner.py` top of iteration loop). OpenClaw-inspired. The provider already retries individual transient timeouts; a timeout reaching the runner means those retries were exhausted. Tolerating one such event is fine — the next iteration may succeed after an injection or context repair — but multiple in a row burn tokens against a stalled endpoint. The runner counts iterations whose response is an idle/wall-clock timeout (`finish_reason="error" and error_kind="timeout"`); any iteration that produced forward progress (tool_calls or non-empty content) resets the counter. When it exceeds `DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS` (default 1, matching OpenClaw's `MAX_CONSECUTIVE_IDLE_TIMEOUTS_BEFORE_OUTPUT`), the run terminates with `stop_reason="circuit_breaker_idle_timeout"` and a `circuit_breaker.idle_timeout` telemetry event for distinct-from-error diagnostics.

**2D — Per-block tool-result validation** (`utils/tool_result_validation.py`, applied in `runner.py::_normalize_tool_result`). OpenClaw-inspired. The aggregate `max_tool_result_chars` cap + disk spillover handles textual outputs, but `stringify_text_blocks` bails out when a result list contains non-text blocks — so a tool returning a 30 MB base64 image (e.g. `image_generation`) would inject that straight into context. Per-block caps run *before* the aggregate path: text blocks > 100 KB are truncated in place (preserving sibling blocks), `image_url` data-URLs > 5 MB are replaced with a text placeholder, `input_audio` data > 10 MB likewise. HTTP/HTTPS image URLs pass through (they're references, not payloads). Unknown block types pass through untouched so provider adapters can still sort out Anthropic-style `tool_result`/`tool_use` blocks.

**2E — Re-sanitize after `context_transform`** (`runner.py::_build_request_kwargs`). OpenClaw-inspired. The pre-call sanitize pipeline (drop_orphan_tool_results + backfill_missing_tool_results, both before AND after `_snip_history`) runs on the *untransformed* message list. A `context_transform` hook that trims for token budget can then drop a message in the middle of a `tool_use`/`tool_result` pair, producing a 400 from Anthropic/OpenAI (`tool_use_id ... was not found`). The runner now re-runs the sanitize pass on the transformed list before handing it to the provider; the re-sanitize is wrapped in `try/except` so a bug in repair never breaks the request.

**2F — Compaction grace window** (`runner.py::_await_with_compaction_grace`, wired in `loop.py` via `Consolidator.get_lock(session_key).locked()`). OpenClaw-inspired (`run/compaction-timeout.ts::resolveRunTimeoutDuringCompaction`). The outer wall-clock LLM timeout (`DURIN_LLM_TIMEOUT_S`) is necessary to bound runaway providers, but it kills calls that are slow precisely *because* consolidation is mid-flight reshaping the context. When the base timeout fires, the runner checks the optional `is_compacting` callback on `AgentRunSpec`. If True, it extends the deadline once by `DURIN_COMPACTION_GRACE_S` (default 30 s) and emits a `compaction.grace_extended` telemetry event. If the call still doesn't return, the regular `TimeoutError` path fires. Implemented with `asyncio.wait({task}, timeout=...)` rather than `asyncio.wait_for` so the task isn't cancelled at the base timeout — allowing us to probe state and keep waiting on the same coro. Grace is one-shot per request.

**2G — Per-model `parallel_tool_calls` gating** (`OpenAICompatProvider._resolve_parallel_tool_calls`, configured via `agents.defaults.parallel_tool_calls` in user config). OpenClaw-inspired. Some models misbehave when sent OpenAI's `parallel_tool_calls=true` request flag — they over-emit calls, hallucinate args, or 400. The config maps model-name substrings to True/False; first match wins. The provider injects `parallel_tool_calls` into the request kwargs only when there's a match AND `tools` is non-null (the API rejects the flag in tool-less calls). No injection by default — provider default is preserved.

**2H — Per-turn aggregate tool-result budget** (`runner.py::_enforce_turn_budget`). Hermes-inspired (`tools/tool_result_storage.py::enforce_turn_budget`). The per-tool cap + disk spillover catches single huge results, but when an LLM emits N parallel calls each returning <per-tool cap, the aggregate can still overflow context. After all tool results in a turn are collected, the runner sums their sizes; if the total exceeds `DURIN_TURN_BUDGET_CHARS` (default 200 KB), the largest not-yet-persisted results are spilled to disk in priority order (largest first) until the aggregate fits. Already-persisted results (containing the `[tool output persisted]` marker) are skipped to avoid double-work. Mutates the appended tool messages in place. Emits a `turn_budget.enforced` telemetry event when triggered. `DURIN_TURN_BUDGET_CHARS=0` disables.

**2I — Heartbeat isolated sessions** (`heartbeat/service.py::heartbeat_session_key`, wired in `cli/commands.py::on_heartbeat_execute`). OpenClaw-inspired. Default behaviour is unchanged — the heartbeat reuses one long-running session named `heartbeat` and trims via `retain_recent_legal_suffix(keep_recent_messages)` between ticks. With `heartbeat.isolatedSessions=true` each tick gets a fresh `heartbeat-<12-hex>` session that the executor deletes (cache + disk) after the run. Useful when heartbeat tasks are stateless one-shots (e.g. "did anything change since last tick?") and shouldn't drift from accumulated context.

### Tier 2 — Resilience (Phase 2A)

**3A — Pre-emptive compaction trigger** (`agent/memory.py::Consolidator._preemptive_trigger_tokens`). OpenClaw-inspired (`preemptive-compaction.ts`). Old behaviour: the consolidator only fired when `estimated_tokens > input_token_budget` — i.e. when we were already at the context wall. New: fires when `estimated_tokens > preemptive_compact_ratio * context_window` (default 0.5), so a turn that would have shipped ~93% of the window now compacts at ~50% instead. Per-preset via `ModelPresetConfig.preemptive_compact_ratio` — a 1M-window model wants ~0.15 (compact at 150K — paying per token shipped, you don't want to wait until 500K), a 128K model is fine at 0.5. Falls back to `AgentDefaults.preemptive_compact_ratio` when the preset doesn't override. Clamped above by the input budget so a misconfigured 0.99 still leaves a safety margin. `consolidation_ratio` was re-based off the new trigger (instead of the budget) so each compaction round still does meaningful work after the threshold drops. Emits `compaction.preemptive_trigger` telemetry when the pre-emptive threshold fires below the legacy budget ceiling.

**3B — Mid-turn precheck signal** (`agent/runner.py::_mid_turn_precheck`). OpenClaw-inspired (`midturn-precheck.ts`). A1 covers the *start* of a turn — consolidator runs before `_run_agent_loop`. But intra-turn growth (a 60 KB tool result, an image block surviving truncation) can push the post-sanitize prompt back over budget. After each iteration's sanitize pipeline, estimate the message + tool token cost; if it exceeds the input budget, abort the turn with `stop_reason="mid_turn_precheck_overflow"` and a `mid_turn_precheck.overflow` telemetry event BEFORE making the LLM call. Saves the wasted call that would have returned a 400 anyway and gives callers a distinct stop_reason. The next turn re-runs A1, compacting first. Estimator exceptions are swallowed (best-effort gate — never block a turn on a broken counter).

**3C — Compaction lock aggregate timeout** (`agent/memory.py::Consolidator._lock_timeout_s`). OpenClaw-inspired (`compaction-retry-aggregate-timeout.ts`). The per-session compaction lock (`asyncio.Lock`) used to be `async with`-ed unbounded. If a compaction hung mid-summarize (provider stuck, network freeze), the next call to `maybe_consolidate_by_tokens` on that session would wait on the lock indefinitely — the session lane silently dies. Now bounded by `DURIN_COMPACTION_LOCK_TIMEOUT_S` (default 180s). When the timeout expires, the call abandons the acquisition and returns without consolidating (an oversized prompt is recoverable; a hung session is not) and emits a `compaction.lock_timeout` telemetry event. `0` disables the timeout (legacy unbounded behaviour). The `async with` was rewritten as `acquire()` + `try/finally release()` so the body still releases on success and on raise — including when the body raises mid-compaction.

**What we deliberately did NOT add**:
- Forced-verification gate (refuted as PlanHook in V7/V8 — 0 hits, hurt scenario_3 by 2pp). Conditional version remains a Phase 2 candidate.
- Semantic friction injection on errors (cognitive manipulation; contradicts the empirical pivot — see `02_bitacora.md`).
- MCTS/tree-search wrapping the LLM (cost prohibitive for sync workflows; semantic search adds little value when the model already reasons internally).

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
│   │                                    # (LiteLLM + OpenRouter + models.dev,
│   │                                    #  ~785 models, regenerated by
│   │                                    #  scripts/refresh_model_capabilities.py)
│   ├── anthropic_provider.py / bedrock_provider.py / openai_codex_provider.py
│   ├── openai_compat_provider.py       # cache_control ephemeral for caching
│   ├── azure_openai_provider.py / github_copilot_provider.py
│   ├── local_llama_provider.py / fallback_provider.py
│   └── transcription.py / image_generation.py
├── security/              # Auth, secrets, permissions
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
└── refresh_model_capabilities.py    # dev tool — regenerates the consensus
                                     # snapshot from the 3 source feeds,
                                     # filtered by TRUSTED_VENDORS whitelist
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

Tool-level instrumentation (Phase 1c, May 2026):
- `read_file` emits `tool.read_file` events with `{path, offset, limit, total_lines, returned_lines, result_chars, kind, truncated, dedup}` on the successful text-read and dedup paths.
- `grep` emits `tool.grep` events with `{pattern_len, fixed_strings, case_insensitive, output_mode, limit, offset, glob_filter, type_filter, displayed, total_before_pagination, result_chars, truncated, size_truncated, skipped_binary, skipped_large}` on every non-error completion (including zero-match).
- `edit_file` emits `tool.edit_file` events with `{path, match_strategy, matches, outcome, old_text_chars, new_text_chars}` for every call. `outcome` ∈ `{edited, not_found, ambiguous}`. `match_strategy` ∈ `{exact, line_trimmed, line_trimmed_quote_normalized, quote_normalized, block_anchor, null}` — lets us measure how often each cascade layer earns its keep.
- `repo_overview` emits `tool.repo_overview` events with `{path, depth, ecosystems, package_manager, dependency_files_count, entrypoints_count, structure_lines, truncated, result_chars}`.
- `exec` emits `tool.exec.spill` events with `{spilled, original_chars, rendered_chars, spill_path, spill_error}` when output exceeds the cap and gets spilled to disk via `truncate_with_spill`.
- No event is emitted on error paths — by design, the goal is measuring information-loss patterns, not failure counts. Telemetry failures are silently swallowed so tool calls never break from a logging issue.

The instrumentation is in service of validating SWE-agent's "tool I/O quality > loop quality" finding in our specific setup (frontier reasoning models with 1M context). The implementation does not change defaults — that decision comes after enough telemetry has been collected over real workloads. See `docs/01_roadmap.md` Phase 1c and `docs/02_bitacora.md` for rationale.

### Sprint A — Tool I/O hygiene additions (May 2026)

Implemented per `docs/07_external_agents_review.md` §6-§9. Four language-agnostic, validated patterns ported from OpenCode / SWE-agent:

**T3 — Read suggestion-on-miss.** `ReadFileTool` now suggests close-named files in the same directory when the requested path doesn't exist. Shared helper `_file_not_found_msg` lives on `_FsTool` and is also used by `EditFileTool`. Uses `difflib.get_close_matches(cutoff=0.6, n=3)`.

**T1 — `repo_overview` tool** (`durin/agent/tools/repo_overview.py`). One-shot orientation: depth-bounded directory tree (default 3, capped at 200 entries) plus detected ecosystems (Python, Node.js, Go, Rust, Ruby, Java/Kotlin, PHP), package manager (npm/pnpm/yarn/bun/poetry/cargo/etc.), dependency files, common entrypoints. No embeddings, no AST — purely structural. Lets the model orient before grep/list_dir. Inherits from `_FsTool` for telemetry + path resolution.

**T4 — Tool output spill** (`durin/agent/tools/output_spill.py`). When a tool produces output larger than its budget, the FULL content is written to `<workspace>/.durin/spills/<tool>_<ts>_<hash>.txt` and the model receives head+tail of the budget plus a reference (`read_file(path=...)`) to recover the omitted middle. Wired into `ExecTool` (10K-char threshold). Spill write failures fall back to plain head/tail truncation — never breaks the tool call.

**T2 — Block-anchor matcher in the edit cascade** (`filesystem.py::_find_block_anchor_matches`). Adds a fifth fallback to `EditFileTool`'s replacer chain: when `old_text` has 3+ lines, match first and last lines exactly (after strip) and use a similarity threshold on the middle. Relaxed threshold (0.5) when a single candidate exists, strict (0.85) with multiple. Handles cases where the model knows the start and end of a block but the interior shifted (reformatting, added comments, whitespace changes). Cascade order: `exact → line_trimmed → line_trimmed_quote_normalized → quote_normalized → block_anchor`. The strategy used is reported in `tool.edit_file` telemetry so we can measure how often each layer fires.

### Sprint B — Permission-as-data agent modes (May 2026)

Implemented per `docs/07_external_agents_review.md` §L3. Plan / Build / Explore modes selectable per session, with tool surface filtered at the LLM boundary by the active mode.

**Core** (`durin/agent/agent_mode.py`). `AgentMode` is a frozen dataclass with `allowed: frozenset[str] | None`, `denied: frozenset[str]`, and optional `prompt_suffix`. Three built-ins:
- `build` — default, no restriction
- `plan` — read-only + `exit_plan_mode` only; the model investigates and surfaces a plan for user approval
- `explore` — read-only for sub-agents (no exit affordance)

Session state lives in `session.metadata`:
- `agent_mode` — currently active mode name
- `pre_plan_mode` — set when entering plan mode, restored on exit (OpenClaude's `prePlanMode` pattern)

**Tool filtering in the runner** (`runner.py::_active_tool_definitions`). The runner accepts an optional `mode_provider` callable in `AgentRunSpec`; when present, it's called per iteration and the resulting mode filters the tool definitions sent to the LLM. The registry's cached definitions stay valid — filtering is a per-call slice. When the model emits a cached tool name that's no longer allowed (rare), `_run_tool` short-circuits with a clear denial: *"Tool 'X' is not available in mode 'plan'..."*. `AgentLoop._dispatch_message` wires the provider to read from `session.metadata` at call time, so mid-run mode switches take effect at the very next iteration.

**LLM-facing tools** (`durin/agent/tools/plan_mode.py`):
- `enter_plan_mode(reason?)` — switches the session into plan mode (the model may invoke this voluntarily; typically the user activates plan mode via `/plan` instead)
- `exit_plan_mode(plan)` — **writes the plan to `<workspace>/.durin/plans/plan_<timestamp>.md`** and yields to the user for approval. Does NOT actually exit plan mode — the session remains in plan until the user runs `/build`. While in plan, the user can edit the plan file directly with any editor; `/build` picks up the file content as-edited.

**File-based plan storage** (replaced an initial MVP with argument-string plan). Plans live in `<workspace>/.durin/plans/<session-slug>/plan_<timestamp>.md` — one subdirectory per session, one file per `exit_plan_mode` call inside it. The session slug is the sanitized session key (`websocket:chat42` → `websocket_chat42`), so concurrent chats don't collide and the user can locate plans for a specific conversation.

Benefits over the inlined-arg approach: persistence across context compaction, edit-before-approve UX (user opens the .md and tweaks step 3), multi-turn refinement (model rewrites the file), post-mortem review via `ls .durin/plans/<session>/`, and token efficiency (plan lives on disk, not in message history).

**Plan flow with compaction survival** (Claude Code parity, see `docs/07_external_agents_review.md`):

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

**Telemetry** (Phase 1c — extended):
- `agent_mode.turn_start` — emitted at the start of each `AgentRunner.run()` call with the active mode
- `agent_mode.switch` — emitted on every mode change, with `{from, to, trigger}` (`trigger ∈ {slash_command, tool}`)
- `agent_mode.tool_denied` — emitted when a tool call is denied by the mode (`{tool, mode}`)
- `plan_mode.presented` — emitted when `exit_plan_mode` surfaces a plan (`{plan_chars, from_mode}`)

**What we did NOT do, intentionally**:
- No "auto-resume on exit" — the user runs `/build` explicitly. This avoids the model executing without review.
- No mode-specific model override (OpenCode supports this; we don't see a case for it yet).
- No "ask" permission action (OpenCode supports `ask` to pop a UI dialog; we substitute the slash-command approval gate which works in every channel).

### Future improvements (Sprint B → daily-driver readiness)

Tracked here so the daily-driver path is explicit. None of these are blockers for the current implementation — they're per-channel UX polish.

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

`durin/session/manager.py` handles session lifecycle. Each session is a JSON-lines file containing message history, metadata, and goal state. `MemoryStore` (`durin/agent/memory.py`) handles long-lived markdown memory files (`MEMORY.md`, etc.) consumed by `ContextBuilder` into the system prompt.

**Session storage is immutable**: messages append-only, never trimmed. `Consolidator.maybe_consolidate_by_tokens` (in `durin/agent/memory.py`) advances a cursor (`last_consolidated`) when the prompt exceeds the budget — it generates a narrative summary, persists it to `history.jsonl` + `session.metadata["_last_summary"]`, and advances the cursor. **The raw `session.messages` list is never modified in-place**; only the cursor advances. The LLM sees `messages[last_consolidated:]` (capped by `max_messages`) + the summary, while disk retains the complete history for post-processing and the future memory subsystem.

In-memory per-turn shaping (does not touch disk):
- `_microcompact` in `runner.py` replaces older tool-result content with `[<tool> result omitted from context]` placeholders on the copy sent to the LLM
- `_snip_history` in `runner.py` further trims the copy from the start when it still doesn't fit the context window

### Session meta (per-session lifecycle index, May 2026)

Sessions are immutable but the message stream isn't enough to recover "significant events" from prior turns — plan submissions, plan approvals, mode transitions, etc. live in transient metadata that gets overwritten. The session meta file is a structured index of these events.

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
  ]
}
```

`type` is the discriminator for extensibility — today only `plan` is implemented, but the format is designed for future event kinds (review, deliberation, anything significant) to coexist without schema changes.

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

## 8. Sandboxing

Tool execution is sandboxed via `durin/agent/tools/sandbox.py`. Three backends:
- `bwrap` — Linux namespace sandbox (production)
- `docker` — Docker container (for benchmark-style isolation, see registration helpers)
- `testbed` — conda-env wrapper for running inside benchmark containers

The agent's exec tool routes through `wrap_command(sandbox, command, workspace, cwd)`.

---

## 9. Providers

`durin/providers/` ships adapters for Anthropic, OpenAI-compat (incl. Z.ai, OpenRouter, Azure, Ollama, LM Studio, Gemini, and 25+ others — see `registry.py`), Bedrock, GitHub Copilot, local llama-cpp, OpenAI Codex, and a fallback wrapper. `factory.make_provider(config)` resolves the active provider/model from config + presets.

### Capability metadata (capabilities.py + data/model_capabilities.json)

`get_model_capabilities(model, provider, overrides)` resolves a `ModelCapabilities` dataclass via a four-layer fallback:

1. **Explicit override** from `config.model_capabilities` — always wins. Use when adding a private/custom model the snapshot doesn't know about.
2. **Vendored consensus snapshot** at `providers/data/model_capabilities.json`. Built by `scripts/refresh_model_capabilities.py` from LiteLLM + OpenRouter + models.dev, filtered by a TRUSTED_VENDORS whitelist (anthropic/openai/google/zai/meta/mistral/deepseek/xai/qwen/moonshot/amazon/cohere/minimax/stepfun/ai21/ibm/01-ai/databricks/nvidia/voyage/perplexity/writer/cerebras). Aggregator providers (kilo, vercel, 302ai, etc.) are deliberately filtered out so they can't pollute capability flags (one of them once labeled a Zhipu model as audio-capable; filtering eliminated the noise).
3. **Heuristic by model prefix** — last-resort recognition for custom/local models (`claude-*` → vision, `glm-*` → text-only, etc.).
4. **Pessimistic default** — all capabilities False; safe under-promise.

The returned dataclass carries a `source` field naming the layer that produced it; consumers that need authoritative data (e.g. capability bridges deciding when to expose themselves) gate behavior on `source in {"override", "snapshot"}`.

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

## 10. Testing

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

Total: **3,293 tests passing, 15 skipped**.

---

## Last updated: 2026-05-20

> Latest pass: ARCHITECTURE refreshed for everything shipped since 2026-05-19. New / changed material:
> - **Tools roadmap items 1–9 + skill-disclosure refinement** (TodoWrite, Sleep, AskUserQuestion, session_search, subagent lifecycle + monitor, cron update, interpret_image, interpret_audio).
> - **Capability metadata + bridges** — full pipeline from `scripts/refresh_model_capabilities.py` consensus snapshot through `get_model_capabilities` resolver into `aux_providers` and the bridge tools.
> - **Tool-call meta timeline** — every tool call gets a `type=tool_call` event in `<session>.meta.json` with `msg_index` pointer, written centrally in `_save_turn` (no per-tool opt-in needed).
> - **Pi-inspired refinements** — `context_transform` hook, `disable_model_invocation` skill flag, head/tail truncation policy.
> - **Perf C-tier** — anchored token accounting via `usage_prompt_tokens` stamps + `cache.usage` telemetry event.
> - **Iteration flow updated** with the new steps (context_transform invocation, `_run_tool_timed` stamping, `cache.usage` emission).
> - **Module map rewritten** to include the new tool/session/provider files (and to remove the deleted `autocompact.py`).
> - Stale docs (`04_agent_strategies_catalog.md`, `05_log_swebench.md`, `06_log_experiments.md`) moved to `docs/archive/`.
