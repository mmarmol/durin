# arch / loop — agent loop, runner, sessions, providers

> The control-flow surface: how a turn flows from inbound message → LLM
> call → tool execution → response. Covers the iteration loop, runner
> guards, hook surface, agent modes, sessions, providers, sandboxing,
> and long-running goals.
>
> See [memory.md](memory.md) for the memory subsystem,
> [observability.md](observability.md) for telemetry/doctor/gateway,
> [ux.md](ux.md) for CLI/TUI surfaces.

---

## 1. Iteration flow

```mermaid
flowchart TD
    Start([InboundMessage]) --> Disp[AgentLoop _dispatch_message]
    Disp --> Run[AgentRunner run]
    Run --> Iter{iteration under max?}
    Iter -- no --> Done([OutboundMessage])
    Iter -- yes --> Gov[ctx governance: microcompact, snip, budget]
    Gov --> BI[hook before_iteration]
    BI --> Req[_build_request_kwargs + context_transform]
    Req --> LLM[provider chat completion]
    LLM --> Resp[response: content, tool_calls, reasoning]
    Resp --> Has{tool_calls present?}
    Has -- no --> Final[finalize content]
    Has -- yes --> BET[hook before_execute_tools]
    BET --> Batch[topological batching by concurrency_safe]
    Batch --> RunT[_run_tool_timed parallel batches]
    RunT --> Append[append tool results to messages]
    Append --> AI[hook after_iteration emits cache.usage]
    Final --> AI
    AI --> Iter
```

Driven by `AgentRunner.run()` in [durin/agent/runner.py](../../durin/agent/runner.py). Default `max_iterations = 200`. Hooks attach via the generic `AgentHook` interface — no hooks are bundled by default.

### Runner state-tracking + guards

All turn-scoped, defensive. They shape behaviour only when the model misbehaves or the environment fails.

| Guard | What it does | Where |
|---|---|---|
| **Loop detection** | `sha256(tool_name + sorted args)` of any HARD-failure tool call is cached for the turn; a repeat hit short-circuits with a synthetic "BLOCKED" tool result. Pytest-style soft failures are NOT recorded. | `runner.py::_run_tool` |
| **Topological tool ordering** | Walks the model's tool list in order, groups only CONSECUTIVE `concurrency_safe=True` tools into a parallel batch. Mutations + exclusives become singletons. Read-after-write semantics preserved. Tools default to `read_only=False` — opt-in safety. | `runner.py::_partition_tool_batches` |
| **Reasoning-phase truncation recovery** | When `finish_reason=length` and `content` blank but `reasoning_content` non-empty, append the partial reasoning + cue asking the model to wrap up. | `runner.py` length-handling branch |
| **Idle-timeout circuit breaker** | After `DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS` (default 1, trips on 2nd) consecutive `error_kind=timeout` iterations, run terminates with `stop_reason=circuit_breaker_idle_timeout`. Forward progress resets. | `runner.py` top of iteration loop |
| **Per-block tool-result validation** | Text > 100 KB truncated in place; `image_url` data-URL > 5 MB → text placeholder; `input_audio` > 10 MB likewise. HTTP/HTTPS image refs pass through. | `utils/tool_result_validation.py` |
| **Re-sanitize after `context_transform`** | Runs `drop_orphan_tool_results` + `backfill_missing_tool_results` once more after the optional transform, so a dropped message mid-pair doesn't ship an invalid `tool_use`/`tool_result` mismatch. | `runner.py::_build_request_kwargs` |
| **Compaction grace window** | When `DURIN_LLM_TIMEOUT_S` is about to fire and `is_compacting` callback returns True, deadline extends once by `DURIN_COMPACTION_GRACE_S` (default 30s). Emits `compaction.grace_extended`. One-shot per request. | `runner.py::_await_with_compaction_grace` |
| **Per-model `parallel_tool_calls` gating** | `agents.defaults.parallel_tool_calls` is a substring-keyed dict mapping model name → bool. Provider injects the flag only on match AND when `tools` is non-null. Emits `provider.parallel_tool_calls_injected` once per unique triple per process. | `OpenAICompatProvider._resolve_parallel_tool_calls` |
| **Per-turn aggregate tool-result budget** | Sums tool result sizes; over `DURIN_TURN_BUDGET_CHARS` (default 200 KB) spills the largest not-yet-persisted results to disk, largest first, until aggregate fits. `=0` disables. Emits `turn_budget.enforced`. | `runner.py::_enforce_turn_budget` |
| **Heartbeat session mode** | Default: one long-running `heartbeat` session (trimmed by `keep_recent_messages`). `heartbeat.isolatedSessions=true` gives each tick a fresh `heartbeat-<12hex>` session deleted after the run. | `heartbeat/service.py` |
| **Pre-emptive compaction trigger** | Fires when `estimated_tokens > preemptive_compact_ratio * context_window` (default 0.5; 1M-window models override to ~0.15). Emits `compaction.preemptive_trigger`. | `agent/memory.py::Consolidator` |
| **Mid-turn precheck signal** | After sanitize pipeline each iteration, estimates token cost; if over input budget, aborts with `stop_reason=mid_turn_precheck_overflow` BEFORE the LLM call. Emits `mid_turn_precheck.overflow`. | `runner.py::_mid_turn_precheck` |
| **Compaction lock aggregate timeout** | Per-session compaction lock bounded by `DURIN_COMPACTION_LOCK_TIMEOUT_S` (default 180s). `=0` disables. Emits `compaction.lock_timeout`. | `agent/memory.py::Consolidator._lock_timeout_s` |
| **Tool-call argument repair** | `html.unescape` (only on entity markers), strips ≤96 leading garbage chars + ≤3 trailing, then `json_repair.loads`. Bounded by 64 KB buffer. Emits `tool_call.argument_repair`. | `utils/tool_argument_repair.py` |
| **Unknown-tool loop guard** | Counts calls per unknown tool name per turn; over `DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS` (default 2) terminates with `stop_reason=unknown_tool_loop_guard`. Surfaces real tool names in the error. Emits `unknown_tool.loop_guard`. | `runner.py` top of `should_execute_tools` |
| **History image/audio prune** | Keeps most recent `DURIN_HISTORY_IMAGE_PRESERVE_TURNS` (default 3) intact; older user/tool messages get media blocks replaced with `[image data removed - already processed by model]` (or audio equivalent). Idempotent. Emits `history_media.pruned`. | `utils/history_image_prune.py` |
| **3-tier system prompt** | Stable (identity → bootstrap → active-skills → catalog) + Context (active mode suffix) + Volatile (memory → recent history → archived summary), joined with `\n\n---\n\n`. Stable byte-identical across turns for cache hits. | `agent/context.py::ContextBuilder` |
| **Post-compaction loop guard** | Armed for `DURIN_POST_COMPACTION_GUARD_WINDOW` (default 3) tool calls after a compaction round. Same `(name, args_hash, result_hash)` triple repeating `window_size` times aborts with `stop_reason=post_compaction_loop`. Emits `post_compaction_loop.tripped`. | `utils/post_compaction_guard.py` |

---

## 2. Hooks system

`durin/agent/hook.py` defines:

```python
class AgentHook:
    async def before_iteration(self, context: AgentHookContext) -> None
    async def before_execute_tools(self, context: AgentHookContext) -> None
    async def after_iteration(self, context: AgentHookContext) -> None
    async def on_stream(...) / on_stream_end(...) / emit_reasoning(...)
    def finalize_content(self, context, content) -> str | None
```

`AgentHookContext` exposes `iteration`, `messages`, `response`, `usage`, `tool_calls`, `tool_results`, `tool_events`, `streamed_content`, `final_content`, `stop_reason`, `error`. Mutating `messages` is the supported way to inject system messages mid-turn (used by `Consolidator`).

`CompositeHook` fans out to a list of hooks with per-hook exception isolation, so a faulty third-party hook can't crash the loop.

No hooks are wired in by default after the prune.

---

## 3. Permission-as-data agent modes

Plan / Build / Explore modes selectable per session. The active mode filters the tool surface at the LLM boundary.

**Core** (`durin/agent/agent_mode.py`). `AgentMode` is a frozen dataclass with `allowed: frozenset[str] | None`, `denied: frozenset[str]`, and optional `prompt_suffix`. Three built-ins:

- `build` — default, no restriction
- `plan` — read-only + `exit_plan_mode` only; investigates and surfaces a plan for user approval
- `explore` — read-only for sub-agents (no exit affordance)

Session state lives in `session.metadata`:

- `agent_mode` — currently active mode name
- `pre_plan_mode` — set when entering plan mode, restored on exit

**Tool filtering in the runner** (`runner.py::_active_tool_definitions`). The runner accepts an optional `mode_provider` callable in `AgentRunSpec`; when present, it's called per iteration and the resulting mode filters the tool definitions sent to the LLM. When the model emits a cached tool name no longer allowed, `_run_tool` short-circuits with a clear denial.

**LLM-facing tools** (`durin/agent/tools/plan_mode.py`):

- `enter_plan_mode(reason?)` — switches into plan mode
- `exit_plan_mode(plan)` — writes the plan to `<workspace>/.durin/plans/plan_<timestamp>.md` and yields to the user for approval. Does NOT actually exit plan mode — the user runs `/build`.

**File-based plan storage**. Plans live in `<workspace>/.durin/plans/<session-slug>/plan_<timestamp>.md`. One subdirectory per session, one file per `exit_plan_mode` call. The user can edit the plan file directly with any editor; `/build` picks up the file content as-edited.

**Plan flow with compaction survival**:

| Phase | What happens |
|---|---|
| `/plan` activates | Mode = plan. Prior `executing_plan_path` cleared. |
| `exit_plan_mode(plan)` | Writes plan + sets `session.metadata.active_plan_path`. |
| `/build` approves | `active_plan_path` → `approved_plan_path` (one-shot reminder) AND `executing_plan_path` (persistent for autocompact). Mode restored. |
| Next turn after /build | `ContextBuilder.build_messages` injects one-shot reminder; `approved_plan_path` popped. |
| Autocompact archives | `autocompact._read_plan_carryover` reads `executing_plan_path` and splices plan content into the summary block (cap 6000 chars). |

**Telemetry**: `agent_mode.turn_start`, `agent_mode.switch` (`{from, to, trigger}`), `agent_mode.tool_denied` (`{tool, mode}`), `plan_mode.presented` (`{plan_chars, from_mode}`).

**Slash commands** (`durin/command/builtin.py`): `/plan`, `/build`, `/mode [name]`. All universal across channels via the shared `CommandRouter`.

---

## 4. Long tasks and goal state

`durin/agent/tools/long_task.py` defines `LongTaskTool` (register an objective) and `CompleteGoalTool` (close with a recap). Goal state stored in `session.metadata[GOAL_STATE_KEY]` and mirrored into the runtime-context block each turn via `durin/session/goal_state.py`.

After the prune, `complete_goal` no longer consults any plan-tier verification gate. Only requires an active goal.

---

## 5. Sessions and persistence

```mermaid
flowchart LR
    msg["InboundMessage"] --> jsonl["sessions/KEY.jsonl<br/>append-only"]
    jsonl --> meta["sessions/KEY.meta.json<br/>derived"]
    meta --> evs["events list<br/>plan, tool_call, ..."]
    meta --> drv["derived block<br/>_last_summary, ..."]
    jsonl --> md["sessions/KEY.md<br/>#turn-N anchors"]
```

`durin/session/manager.py` handles session lifecycle. Two files per session:

| File | Content | Purpose |
|---|---|---|
| `<key>.jsonl` | Message history + identity metadata (mode, plan path, todos, channel, title) on line 0 | **Source of truth.** Replayable; messages append-only, never trimmed. |
| `<key>.meta.json` | Lifecycle event timeline + a `derived` block (LLM projections) | **Derived state.** Regenerable from `.jsonl` + `memory/history.jsonl`. Safe to delete and rebuild. |

**Split rule**: if losing the file means you can't reconstruct it from the other, it's source-of-truth (`.jsonl`). Otherwise derived (`.meta.json`).

`Consolidator.maybe_consolidate_by_tokens` (in `durin/agent/memory.py`) advances a cursor (`last_consolidated`) when the prompt exceeds budget — generates a narrative summary, persists to `history.jsonl`, writes to `.meta.json::derived._last_summary`, advances the cursor. **The raw `session.messages` list is never modified in-place** — only the cursor advances. The LLM sees `messages[last_consolidated:]` (capped by `max_messages`) + the summary.

`SessionManager._DERIVED_METADATA_KEYS` is the canonical set of `session.metadata` keys that route to the sidecar's `derived` block instead of line-0.

**In-memory per-turn shaping** (does not touch disk):

- `_microcompact` replaces older tool-result content with `[<tool> result omitted from context]` placeholders on the copy sent to the LLM.
- `_snip_history` further trims the copy from the start when it still doesn't fit the context window.

### Session meta sidecar shape

```json
{
  "session_key": "websocket:chat42",
  "events": [
    {
      "type": "plan",
      "id": "plan_20260519_143022_123",
      "title": "Refactor authentication module",
      "plan_path": ".durin/plans/websocket_chat42/plan_20260519_143022_123.md",
      "created_at": "2026-05-19T14:30:22.123",
      "approved_at": "2026-05-19T14:35:12.456",
      "msg_index": { "approved": 240, "closed": null },
      "outcome": "executing"
    }
  ],
  "derived": {
    "_last_summary": { "text": "Compaction summary…", "last_active": "2026-05-19T14:35:12.456" }
  }
}
```

Two top-level blocks:

- `events` — lifecycle index. `type` is the discriminator for extensibility (today: `plan`, `tool_call`).
- `derived` — LLM-produced projections; whatever keys are in `_DERIVED_METADATA_KEYS` get persisted here instead of line-0. On load, `_merge_derived_from_sidecar` merges them back into the in-memory metadata dict.

**Plan lifecycle**:

- `exit_plan_mode` tool appends a fresh plan event with `outcome=pending` and extracted title.
- `/build` transitions to `outcome=executing`, recording `approved_at` and `msg_index.approved`.
- `/plan` slash command closes any prior executing plan with `outcome=superseded`.

**Atomic writes**: read → modify → write `.tmp` → `os.replace`. No partial states on disk.

**What does NOT go here**: per-turn telemetry (lives in `~/.cache/durin/telemetry/`), plan contents (own `.md` files), anything already in `session.jsonl`.

---

## 6. Providers

`durin/providers/` ships adapters for Anthropic, OpenAI-compat (Z.ai, OpenRouter, Azure, Ollama, LM Studio, Gemini, and 25+ others), Bedrock, GitHub Copilot, local llama-cpp, OpenAI Codex, and a fallback wrapper. `factory.make_provider(config)` resolves the active provider/model from config + presets.

### Capability metadata

`get_model_capabilities(model, provider, overrides)` resolves a `ModelCapabilities` dataclass via a four-layer fallback:

1. **Explicit override** from `config.model_capabilities` — always wins. Use for private/custom models the snapshot doesn't know.
2. **Vendored consensus snapshot** at `providers/data/model_capabilities.json` (schema v2). Built by `scripts/refresh_model_capabilities.py` in two phases:
   - **Phase 1 community merge** (LiteLLM + OpenRouter + models.dev), filtered by TRUSTED_VENDORS whitelist. Aggregator providers (kilo, vercel, 302ai, etc.) filtered out. Booleans OR-merge; numerics MAX.
   - **Phase 2 vendor-API overlay** (opt-in). When `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY` are present, `scripts/_vendor_sources.py` hits the vendor's `/models` and OVERWRITES community values field-by-field. Vendor data is sparse: only fields the vendor explicitly asserts are applied. Each record carries `_authority` ∈ `{vendor, merge}` and `_vendor_sources` list.
3. **Heuristic by model prefix** — last resort for custom/local models (`claude-*` → vision, `glm-*` → text-only, etc.).
4. **Pessimistic default** — all False; safe under-promise.

The dataclass carries `source` naming the layer that produced it. Consumers needing authoritative data gate on `source in {"override", "snapshot"}`.

### Capability bridges (aux models)

When the primary model lacks a modality but the user has declared an `aux_model`, durin exposes a delegating tool:

- `aux_models.vision` → `interpret_image(image_path, question)` — base64-encodes PNG/JPEG/GIF/WEBP, ships as `image_url` block.
- `aux_models.audio` → `interpret_audio(audio_path, question)` — ships WAV/MP3/M4A/OGG/FLAC/WebM as `input_audio` block (chat-multimodal aux only).

Aux providers built once at startup by `loop._build_aux_providers(config)` and handed through `ToolContext.aux_providers`. Tools gate via `enabled(ctx)` classmethod — without an aux configured, the tool never appears in the model's tool list.

### Prompt caching

`_apply_cache_control` stamps Anthropic-style `cache_control: {type: ephemeral}` on system + last user content + last tool definition for providers with `supports_prompt_caching=True` (Anthropic, OpenRouter). Others using automatic prefix caching (Zhipu/MiniMax/DeepSeek/Qwen/Mistral/xAI/StepFun/Moonshot) need no markers — they cache transparently as long as the prefix is stable. `cached_tokens` normalized across all providers (`prompt_tokens_details.cached_tokens`, `cached_tokens`, `prompt_cache_hit_tokens`, `cache_read_input_tokens` all map to the same key). `AgentProgressHook.after_iteration` emits `cache.usage` per turn.

### Token accounting

`build_assistant_message(..., prompt_tokens=...)` stamps provider-reported `prompt_tokens` onto persisted assistant messages as `usage_prompt_tokens`. `latest_prompt_tokens_anchor(messages)` walks backward to find the most recent stamp; `estimate_prompt_tokens_chain` uses that as an authoritative baseline and tiktoken-estimates only the tail.

---

## 7. Sandboxing

Tool execution sandboxed via `durin/agent/tools/sandbox.py`. Three backends:

- `bwrap` — Linux namespace sandbox (production)
- `docker` — Docker container (benchmark-style isolation)
- `testbed` — conda-env wrapper for running inside benchmark containers

The exec tool routes through `wrap_command(sandbox, command, workspace, cwd)`.
