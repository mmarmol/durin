# 07 — External Agents Review: Loop & Tool Patterns

> May 2026 deep review of four open-source coding agents (OpenHands, Hermes Agent, OpenCode, OpenClaude) for patterns we could adopt in Durin. Companion to `04_agent_strategies_catalog.md`; that doc surveys the landscape — this one is **code-level** analysis of repos cloned to `~/git_personal/` plus an explicit weighing of each adoption candidate.

---

## Methodology

- **Repos cloned shallow** to `~/git_personal/{openhands, hermes-agent, opencode, openclaude}/` (May 2026).
- **4 agents analyzed in parallel** by Claude Code subagents, one per repo. Each produced ~400-500 words on (1) loop architecture, (2) tool inventory, (3) most distinctive pattern.
- For each pattern surfaced, an explicit weighing follows in §5 and §6: effort, value, risk, decision (ADOPT / DEFER / REJECT) with rationale.
- **Scope filter**: language-agnostic patterns preferred; per-language additions get a discount because Durin targets multiple languages.

The raw subagent reports are summarized below — full file references included so future-you can dive into the source.

---

## 1. OpenHands (All-Hands-AI/OpenHands)

**Language**: Python • **Repo size**: 26M (shallow) • **License**: MIT

### Loop

Driver in `software-agent-sdk/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py:747` (`LocalConversation.run`). Per-step logic in `agent/agent.py:475` (`Agent.step`).

Key mechanisms:
- **Condenser as first-class event** (`context/condenser/llm_summarizing_condenser.py:37`). When fired, emits a `CondensationRequest` event that rewrites the View; next step regenerates the LLM messages from events. Same mechanism handles `LLMContextWindowExceedError` and `LLMMalformedConversationHistoryError` — context recovery is unified.
- **StuckDetector** with 5 heuristics: repeating action+observation, repeating action+errors, monologue, alternating pattern, context-window-error loop.
- **Hook system** (UserPromptSubmit, PreToolUse, PostToolUse, Stop, SessionStart/End) can block or deny stop (inject feedback, resume).
- **Critic + iterative refinement** (`agent/critic_mixin.py:76`): post-`FinishAction`, a separate Critic LLM scores; if below threshold, injects a `MessageEvent(source="user")` followup and resumes.
- **ParallelToolExecutor**: multiple tool_calls in one turn → threaded execution with resource locks declared per tool.
- **Confirmation mode** with `security_analyzer` + `confirmation_policy.should_confirm(risk)`.

No explicit plan/execute phases. Phases are emergent from which tool the LLM picks.

### Tools

Lives in `openhands-tools/openhands/tools/` and `openhands-sdk/openhands/sdk/tool/builtins/`. Defined via subclass of `ToolDefinition[Action, Observation]` with Pydantic models; OpenAI schema derived automatically. MCP supported in parallel via `MCPToolDefinition`.

Notable tools:
- `TerminalTool` — tmux-based persistent shell
- `FileEditorTool` — view/create/str_replace/insert (Anthropic-style)
- `ApplyPatchTool` — patch envelope alternative
- `GrepTool`, `GlobTool` — ripgrep + glob
- `BrowserToolSet` — browser-use sub-tools (Navigate, Click, Type, Scroll, …)
- `PlanningFileEditorTool` — FileEditor restricted to markdown plan files
- `TaskTrackerTool` — TODO management
- `TaskTool`/`DelegateTool` — sub-agent spawning with own workspace
- `TomConsultTool` / `SleeptimeComputeTool` — offline memory sub-agent
- **`SwitchLLMTool`** — model swap mid-run
- **`InvokeSkillTool`** — invoke Anthropic-style skill packs by keyword/task trigger
- `FinishTool`, `ThinkTool`

### Most distinctive

1. **Condenser-as-event**: compression is auditable, persistible, idempotent. Recovery from context overflow reuses the same channel — no special-case code path.
2. **Critic + iterative refinement above the ReAct loop**: post-finish, a separate Critic LLM either accepts or injects followup and resumes. Score persisted in `state.agent_state[ITERATIVE_REFINEMENT_ITERATION_KEY]`.

---

## 2. Hermes Agent (NousResearch/hermes-agent)

**Language**: Python • **Repo size**: 11M (most are release notes; code in `agent/`, `tools/`, `skills/`) • **License**: MIT

### Loop

Main loop in `agent/conversation_loop.py:187` (`run_conversation`). The function is ~4099 lines — Hermes is a monolith.

```
build/restore cached system_prompt (stable + context + volatile tiers)
init IterationBudget(agent.max_iterations); api_call_count = 0
while api_call_count < max_iterations and iteration_budget.remaining > 0:
    api_call_count += 1; budget.consume()
    api_kwargs = agent._build_api_kwargs(messages)  # cache_control breakpoints injected
    response = _interruptible_streaming_api_call(api_kwargs)  # 90s stale / 60s read timeouts
    handle retries (auth, 429, rate-guard, length-continuation, compression)
    if assistant_message.tool_calls:
        sanitize/dedupe/cap, then agent._execute_tool_calls(...)
        continue
    else:
        append final_msg; break
persist_session(); save_trajectory()
if final_response and (_should_review_memory or _should_review_skills):
    agent._spawn_background_review(messages_snapshot, ...)
```

Distinctive mechanisms:
- **Background review fork** (`agent/background_review.py:321 _run_review_in_thread`). After the user response is delivered, a daemon thread forks a new `AIAgent` with **the same runtime (model, credentials, cached system prompt)** but a tool whitelist of `memory + skill_manage` only. Replays the conversation. Can call `skill_manage(create|patch|edit|delete)`. Provenance via `ContextVar` `set_current_write_origin` — only this fork can `mark_agent_created`.
- **Curator fork** (`agent/curator.py`). Inactivity-triggered (~7 days). Another forked `AIAgent` to consolidate / archive / pin agent-created skills, using lifecycle timestamps in `~/.hermes/skills/.curator_state`. Only touches skills with `agent_created=True`.
- **Three-tier system prompt** (`agent/system_prompt.py:10-19`):
  - **Stable**: SOUL.md identity + skills index + tool guidance
  - **Context**: AGENTS.md / .cursorrules
  - **Volatile**: MEMORY.md + USER.md + external provider
  - Computed once per session, cached on `agent._cached_system_prompt`. Mid-session memory/skill writes go to disk but **don't re-render the prompt** — prefix cache stays valid until next session.
- **Skill retrieval at session start**: `prompt_builder.py:992` injects a compact index (name + 1-line description per skill, LRU + disk snapshot). Full `SKILL.md` is loaded on-demand via the `skill_view` tool — progressive disclosure.
- **External memory providers**: Honcho / mem0 / supermemory plug into `MemoryManager` — exactly one external + the builtin.

### Tools

Lives in `tools/`. Discovery via AST scan of top-level `registry.register(...)` calls (`tools/registry.py:57`). Schemas are plain OpenAI function-calling JSON.

60+ builtin tools, including:
- File ops: `read_file`, `write_file`, `patch`, `search_files`
- Shell: `terminal`, `process`, `execute_code` (sandboxed)
- Web: `web_search`, `web_extract`, `x_search`
- Browser (Playwright/CDP): `browser_navigate/click/type/scroll/snapshot/console/back/press/vision/cdp/get_images`
- Computer: `computer_use` (screenshot + mouse control)
- Multimodal: `vision_analyze`, `video_analyze`, `image_generate`, `video_generate`, `text_to_speech`
- Memory: `memory`
- Skills: `skills_list`, `skill_view`, `skill_manage`
- Agents: `delegate_task`, **`mixture_of_agents`** (fan-out to multiple models, aggregate)
- Workflow: `todo`, `kanban_*`, `clarify`, `send_message`, `session_search`, `cronjob`
- Integrations: `discord`, `feishu_doc/drive`, Yuanbao, HomeAssistant

**Tools vs skills — mechanical distinction**:
- **Tools** are Python handlers invoked with structured args
- **Skills** are markdown documents (`SKILL.md` + optional `references/`, `templates/`, `scripts/`) under `~/.hermes/skills/`
- Skill index is in the system prompt; bodies loaded by calling the `skill_view` **tool**
- Skills are agent-written via `skill_manage` — **exclusively from the bg-review fork**

### Most distinctive

1. **Two-clock self-improvement, isolated by ContextVar provenance**. The bg-review fork (post-turn) and the curator fork (inactivity-triggered ~7 days) both inherit the parent's runtime/credentials/prompt-cache for zero extra latency, but operate with a memory+skill-only tool whitelist. Provenance tracking ensures the curator only touches agent-created skills.
2. **Frozen-snapshot system prompt for prefix-cache stability**. Three tiers joined once and cached; mid-session writes hit disk but never re-render the prompt — everything (including skill creates from bg fork) takes effect only on next session.

**Re: DSPy/GEPA** — not integrated in the loop. Only references are docs-only skills. Hermes' self-evolution is purely the LLM-driven skill-loop above; no optimizer/gradient/dataset machinery.

---

## 3. OpenCode (sst/opencode)

**Language**: TypeScript (Bun runtime, Effect-TS); Go for TUI only • **Repo size**: 32M • **License**: MIT

### Loop

The Go code is just the TUI client. The agent server runs in Bun. Main loop at `packages/opencode/src/session/prompt.ts:1240` (`runLoop`); per-turn processing at `session/processor.ts:779`; LLM stream wraps Vercel `ai-sdk`'s `streamText` at `session/llm.ts:85`.

```
runLoop(sessionID):
  step = 0
  while true:
    msgs = filterCompacted(sessionID)
    if last assistant has finish-reason != "tool-calls" and no pending tool parts: break
    step++
    if step == 1: fire-and-forget title-generation
    if pending task is subtask: handleSubtask; continue
    if pending task is compaction OR isOverflow(tokens): run compaction.process; continue
    tools = resolveTools(agent, model, permission)        // permission-filtered
    msgs = SessionReminders.apply(msgs, agent)            // inject system-reminders
    handle = processor.create(assistantMessage)
    result = handle.process({ user, system, messages, tools, model })
      // opens LLM stream, dispatches StreamEvents to handleEvent,
      // executes tool calls inline via Effect, dual-writes message parts,
      // returns "stop" | "compact" | "continue"
    if result == "stop": break
    if result == "compact": compaction.create({ auto: true }); continue
```

Distinctive mechanisms:
- **Modes as data, not code** (`agent.ts:127-164`). `build` is default (full permissions). `plan` denies `edit/*` except writes into `.opencode/plans/*.md` + exposes `plan_exit` tool. `explore`, `general`, `scout` are subagents with read-only ruleset. `compaction`, `title`, `summary` are hidden primaries with `*: deny`. Each agent is a config object with `mode`, `permission`, optional `prompt`, `steps` cap, model variant.
- **Compaction as a dedicated agent**, not an inline summarizer. Runs the `compaction` agent over `selected.head` with a fixed Markdown template (Goal / Constraints / Progress / Decisions / Next Steps / Critical Context / Relevant Files). Preserves tail of 2-8k tokens. Replays original user message. Triggered automatically when `isOverflow(tokens)` (against `model.limit.input - 20k buffer`) or manually via `compaction.create`.
- **doom_loop permission** (`processor.ts:441`) — interrupts runaway tool-call repetition (similar in spirit to our 1A).
- **Tool output truncation**: every tool output capped at `TOOL_OUTPUT_MAX_CHARS = 2000`. Beyond cap, written to temp file with a reference in the message.

### Tools

Lives in `packages/opencode/src/tool/`. Builtin list hardwired in `registry.ts:250-269`. Definitions use Effect Schema + `Tool.define(id, Effect)`.

- `invalid` — fallback tool when model emits non-existent tool call
- `shell` — bash with subdir for prompts
- **`read`** — windowed file viewer with `offset/limit` (default 2000 lines), `MAX_LINE_LENGTH` truncation, byte cap, **"did you mean" suggestion on miss**, LSP warm-up
- `glob`, `grep` — ripgrep with directory filter
- **`edit`** — string-replace with **6 cascading Replacer strategies**: Simple, LineTrimmed, **BlockAnchor**, WhitespaceNormalized, **IndentationFlexible**, EscapeNormalized
- `write`, `task` (subagent), `task_status`
- `fetch` (webfetch), `search` (websearch, provider-gated)
- `todo` — TodoWrite list
- **`repo_clone`, `repo_overview`** — clone external repo to managed cache; emit depth-bounded structure tree + detected ecosystem/entrypoints. NO embeddings, NO PageRank, just structural walk capped by `STRUCTURE_LIMIT`. Scout-only.
- `skill` — load Skill (Markdown + scripts) into context on demand
- `patch` — Codex apply_patch envelope, conditionally swapped in for GPT models in place of `edit`/`write`
- `lsp` — LSP operations (workspaceSymbol) exposed as tool (experimental)
- `plan`, `question`, `external-directory` — workflow/permission tools

### Most distinctive

1. **Effect-TS service graph + client/server separation**. Every subsystem is a `Context.Service` wired through `Layer`s. The same engine drives TUI, Slack, Desktop, Web, and SDK without re-implementing the loop. Concurrency, cancellation, retries, tracing all come from Effect.
2. **Per-tool ruleset with wildcard permission engine and declarative agents**. Agents are data; tools are filtered each turn by `Permission.evaluate(toolName, pattern, ruleset)`. Plan vs build is a different ruleset, not a separate code path.

---

## 4. OpenClaude (Gitlawb/openclaude)

**Language**: TypeScript (Bun) + Python sidecar • **Repo size**: 4.1M • **License**: MIT • Fork of Claude Code

### Loop

`src/query.ts:250` (`queryLoop`) — `while(true)` over a mutable `state` object. Public entry `query.ts:228`. SDK + gRPC wrapper at `src/QueryEngine.ts:207`. Tool execution via `runTools` (`src/services/tools/toolOrchestration.ts`) or `StreamingToolExecutor`.

```
1.  prefetch memory + skill discovery (non-blocking)
2.  apply tool-result budget → snip → microcompact → context-collapse → autocompact
3.  block if absolute-token blocking limit hit (or 3+ compact failures)
4.  await deps.callModel({...})
    for each streamed message: yield, push, collect tool_use blocks
5.  catch FallbackTriggeredError → swap to fallbackModel, retry
6.  if !needsFollowUp: try collapse-drain → reactive-compact → max-output-tokens recovery → continuation-nudge → return {completed}
7.  runTools(toolUseBlocks)
8.  check abort, hook_stopped, tool-failure-loop-guard (path/signature/category counters)
9.  generateToolUseSummary (async, Haiku) for mobile UI
10. if turnCount+1 > maxTurns → return {max_turns}
11. state = { messages: [prev, assistant, results], turnCount+1 }; continue
```

**This IS Claude Code preserved verbatim** (same `tengu_*` analytics events, same compact/microcompact/snip pipeline, same `feature()` gates). The fork's modifications are surgical: `providerOverride` field + OpenAI-compatible shim (`src/services/api/openaiShim.ts`, 2586 LOC) translating Anthropic SDK to chat-completions/responses.

### Tools

Lives in `src/tools/`, one directory per tool. Aggregation in `src/tools.ts:182 getAllBaseTools`. Definition via `buildTool({...})`.

40+ tools. Notable ones beyond ReAct baseline:
- `AgentTool` (spawn subagent), `BashTool`, `FileRead/Edit/Write`, `Glob`, `Grep`
- `LSPTool` (LSP refs/defs)
- `WebFetch`, `WebSearch`, `TodoWriteTool`
- `EnterPlanModeTool` / `ExitPlanModeV2Tool`
- `SkillTool` — load+invoke `.claude/skills/*`
- **`ToolSearchTool`** — lazy-load deferred tools by keyword (`shouldDefer` flag)
- `TaskCreate/Get/Update/List/Stop/Output` — background tasks
- **`EnterWorktreeTool` / `ExitWorktreeTool`** — git worktrees for parallel work
- `SendMessageTool`, `ListPeersTool` — UDS inbox / inter-agent IPC
- `BriefTool` — compact sub-task brief
- `MonitorTool`, `SleepTool`, `RemoteTriggerTool`, `ScheduleCronTool`, `PushNotificationTool`, `SubscribePRTool`, `SuggestBackgroundPRTool`
- `SnipTool`, `CtxInspectTool`, `VerifyPlanExecutionTool` — context surgery

### Most distinctive (what the fork added vs Claude Code)

1. **OpenAI-compatible provider shim** (2586 LOC) — env-driven routing to OpenAI, Azure, Ollama, LM Studio, OpenRouter, Together, Groq, Fireworks, DeepSeek, Mistral, Gemini, GitHub Copilot, Codex.
2. **Smart routing two layers**: TS heuristic at `smartModelRouting.ts:120` (cheap-vs-strong by char/word/code-fence/keyword); Python health-based router at `python/smart_router.py:144` (pings, latency EMA, error-rate, auto-mark unhealthy).
3. **gRPC service mode** — multi-tenant agent server.

Everything else (subagents, plan mode, hooks, MCP, ToolSearch, microcompact/snip, knowledge graph, conversation arc, coordinator mode, worktrees, cron, the entire `feature()` flag system) is upstream Claude Code preserved.

---

## 5. Cross-cutting consensus patterns

What all 4 (or near-all) agents do — strong signal these are not optional in 2026:

| Pattern | OpenHands | Hermes | OpenCode | OpenClaude | Durin status |
|---|---|---|---|---|---|
| **History compaction near limit** | ✅ Condenser event | ✅ External provider | ✅ Compaction agent | ✅ microcompact/snip | ✅ microcompact + snip |
| **Stuck/loop detection** | ✅ StuckDetector (5) | ❌ | ✅ doom_loop | ✅ tool-failure-loop-guard | ✅ 1A hash-based |
| **Subagents / delegation** | ✅ Delegate/Task | ✅ delegate_task | ✅ task + subagents | ✅ AgentTool | ✅ subagent.py |
| **Skills as markdown bundles on-demand** | ✅ InvokeSkill | ✅ skill_view | ✅ skill | ✅ SkillTool | ⚠️ skills dir, no progressive disclosure |
| **Tool output truncation** | ✅ | ✅ | ✅ 2000-char + temp file | ✅ snip | ✅ MAX_TOOL_RESULT_CHARS |
| **MCP support** | ✅ | ✅ | ✅ | ✅ | ✅ |

The two that **all 4 have and we partially lack**: skill progressive disclosure (we have skills dir but no index-then-load pattern), and a separate explicit compaction-agent vs an inline summarizer.

---

## 6. Loop adoption candidates — weighed

For each: **Effort** (LOW=hours, MED=days, HIGH=weeks+), **Value** (LOW/MED/HIGH based on evidence), **Risk** (LOW/MED/HIGH — refuted-territory check), **Decision** (ADOPT NOW / DEFER / REJECT).

### L1 — Hermes' bg-review fork pattern for memory

**Mechanism**: after each completed turn, spawn a daemon thread that forks a new agent inheriting parent runtime (model, creds, cached prompt). Tool whitelist of `memory + skill_manage` only. Replays conversation. Writes to disk via ContextVar-gated provenance flag. Effect is only visible next session.

**In Durin**:
- New module `durin/agent/background_review.py` (~300-400 lines)
- New ContextVar `_write_origin` paralleling `_current_file_states`
- Hook into `AgentLoop` after final response delivery
- Reuse existing tool whitelist mechanism (we have `_scopes`)
- New skills/memory write tools gated by ContextVar

**Effort**: HIGH (2-3 weeks including tests) • **Value**: HIGH (direct match to Doc 03 design) • **Risk**: MED (concurrency bugs, provenance leaks)

**Decision**: **DEFER to Phase 2**. This is essentially the implementation of memory we already plan. Adopt the Hermes pattern as the *reference architecture* but only build when Phase 2 starts.

---

### L2 — Hermes' frozen 3-tier system prompt

**Mechanism**: build system prompt once per session from {stable, context, volatile} tiers; cache on `agent._cached_system_prompt`. Mid-session writes to memory/skills go to disk but DON'T re-render. Effect propagates next session. Result: prefix cache never invalidates during session = significant token savings.

**In Durin**:
- Touch `durin/agent/context.py` (ContextBuilder)
- Split system-prompt construction into the 3 tiers
- Add cache key on session_id
- Memory/skill write paths must skip prompt re-render

**Effort**: MED (1 week) • **Value**: HIGH (token efficiency, latency) • **Risk**: LOW

**Decision**: **ADOPT (with Phase 2)**. Standalone version is also valuable — even without memory writes, we benefit from a stable cached prompt.

---

### L3 — OpenCode's permission-as-data agent modes

**Mechanism**: plan vs build vs explore is **data** (a ruleset like `{edit: deny, plan_exit: allow}`), not a separate code path. Tools filter per-turn via `Permission.evaluate(toolName, ruleset)`.

**In Durin**:
- New `durin/agent/agent_mode.py` with a Mode dataclass
- Existing tool registry gains permission filter pass before each turn
- Plan-mode = mode with `edit: deny`, `write: deny`, `plan_exit: allow`
- This obsoletes the V7/V8 plan-tier approach we refuted (no special code, just data)

**Effort**: MED (1 week) • **Value**: MED (cleaner than refuted plan-tier; enables future modes cheaply) • **Risk**: LOW

**Decision**: **ADOPT** when V9e wraps. Avoids repeating the V7/V8 mistake while keeping the *option* of plan-mode as a config.

---

### L4 — OpenCode's compaction-as-separate-agent

**Mechanism**: when overflow detected, run a dedicated `compaction` agent over the history head, with a fixed Markdown template (Goal / Constraints / Progress / Decisions / Next Steps / Critical Context / Relevant Files). Preserve tail of 2-8k tokens. Original user message replayed.

**In Durin**:
- Our `autocompact.py` is closer to OpenHands' inline summarizer
- This would refactor it into a sub-call with fixed structure
- Could reuse subagent infrastructure

**Effort**: MED (1 week) • **Value**: MED (better recovery quality vs current opaque summary) • **Risk**: LOW

**Decision**: **DEFER**. Solid pattern but current autocompact works; revisit if our telemetry shows compaction quality is poor.

---

### L5 — OpenHands' Condenser as first-class event

**Mechanism**: condensation is an *event* added to the conversation stream, not a hidden mutation. Next step regenerates messages from events. Same channel handles `LLMContextWindowExceedError` recovery — unified path.

**In Durin**:
- Current autocompact is opaque (compresses without leaving trace)
- This would require event-sourced conversation state — significant refactor
- Reuses our existing event/hook infrastructure partially

**Effort**: HIGH (3-4 weeks refactor) • **Value**: MED (auditability, debugging) • **Risk**: MED (touches the heart of state mgmt)

**Decision**: **REJECT for now**. Refactor cost too high; current opaque autocompact is acceptable. Reconsider only if compaction debuggability becomes a real bottleneck.

---

### L6 — OpenHands' StuckDetector (5 heuristics)

**Mechanism**: detects 5 different stuck patterns: repeating action+observation, repeating action+errors, monologue (text only, no progress), alternating pattern (A-B-A-B), context-window-error loop.

**In Durin**:
- We have 1A (hash-based same-call-same-args detection in `runner.py`). Limited to identical signatures within a turn.
- StuckDetector would catch more subtle patterns (alternation, monologue, etc.)
- Implementation: new module `durin/agent/stuck_detector.py` with the 5 heuristics

**Effort**: MED (3-5 days) • **Value**: MED (catches patterns 1A misses) • **Risk**: LOW (read-only detection, no state mutation)

**Decision**: **DEFER pending telemetry data**. Let Phase 1c telemetry tell us if 1A catches everything or if we need broader detection. Don't add complexity speculatively.

---

### L7 — OpenHands' Critic + iterative refinement

**Mechanism**: after `FinishAction`, a separate Critic LLM scores the result. If below threshold, inject a user-message followup and resume the loop.

**In Durin**:
- **Refuted territory**. V3/V4/V6 showed same-model self-verification fails (model shares blind spots with itself).
- OpenHands uses a *separate* Critic LLM (potentially different model), which reduces but does not eliminate the risk.
- Even with different model, V8's PlanHook (forced verification gate) was refuted: 0/24 hits, -2pp on scenario_3.

**Effort**: MED (1 week) • **Value**: UNKNOWN — empirically refuted in our setup • **Risk**: **HIGH** (repeats refuted pattern)

**Decision**: **REJECT**. Until we have a benchmark where Critic-without-gate clearly outperforms baseline with a different model, this is not worth the cost.

---

### L8 — OpenCode's doom_loop permission

**Mechanism**: a permission rule that interrupts runaway tool-call repetition. Similar to our 1A but expressed declaratively.

**In Durin**:
- We already have 1A. doom_loop expressed declaratively might be marginally cleaner.

**Effort**: LOW (1 day refactor) • **Value**: LOW (same outcome as 1A, just different shape) • **Risk**: LOW

**Decision**: **REJECT**. 1A is functionally equivalent. Not worth the refactor.

---

### L9 — OpenHands' hook system (Pre/PostToolUse, Stop, UserPromptSubmit)

**Mechanism**: lifecycle hooks that can block actions, deny stop (inject feedback + resume), or veto user prompts.

**In Durin**:
- We have `AgentHook` system but no hooks wired by default after the prune.
- OpenHands' specific hooks (Stop with feedback injection, UserPromptSubmit veto) are not in our interface.

**Effort**: LOW (extend existing AgentHook with 2-3 new lifecycle points) • **Value**: LOW until we have a hook to wire • **Risk**: LOW

**Decision**: **DEFER**. Extend interface only when there's a concrete hook to plug in. Avoid speculative infrastructure.

---

## 7. Tool adoption candidates — weighed

### T1 — OpenCode's `repo_overview` tool

**Mechanism**: depth-bounded structure tree of a codebase + detected ecosystem (build system, language, entrypoints). NO embeddings, NO PageRank, just a structural walk capped by `STRUCTURE_LIMIT`.

**In Durin**:
- New tool in `durin/agent/tools/repo_overview.py`
- Walks workspace tree skipping noise dirs (already in `ListDirTool._IGNORE_DIRS`)
- Detects: pyproject.toml/package.json/Cargo.toml/go.mod/etc. → ecosystem labels
- Returns structure + ecosystem in a single call

**Effort**: LOW (1-2 days) • **Value**: HIGH (model orients before diving; nobody else our level has it) • **Risk**: LOW

**Decision**: **ADOPT NOW**. Single biggest tool gain. Lenguaje-agnostic. Trivial implementation. Direct fit to context-orchestration pivot.

---

### T2 — OpenCode's cascading replacer strategies (block-anchor + indent-flex)

**Mechanism**: `edit` tool tries strategies in order: exact → line-trim → block anchor → whitespace normalize → indentation flexible → escape normalize. Each is a fallback when the previous fails.

**In Durin**:
- Our `EditFileTool._find_matches` already does 4 strategies: exact, trim, trim+quotes, quote-normalize.
- We lack **block anchor** (match first+last line, fuzzy middle) and **indentation flexible** (match content regardless of leading whitespace prefix).
- Implementation: extend `_find_matches` cascade in `durin/agent/tools/filesystem.py`

**Effort**: LOW (2-3 days incl. tests) • **Value**: MED (fewer edit failures) • **Risk**: LOW (purely additive matchers)

**Decision**: **ADOPT NOW**. Cheap, additive, measurable (count edit-failure reductions).

---

### T3 — OpenCode's read suggestion-on-miss

**Mechanism**: when `read_file` is called on a path that doesn't exist, suggest closest matches in same dir ("did you mean: X, Y, Z?").

**In Durin**:
- We already have this in `EditFileTool._file_not_found_msg` using `difflib.get_close_matches`.
- ReadFileTool has plain `Error: File not found` without suggestions.
- Implementation: 5-line addition to ReadFileTool error path.

**Effort**: LOW (1 hour) • **Value**: LOW-MED (saves a turn when LLM guesses path wrong) • **Risk**: LOW

**Decision**: **ADOPT NOW**. Trivial. Lift existing helper from EditFileTool.

---

### T4 — OpenCode's tool output cap + temp file fallback

**Mechanism**: every tool output capped at `TOOL_OUTPUT_MAX_CHARS = 2000`. Overflow written to temp file with a reference in the message ("Full output at /tmp/...").

**In Durin**:
- We have `MAX_TOOL_RESULT_CHARS` in `AgentRunSpec` (governs overall cap).
- We don't write overflow to temp files — overflow just gets truncated.
- The Hermes-like pattern (cap + reference) lets the LLM ask to read more if needed.

**Effort**: LOW (1-2 days) • **Value**: MED (model can recover full output without contaminating context every time) • **Risk**: LOW

**Decision**: **ADOPT** as part of Phase 1c. Pairs well with our new telemetry — we can measure how often the model actually asks for the full output.

---

### T5 — `apply_patch` tool (Codex envelope)

**Mechanism**: alternative to string-replace edit. The model produces a patch in a structured envelope; the tool applies it as a unified diff.

**In Durin**:
- We have `edit_file` (string match + replace). For some models (especially OpenAI), patch grammar is more native.
- OpenCode swaps in `apply_patch` for GPT models in place of `edit`/`write`.
- Implementation: new tool `durin/agent/tools/apply_patch.py` + model-family gating in tool registry.

**Effort**: MED (1 week incl. parser tests) • **Value**: MED (depends on how much we use OpenAI-family models) • **Risk**: LOW

**Decision**: **DEFER**. Only worth it if we widen to OpenAI-family models in benchmark. Our current focus is glm-5.1.

---

### T6 — Skill packs as markdown bundles with progressive disclosure

**Mechanism**: skills are markdown documents stored on disk. The agent sees a 1-line description per skill in the system prompt (cheap). To use one, calls `skill_view` to load the full `SKILL.md`. Bundles can include `scripts/`, `templates/`, `references/`.

**In Durin**:
- We have `durin/skills/` and a basic discovery in `agent/skills.py`.
- We DON'T have the index-then-load pattern. Currently skills are either auto-injected or absent.
- Implementation: refactor `ContextBuilder` to inject only the index; add `skill_view` tool.

**Effort**: MED (1-2 weeks incl. migration) • **Value**: HIGH (token savings on long sessions; aligns with Hermes/OpenHands/OpenCode consensus) • **Risk**: LOW

**Decision**: **ADOPT** after T1-T4 (which are cheaper). Strong industrial consensus pattern; we're behind here.

---

### T7 — OpenHands' `PlanningFileEditorTool`

**Mechanism**: a FileEditor variant restricted to writing/editing markdown plan files. Used when the model wants to materialize a plan to disk before executing.

**In Durin**:
- Could be implemented as a thin wrapper over our existing `write_file` with path filter.
- Implementation: small tool addition + system prompt nudge.

**Effort**: LOW (1 day) • **Value**: LOW-MED (helps long-horizon tasks; impact unclear for short tasks) • **Risk**: LOW

**Decision**: **DEFER**. Solid pattern but our current tasks don't show evidence of needing plan-materialization. Re-evaluate after V9e analysis.

---

### T8 — `ToolSearch` deferred tool loading

**Mechanism**: tools have a `shouldDefer` flag. Deferred tools don't ship their schemas in the prompt — only their names + 1-line description. The model calls `ToolSearch` to load specific schemas when it needs them.

**In Durin**:
- We ship all tool schemas every turn.
- Saves prompt tokens proportional to tool count × schema size.
- Implementation: extend tool registry with `shouldDefer` + new `ToolSearch` tool.

**Effort**: MED (1 week) • **Value**: MED (depends on tool count; high for plugin-heavy installs) • **Risk**: LOW

**Decision**: **DEFER**. Useful when tool count grows. Current tool set is manageable.

---

### T9 — LSP-as-tool

**Mechanism**: expose LSP operations (workspaceSymbol, definition, references) as a callable tool. Language-specific.

**In Durin**:
- Powerful but **per-language maintenance burden** (LSP server per language).
- We previously rejected per-language tools for this reason.

**Effort**: HIGH (LSP wrapper + per-language bindings) • **Value**: HIGH per-language but doesn't compound across languages • **Risk**: MED (server lifecycle, version drift)

**Decision**: **REJECT** for the cross-language case. Reconsider only if we focus Durin on one language.

---

### T10 — `SwitchLLMTool`

**Mechanism**: a tool the agent calls to swap its own LLM mid-run.

**In Durin**:
- Possible in principle (we have multiple providers via factory).
- Empirical value unclear; risk of model drift mid-task is real.

**Effort**: MED (1 week) • **Value**: UNCLEAR • **Risk**: MED

**Decision**: **REJECT**. Speculative. No evidence it helps in our setup.

---

### T11 — `mixture_of_agents` (fan-out + aggregate)

**Mechanism**: dispatch the same task to multiple models, aggregate their outputs.

**In Durin**:
- We have subagent infrastructure, but no aggregation step.
- Cost = N × single-call cost. Hard to justify at our scale.

**Effort**: MED (1 week) • **Value**: LOW for our use case (synchronous interactive) • **Risk**: LOW

**Decision**: **REJECT**. Cost/value misaligned with our setup.

---

## 8. Priority matrix

Adoption sequence by combined score (Value × inverse Effort × inverse Risk):

| Rank | Item | Effort | Value | Risk | Decision |
|---|---|---|---|---|---|
| 1 | **T1 repo_overview tool** | LOW | HIGH | LOW | **ADOPT NOW** |
| 2 | **T3 read suggestion-on-miss** | LOW | LOW-MED | LOW | **ADOPT NOW** (trivial) |
| 3 | **T2 cascading replacer (block-anchor + indent-flex)** | LOW | MED | LOW | **ADOPT NOW** |
| 4 | **T4 tool output cap + temp file** | LOW | MED | LOW | **ADOPT** (Phase 1c) |
| 5 | **L3 permission-as-data agent modes** | MED | MED | LOW | **ADOPT** post-V9e |
| 6 | **T6 skill progressive disclosure** | MED | HIGH | LOW | **ADOPT** after 1-4 |
| 7 | **L2 frozen 3-tier system prompt** | MED | HIGH | LOW | **ADOPT** (Phase 2) |
| 8 | **L1 Hermes bg-review fork pattern** | HIGH | HIGH | MED | **DEFER** (Phase 2 reference arch) |
| 9 | L4 compaction-as-separate-agent | MED | MED | LOW | DEFER |
| 10 | L6 StuckDetector 5 heuristics | MED | MED | LOW | DEFER (post-telemetry) |
| 11 | T7 PlanningFileEditorTool | LOW | LOW-MED | LOW | DEFER (post-V9e) |
| 12 | T8 ToolSearch deferred loading | MED | MED | LOW | DEFER |
| 13 | T5 apply_patch | MED | MED | LOW | DEFER (model-family gated) |
| 14 | L9 hook system extension | LOW | LOW | LOW | DEFER |
| – | L5 Condenser as event | HIGH | MED | MED | REJECT (refactor cost) |
| – | L7 Critic + iterative refinement | MED | UNCLEAR | **HIGH** | **REJECT** (refuted pattern) |
| – | L8 doom_loop permission | LOW | LOW | LOW | REJECT (1A equivalent) |
| – | T9 LSP-as-tool | HIGH | HIGH per-lang | MED | REJECT (per-language burden) |
| – | T10 SwitchLLMTool | MED | UNCLEAR | MED | REJECT |
| – | T11 mixture_of_agents | MED | LOW | LOW | REJECT |

---

## 9. Recommended sequencing (post V9e analysis)

**Sprint A — Quick wins (1-2 weeks)**:
1. T1 repo_overview (1-2 days)
2. T3 read suggestion-on-miss (1 hour)
3. T2 cascading replacer extension (2-3 days)
4. T4 tool output cap + temp file (1-2 days)

All four are LOW effort, validate the Phase 1c framing (tool I/O hygiene), and the telemetry we just added will measure their impact.

**Sprint B — Agent modes (1 week)**:
5. L3 permission-as-data modes (replaces the refuted plan-tier idea cleanly)

**Sprint C — Skills (1-2 weeks)**:
6. T6 skill progressive disclosure (index-then-load)

**Phase 2 work (months)**:
7. L2 frozen 3-tier system prompt
8. L1 Hermes bg-review fork pattern — use as reference implementation
9. Memory graph per Doc 03 — informed by Hermes' production pattern

This sequence keeps every step short (1-2 weeks max), measurable with telemetry already in place, and aligned with the empirically validated post-pivot framing ("context orchestration > cognitive manipulation").

---

## Last updated: 2026-05-19
