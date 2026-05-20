"""Centralised event schema for durin's telemetry channel.

Each ``TypedDict`` here is the payload contract for one event type. The
``EVENTS`` catalog at the bottom is the single source of truth — every
event emitted via ``current_telemetry().log(event_type, data)`` SHOULD
have its ``event_type`` listed in ``EVENTS`` and its data fields should
match the corresponding TypedDict.

This module is purely declarative — no runtime validation by default.
The value comes from:

- One place to look up "what fields does this event have".
- IDE / type-checker support if a caller chooses to type their dict.
- Mechanical refactors stay sound (rename a field here → grep the
  catalog → know everywhere it needs to change).
- Schema audit tools (see ``test_telemetry_schema.py``) compare emit
  sites in the source tree against this catalog, surfacing orphans
  in either direction.

**Convention for base fields** (NOT enforced, but every reviewer should
flag a deviation):

- ``session_key: str | None`` — present on every event emitted from
  inside a run loop iteration or session-scoped service. Use ``None``
  when the event isn't tied to a session.
- ``iteration: int`` — present on every event emitted from inside the
  run loop. Lets dashboards correlate events to the LLM turn they
  belong to.
- Numeric units in the field name suffix: ``*_chars``, ``*_tokens``,
  ``*_bytes``, ``*_s`` (seconds), ``*_ms`` (milliseconds).
- ``snake_case`` everywhere (no camelCase outliers).
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


# ===========================================================================
# Loop-control events
# ===========================================================================


class CircuitBreakerIdleTimeoutEvent(TypedDict):
    """Idle-timeout circuit breaker tripped after N consecutive provider
    timeouts (Tier 1 2C)."""
    consecutive_timeouts: int
    threshold: int
    iteration: int
    session_key: NotRequired[str | None]


class MidTurnPrecheckOverflowEvent(TypedDict):
    """Post-sanitize prompt still exceeded the input budget; turn was
    aborted BEFORE making the LLM call (Tier 2 A2)."""
    iteration: int
    session_key: NotRequired[str | None]
    estimated_tokens: int
    budget_tokens: int


class UnknownToolLoopGuardEvent(TypedDict):
    """Model called an unknown tool name more than ``threshold`` times
    within one turn (Tier 2 B2)."""
    tool_name: str
    attempts: int
    threshold: int
    iteration: int
    session_key: NotRequired[str | None]


class PostCompactionLoopTrippedEvent(TypedDict):
    """Same ``(tool_name, args_hash, result_hash)`` triple repeated
    ``window_size`` times after a successful compaction (Tier 2 C2)."""
    tool_name: str
    repeat_count: int
    iteration: int
    session_key: NotRequired[str | None]


class TurnBudgetEnforcedEvent(TypedDict):
    """Aggregate tool-result size exceeded the per-turn budget; the
    largest results were spilled to disk (Tier 1 2H)."""
    session_key: NotRequired[str | None]
    budget_chars: int
    before_chars: int
    after_chars: int
    spilled_count: int
    tool_count: int


# ===========================================================================
# Compaction events
# ===========================================================================


class CompactionPreemptiveTriggerEvent(TypedDict):
    """Consolidation fired BEFORE the input budget ceiling because the
    pre-emptive ratio kicked in (Tier 2 A1)."""
    session_key: str
    estimated_tokens: int
    trigger_tokens: int
    budget_tokens: int
    context_window_tokens: int
    ratio: float


class CompactionGraceExtendedEvent(TypedDict):
    """LLM wall-clock timeout would have fired during active compaction;
    deadline extended by one grace window (Tier 1 2F)."""
    base_timeout_s: float
    grace_s: float
    session_key: NotRequired[str | None]


class CompactionLockTimeoutEvent(TypedDict):
    """Per-session compaction lock acquisition timed out — a prior
    compaction is still holding it (Tier 2 A3)."""
    session_key: str
    timeout_s: float


# ===========================================================================
# Provider / tool-arg processing
# ===========================================================================


class ProviderParallelToolCallsInjectedEvent(TypedDict):
    """Per-model ``parallel_tool_calls`` override fired (Tier 1 2G).
    Emitted at most once per (model, value, needle) per process —
    audit follow-up P1.2a."""
    model: str
    value: bool
    match_needle: str


class ToolCallArgumentRepairEvent(TypedDict):
    """Tool-call argument JSON needed pre-processing before
    ``json_repair.loads`` (HTML entities, leading/trailing garbage) —
    Tier 2 B1."""
    repairs: list[str]
    original_len: int
    cleaned_len: int
    parsed_ok: bool


class ProviderRateLimitEvent(TypedDict):
    """Transient rate-limit response triggered a retry-with-backoff
    inside ``LLMProvider._run_with_retry``."""
    attempt: int
    delay_s: float
    status_code: NotRequired[int | None]
    persistent: bool
    error: str


class ProviderRateLimitExhaustedEvent(TypedDict):
    """All retry attempts exhausted by repeated rate-limit responses."""
    attempts: int
    error: str


# ===========================================================================
# Cache / context engineering
# ===========================================================================


class CacheUsageEvent(TypedDict):
    """Provider-side prompt-cache usage for one LLM call. Emitted every
    iteration so dashboards can show cache-hit ratio over time."""
    iteration: int
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cache_ratio_pct: float


class HistoryMediaPrunedEvent(TypedDict):
    """History image/audio prune removed at least one media block from
    a completed turn older than the preservation window (Tier 2 B3,
    audit follow-up P1.2b)."""
    image_blocks_removed: int
    audio_blocks_removed: int
    preserve_turns: int
    iteration: int
    session_key: NotRequired[str | None]


# ===========================================================================
# Agent mode (Sprint B / L3)
# ===========================================================================


class AgentModeTurnStartEvent(TypedDict):
    """Snapshot of which agent mode was active at the start of one
    runner turn (so behavior + outcome can be correlated with mode)."""
    mode: str


class AgentModeSwitchEvent(TypedDict):
    """User switched modes via /plan, /build, /explore, or the
    enter_plan_mode tool."""
    from_: str
    to: str
    trigger: str
    reason: NotRequired[str | None]


class AgentModeToolDeniedEvent(TypedDict):
    """Tool call was blocked because the active mode's allow-list
    excluded it (cached LLM-side tool definition referenced a tool no
    longer available)."""
    tool: str
    mode: str


class PlanModePresentedEvent(TypedDict):
    """``exit_plan_mode`` tool finished writing the plan markdown; the
    runtime now expects the model to present the plan to the user."""
    plan_chars: int
    from_mode: str
    plan_path: str


# ===========================================================================
# Tool-level instrumentation
# ===========================================================================


class ToolReadFileEvent(TypedDict):
    path: str
    offset: NotRequired[int]
    limit: NotRequired[int]
    kind: str
    dedup: bool
    total_lines: NotRequired[int]
    returned_lines: NotRequired[int]
    result_chars: NotRequired[int]
    truncated: NotRequired[bool]


class ToolEditFileEvent(TypedDict):
    path: str
    match_strategy: str
    matches: int
    outcome: str
    old_text_chars: int
    new_text_chars: int
    applied: NotRequired[bool]
    replace_all: NotRequired[bool]


class ToolGrepEvent(TypedDict):
    pattern_len: int
    fixed_strings: bool
    case_insensitive: bool
    output_mode: str
    limit: int
    offset: int
    glob_filter: NotRequired[str | None]
    type_filter: NotRequired[str | None]
    displayed: int
    total_before_pagination: int
    result_chars: int
    truncated: bool
    size_truncated: NotRequired[int]
    skipped_binary: NotRequired[int]
    skipped_large: NotRequired[int]


class ToolExecSpillEvent(TypedDict):
    """``exec`` tool output exceeded inline cap and was persisted to
    disk; the inline result is a reference."""
    spilled: bool
    original_chars: int
    rendered_chars: int
    spill_path: NotRequired[str | None]
    spill_error: NotRequired[str | None]


class ToolRepoOverviewEvent(TypedDict):
    path: str
    depth: int
    ecosystems: list[str]
    package_manager: NotRequired[str | None]
    dependency_files_count: int
    entrypoints_count: int
    structure_lines: int
    truncated: bool
    result_chars: int


class ToolListDirEvent(TypedDict):
    """``list_dir`` directory enumeration. ``displayed`` is what the
    model saw, ``total_before_cap`` includes the entries skipped by the
    ``max_entries`` cap (so dashboards can spot models that habitually
    list giant directories)."""
    path: str
    recursive: bool
    max_entries: int
    displayed: int
    total_before_cap: int
    truncated: bool


class ToolWebSearchEvent(TypedDict):
    """``web_search`` dispatch — captures which provider actually served
    the call (brave / duckduckgo / tavily / etc.) and result size, so
    we can spot a provider silently degrading."""
    provider: str
    query_chars: int
    requested_count: int
    result_chars: int
    error: bool


class ToolWebFetchEvent(TypedDict):
    """``web_fetch`` content extraction. ``extractor`` is the layer
    that ultimately produced the result (``jina`` / ``readability`` /
    ``image_passthrough`` / ``validation`` / ``redirect_check``)."""
    extractor: str
    extract_mode: str
    result_chars: int
    error: bool
    is_image: bool


class ToolTodoWriteEvent(TypedDict):
    """``todo_write`` list-replace operation. Counts let us see whether
    the model is actually advancing through todos vs. accumulating
    pending work — and whether the "at most one in_progress" coercion
    fired."""
    total: int
    pending: int
    in_progress: int
    completed: int
    coerced_multiple_in_progress: bool


class AskUserQuestionAskedEvent(TypedDict):
    """``ask_user_question`` tool surfaced a structured prompt to the
    user; the turn is paused awaiting their selection."""
    question_id: str
    question_chars: int
    option_count: int


class AskVisionStartEvent(TypedDict):
    """``interpret_image`` bridge tool dispatched a request to the
    vision aux-model."""
    aux_model: str
    image_bytes: int
    mime: str
    question_chars: int


class AskVisionErrorEvent(TypedDict):
    exception: str


class AskVisionEndEvent(TypedDict):
    response_chars: int
    had_content: bool


class AskAudioStartEvent(TypedDict):
    """``interpret_audio`` bridge tool dispatched a request to the
    audio aux-model."""
    aux_model: str
    audio_bytes: int
    format: str
    question_chars: int


class AskAudioErrorEvent(TypedDict):
    exception: str


class AskAudioEndEvent(TypedDict):
    response_chars: int
    had_content: bool


class SleepStartEvent(TypedDict):
    """``sleep`` tool entered a bounded wait. The tool yields the
    coroutine so other work can proceed while it sleeps."""
    requested_s: float
    actual_s: float
    clamped: bool
    reason: NotRequired[str | None]


class SleepCancelledEvent(TypedDict):
    elapsed_s: float
    reason: NotRequired[str | None]


class SleepEndEvent(TypedDict):
    elapsed_s: float
    reason: NotRequired[str | None]


# ===========================================================================
# Memory subsystem (Phase 1)
# ===========================================================================


class MemoryRecallEvent(TypedDict):
    """memory_search invocation. Logged once per call (not per result)."""

    query: str
    scope: str
    level: str
    result_count: int
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryStoreEvent(TypedDict):
    """memory_store invocation that successfully wrote a memory entry."""

    entry_id: str
    class_name: str
    author: str
    headline: str
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryIngestEvent(TypedDict):
    """memory_ingest invocation that copied an external artifact into ingested/."""

    entry_id: str
    size_bytes: int
    suffix: str
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryEmbeddingLoadEvent(TypedDict):
    """Embedding model loaded into memory. Logged once per process lifetime."""

    model: str
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryEmbeddingEmbedEvent(TypedDict):
    """Embedding call. One event per batch (not per text)."""

    model: str
    batch_size: int
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


# ===========================================================================
# Catalog — single source of truth
# ===========================================================================


EVENTS: dict[str, type] = {
    # Loop control
    "circuit_breaker.idle_timeout": CircuitBreakerIdleTimeoutEvent,
    "mid_turn_precheck.overflow": MidTurnPrecheckOverflowEvent,
    "unknown_tool.loop_guard": UnknownToolLoopGuardEvent,
    "post_compaction_loop.tripped": PostCompactionLoopTrippedEvent,
    "turn_budget.enforced": TurnBudgetEnforcedEvent,
    # Compaction
    "compaction.preemptive_trigger": CompactionPreemptiveTriggerEvent,
    "compaction.grace_extended": CompactionGraceExtendedEvent,
    "compaction.lock_timeout": CompactionLockTimeoutEvent,
    # Provider / tool-arg processing
    "provider.parallel_tool_calls_injected": ProviderParallelToolCallsInjectedEvent,
    "tool_call.argument_repair": ToolCallArgumentRepairEvent,
    "provider.rate_limit": ProviderRateLimitEvent,
    "provider.rate_limit_exhausted": ProviderRateLimitExhaustedEvent,
    # Cache / context engineering
    "cache.usage": CacheUsageEvent,
    "history_media.pruned": HistoryMediaPrunedEvent,
    # Agent mode (Sprint B / L3)
    "agent_mode.turn_start": AgentModeTurnStartEvent,
    "agent_mode.switch": AgentModeSwitchEvent,
    "agent_mode.tool_denied": AgentModeToolDeniedEvent,
    "plan_mode.presented": PlanModePresentedEvent,
    # Tool-level instrumentation
    "tool.read_file": ToolReadFileEvent,
    "tool.edit_file": ToolEditFileEvent,
    "tool.grep": ToolGrepEvent,
    "tool.exec.spill": ToolExecSpillEvent,
    "tool.repo_overview": ToolRepoOverviewEvent,
    "tool.list_dir": ToolListDirEvent,
    "tool.web_search": ToolWebSearchEvent,
    "tool.web_fetch": ToolWebFetchEvent,
    "tool.todo_write": ToolTodoWriteEvent,
    "ask_user.question_asked": AskUserQuestionAskedEvent,
    "ask_vision.start": AskVisionStartEvent,
    "ask_vision.error": AskVisionErrorEvent,
    "ask_vision.end": AskVisionEndEvent,
    "ask_audio.start": AskAudioStartEvent,
    "ask_audio.error": AskAudioErrorEvent,
    "ask_audio.end": AskAudioEndEvent,
    "sleep.start": SleepStartEvent,
    "sleep.cancelled": SleepCancelledEvent,
    "sleep.end": SleepEndEvent,
    # Memory subsystem (Phase 1)
    "memory.recall": MemoryRecallEvent,
    "memory.store": MemoryStoreEvent,
    "memory.ingest": MemoryIngestEvent,
    # Memory subsystem (Phase 2 — embedding)
    "memory.embedding.load": MemoryEmbeddingLoadEvent,
    "memory.embedding.embed": MemoryEmbeddingEmbedEvent,
}


__all__ = [
    "EVENTS",
    # Loop control
    "CircuitBreakerIdleTimeoutEvent",
    "MidTurnPrecheckOverflowEvent",
    "UnknownToolLoopGuardEvent",
    "PostCompactionLoopTrippedEvent",
    "TurnBudgetEnforcedEvent",
    # Compaction
    "CompactionPreemptiveTriggerEvent",
    "CompactionGraceExtendedEvent",
    "CompactionLockTimeoutEvent",
    # Provider / tool-arg processing
    "ProviderParallelToolCallsInjectedEvent",
    "ToolCallArgumentRepairEvent",
    "ProviderRateLimitEvent",
    "ProviderRateLimitExhaustedEvent",
    # Cache / context engineering
    "CacheUsageEvent",
    "HistoryMediaPrunedEvent",
    # Agent mode
    "AgentModeTurnStartEvent",
    "AgentModeSwitchEvent",
    "AgentModeToolDeniedEvent",
    "PlanModePresentedEvent",
    # Tool-level
    "ToolReadFileEvent",
    "ToolEditFileEvent",
    "ToolGrepEvent",
    "ToolExecSpillEvent",
    "ToolRepoOverviewEvent",
    "ToolListDirEvent",
    "ToolWebSearchEvent",
    "ToolWebFetchEvent",
    "ToolTodoWriteEvent",
    "AskUserQuestionAskedEvent",
    "AskVisionStartEvent",
    "AskVisionErrorEvent",
    "AskVisionEndEvent",
    "AskAudioStartEvent",
    "AskAudioErrorEvent",
    "AskAudioEndEvent",
    "SleepStartEvent",
    "SleepCancelledEvent",
    "SleepEndEvent",
    # Memory subsystem
    "MemoryRecallEvent",
    "MemoryStoreEvent",
    "MemoryIngestEvent",
    "MemoryEmbeddingLoadEvent",
    "MemoryEmbeddingEmbedEvent",
]
