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


class ContextCompositionEvent(TypedDict):
    """Per-turn breakdown of the prompt we send to the LLM.

    Emitted from ``ContextBuilder.build_messages`` *before* the call,
    so we can correlate with the post-call ``cache.usage`` event and
    answer questions like "of the cached tokens, which tier is doing
    the caching?" or "how much of our context is memory vs history
    today?".

    All counts are tiktoken-estimated (cl100k_base) over the rendered
    text — they're indicative, not byte-exact for every provider's
    tokenizer.
    """
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]
    # Three-tier system prompt (cache-friendly layout).
    stable_tokens: int
    stable_breakdown: dict[str, int]      # identity / bootstrap / skills_active /
                                          # skills_catalog / memory_hot
    context_tokens: int
    volatile_tokens: int
    volatile_breakdown: dict[str, int]    # memory_long_term / recent_history /
                                          # session_summary
    # Messages portion.
    history_msg_tokens: int               # prior turns we pass back
    current_msg_tokens: int               # current user message + runtime ctx
    # Tool definitions JSON.
    tools_tokens: int
    # Sum of all the above (what we expect provider's prompt_tokens to
    # approximate, modulo tokenizer differences).
    estimated_total: int


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


class MemoryRecallVectorEvent(TypedDict):
    """Vector retrieval path inside memory_search. Logged once per call.

    Separate from ``memory.recall`` (the generic grep-or-vector summary
    event) so dashboards can split latency / hit count by strategy.

    Entity-aware ranking telemetry (S2 per archived doc 24) piggy-backs
    on this event instead of duplicating into ``memory.recall.entity_aware``
    so consumers can correlate distance-based vs entity-boosted retrieval
    on a single record.
    """

    query: str
    scope: str
    embedding_model: str
    hit_count: int
    duration_ms: float
    # Entity-aware ranking fields (S2 doc 24). NotRequired so the schema
    # accepts older events that pre-date W1 wiring.
    ranking: NotRequired[str]                 # "default" | "entity_aware"
    query_entities_count: NotRequired[int]
    reordered: NotRequired[bool]              # True if top-1 changed pre/post rerank
    top_1_id_before: NotRequired[str]
    top_1_id_after: NotRequired[str]
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryDreamStartEvent(TypedDict):
    """Entity-centric dream pass began (doc 25 §2.A.1).

    Emitted only when the runner actually acquired the lock and is
    about to execute. Skipped passes get :class:`MemoryDreamSkippedEvent`
    instead so dashboards can split "ran" from "didn't run".
    """

    trigger: str  # "cron_daily" | "post_compaction" | "session_close" | "threshold" | "manual"
    entity_filter: str  # entity ref when narrowed, "" otherwise
    entities_pending: int
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryDreamEndEvent(TypedDict):
    """Entity-centric dream pass completed (doc 25 §2.A.1).

    Mirrors :class:`MemoryDreamStartEvent` with the outcome counters
    and wall-clock duration. ``entities_failed`` counts entities whose
    consolidate_entity raised — the rest of the pass still runs, so a
    non-zero failed value is a soft signal, not a stop condition.
    """

    trigger: str
    entity_filter: str
    entities_consolidated: int
    entities_failed: int
    duration_s: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryDreamSkippedEvent(TypedDict):
    """Entity-centric dream trigger fired but no work was done.

    Reasons:

    - ``"throttle"``: ``min_seconds_between_runs`` not elapsed yet.
    - ``"no_pending"``: no entities have post-cursor entries.
    - ``"concurrent_lock"``: another process is dreaming right now.
    - ``"disabled"``: ``memory.dream.enabled`` is False.
    """

    trigger: str
    reason: str
    entity_filter: str
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryAbsorbJudgedEvent(TypedDict):
    """LLM-judge ran on an alias-overlap candidate pair (doc 25 §2.D).

    Emitted for every candidate that survived the cross-type filter
    and the 24h quarantine — i.e. every pair that actually reached
    the LLM. Use for tuning ``confidence_threshold`` against the
    empirical distribution of confidences.
    """

    canonical: str  # ref of the page picked as canonical (slug winner)
    absorbed: str   # ref of the page that would be absorbed
    verdict: str    # "same" | "different" | "unclear"
    confidence: int  # 0-100
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryAbsorbAutoMergedEvent(TypedDict):
    """Auto-absorb actually ran a merge (doc 25 §2.D).

    Emitted after :meth:`EntityAbsorption.absorb` succeeds via the
    auto-trigger path. ``sha`` points to the merge commit (empty
    when the absorb was a no-op because the absorbed page was already
    archived — rare, only happens under racy concurrent triggers).
    """

    canonical: str
    absorbed: str
    confidence: int
    sha: str  # empty when absorb returned None (idempotent no-op)
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryAbsorbSkippedEvent(TypedDict):
    """Auto-absorb considered a candidate but did not merge (doc 25 §2.D).

    Reasons:

    - ``"cross_type"``: candidate refs span different entity types
      (e.g. person:marcelo vs project:marcelo) — filtered before judge.
    - ``"quarantine"``: at least one page is younger than ``min_age_hours``
      (mitigates premature consolidation per glm peer review C3).
    - ``"below_threshold"``: judge said "same" but confidence < floor.
    - ``"verdict_different"`` / ``"verdict_unclear"``: judge declined.
    - ``"judge_failed"``: LLM call or parse failure after all retries.
    - ``"page_load_failed"``: one of the two pages couldn't be loaded.
    """

    canonical: str
    absorbed: str
    confidence: int  # 0 if reason is cross_type / quarantine / judge_failed / page_load_failed
    reason: str
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryAbsorbRevertedEvent(TypedDict):
    """A previously auto-absorbed merge was reverted (doc 25 §2.D + glm C5).

    Emitted from ``durin memory revert`` when the target commit's
    trailers include ``Reason: auto``. This is the "regret rate"
    signal — high revert rate suggests the threshold is too low or
    the judge is too permissive for this workspace's content.
    """

    canonical: str
    absorbed: str
    original_sha: str  # the auto-merge commit being undone
    confidence: int  # confidence the original auto-merge recorded
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryStoreBlockedNearDuplicateEvent(TypedDict):
    """memory_store dedup pre-persist (T1.7 per archived doc 23) refused
    a write because the embedding distance to an existing entry fell
    below the configured threshold. The model receives a warning and may
    re-call with ``force=True`` to bypass; this event records the
    underlying decision so the §2.D gate can be measured ("duplicates
    detected per month").
    """

    candidate_class_name: str
    existing_id: str
    existing_class_name: str
    distance: float
    threshold: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryDreamPatchAppliedEvent(TypedDict):
    """A Dream apply for one entity completed successfully (doc 07 §6.5).

    Counts the ops that landed plus the diagnostics dashboards need
    to spot drift (cursor advanced, body delta length, sources count).
    ``failure_kind`` is always empty here — the failure variant lands
    in :class:`MemoryDreamEntityFailedEvent`.
    """

    entity_ref: str
    trigger: str
    ops_applied: int
    sources_count: int
    body_delta_chars: int
    cursor_after: str  # ISO timestamp the runner stamped on the page
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryDreamEntityFailedEvent(TypedDict):
    """A Dream apply for one entity failed (doc 07 §6.4).

    Emitted on every failed entity (no batching). ``kind`` matches
    :class:`durin.memory.dream_apply.DreamApplyFailureKind` for the
    structural cases, plus ``"llm_call_failed"`` for ambient LLM
    issues and ``"parse_failed"`` for unparseable LLM output. Only
    *structural* kinds (validation / patch_runtime / round_trip)
    contribute to the quarantine counter — that's enforced in
    :mod:`durin.memory.dream_quarantine`, not here. The two are kept
    distinct so dashboards can spot a network outage vs a busted
    entity.
    """

    entity_ref: str
    trigger: str
    kind: str
    error_message: str  # bounded; caller truncates if huge
    failure_count_now: int  # post-increment value, 0 for ambient
    quarantined_until: NotRequired[str]  # ISO timestamp when 3-strike trip
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryIndexWriteEvent(TypedDict):
    """One upsert into the FTS5 lexical index (doc 07 §9.1).

    Fires per file written, so dashboards can detect bursty writes
    (e.g., during `durin reindex`) vs steady-state agent activity.
    ``index`` is either ``"fts"`` (lexical) or ``"lancedb"`` (vector);
    a tool that writes to both fires twice.
    """

    uri: str
    index: str  # "fts" | "lancedb"
    op: str  # "upsert" | "delete"
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryIndexRebuildEvent(TypedDict):
    """A full index rebuild completed (doc 07 §9.2).

    Emitted by ``durin reindex``. ``target`` is ``"fts"``,
    ``"lancedb"``, or ``"all"``. ``indexed`` + ``errors`` mirror
    :class:`durin.memory.indexer.IndexStats`.
    """

    target: str
    indexed: int
    errors: int
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryIndexStalenessDetectedEvent(TypedDict):
    """The on-disk index disagrees with the markdown source (doc 07 §9.3).

    Emitted by the health-check cron when it spots a uri whose
    ``fts_meta.mtime`` lags behind the file's mtime, or a file under
    ``memory/`` that has no row in the index. ``reason`` captures the
    detection signal so dashboards can split "missing row" vs "stale
    mtime" trends.
    """

    uri: str
    reason: str  # "missing_row" | "mtime_lag" | "row_for_missing_file"
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryRecallLexicalEvent(TypedDict):
    """One FTS5 lexical search ran (doc 07 §4.3).

    ``route`` is the value of
    :class:`durin.memory.query_router.LexicalRoute`
    (``unicode61`` | ``trigram`` | ``like_substring``). Dashboards
    use ``route`` distribution + ``hit_count`` to spot when CJK
    queries are falling into the LIKE fallback instead of trigram.
    """

    route: str
    query_chars: int
    cjk_chars: int
    hit_count: int
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryRecallRRFEvent(TypedDict):
    """Cross-source RRF fusion completed (doc 07 §4.5).

    Logs the per-source contribution so dashboards can see whether the
    vector / lexical / grep paths each surfaced anything. ``boosted``
    is True when the caller passed ``keywords`` and the lexical weight
    was bumped to 2.5.
    """

    vector_count: int
    lexical_count: int
    grep_count: int
    fused_count: int
    boosted: bool
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryHotLayerFailureEvent(TypedDict):
    """Hot-layer assembly failed for one component (per doc 06 §8.7).

    The hot layer renders five sections (identity / canonical / fragments /
    headlines / entities). If any section's disk read or parse raises,
    the renderer logs this event and degrades that section to empty so
    the agent prompt still builds. The whole layer never fails hard.

    ``component`` identifies which section degraded. For per-page parse
    failures (one malformed entity page in an otherwise-healthy walk),
    the value includes the filename suffix (e.g.
    ``"canonical_blocks:broken.md"``) so dashboards can tell systemic
    failures apart from one-off bad files.
    """

    component: str  # "canonical_blocks" | "fragment_blocks" | "identity" |
                    # "headlines" | "entities" | "canonical_blocks:<file>"
    error: str
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
    "context.composition": ContextCompositionEvent,
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
    "memory.recall.vector": MemoryRecallVectorEvent,
    "memory.store.blocked_near_duplicate": MemoryStoreBlockedNearDuplicateEvent,
    "memory.dream.start": MemoryDreamStartEvent,
    "memory.dream.end": MemoryDreamEndEvent,
    "memory.dream.skipped": MemoryDreamSkippedEvent,
    "memory.dream.patch_applied": MemoryDreamPatchAppliedEvent,
    "memory.dream.entity_failed": MemoryDreamEntityFailedEvent,
    "memory.absorb.judged": MemoryAbsorbJudgedEvent,
    "memory.absorb.auto_merged": MemoryAbsorbAutoMergedEvent,
    "memory.absorb.skipped": MemoryAbsorbSkippedEvent,
    "memory.absorb.reverted": MemoryAbsorbRevertedEvent,
    "memory.hot_layer.failure": MemoryHotLayerFailureEvent,
    "memory.index.write": MemoryIndexWriteEvent,
    "memory.index.rebuild": MemoryIndexRebuildEvent,
    "memory.index.staleness_detected": MemoryIndexStalenessDetectedEvent,
    "memory.recall.lexical": MemoryRecallLexicalEvent,
    "memory.recall.rrf": MemoryRecallRRFEvent,
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
    "MemoryRecallVectorEvent",
    "MemoryDreamPatchAppliedEvent",
    "MemoryDreamEntityFailedEvent",
    "MemoryHotLayerFailureEvent",
    "MemoryRecallLexicalEvent",
    "MemoryRecallRRFEvent",
    "MemoryIndexWriteEvent",
    "MemoryIndexRebuildEvent",
    "MemoryIndexStalenessDetectedEvent",
]
