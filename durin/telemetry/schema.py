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
    timeouts."""
    consecutive_timeouts: int
    threshold: int
    iteration: int
    session_key: NotRequired[str | None]


class MidTurnPrecheckOverflowEvent(TypedDict):
    """Post-sanitize prompt still exceeded the input budget; turn was
    aborted BEFORE making the LLM call."""
    iteration: int
    session_key: NotRequired[str | None]
    estimated_tokens: int
    budget_tokens: int


class MidTurnPrecheckRecoveredEvent(TypedDict):
    """Post-sanitize prompt was over budget, but emergency-trimming the
    largest tool results brought it back under the wall — the turn proceeded
    instead of aborting."""
    iteration: int
    session_key: NotRequired[str | None]
    estimated_tokens: int
    budget_tokens: int


class OverflowRetryForcedConsolidationEvent(TypedDict):
    """An iteration-0 overflow (before any tool ran) forced a fresh
    consolidation + context rebuild, then re-ran the turn once."""
    session_key: NotRequired[str | None]
    attempt: int


class TurnLatencyEvent(TypedDict):
    """Per-turn wall-clock breakdown so dashboards can split where the time
    went: the model round-trips (``llm_ms``), tool execution (``tools_ms``),
    and everything else — context build, memory, sanitize, consolidation,
    save/respond — as ``local_ms``. ``states`` carries the per-state-machine
    durations (RESTORE/COMPACT/BUILD/RUN/SAVE/RESPOND)."""
    session_key: NotRequired[str | None]
    turn_id: str
    total_ms: float
    llm_ms: float
    tools_ms: float
    local_ms: float
    states: dict[str, float]
    stop_reason: NotRequired[str]


class UnknownToolLoopGuardEvent(TypedDict):
    """Model called an unknown tool name more than ``threshold`` times
    within one turn."""
    tool_name: str
    attempts: int
    threshold: int
    iteration: int
    session_key: NotRequired[str | None]


class PostCompactionLoopTrippedEvent(TypedDict):
    """Same ``(tool_name, args_hash, result_hash)`` triple repeated
    ``window_size`` times after a successful compaction."""
    tool_name: str
    repeat_count: int
    iteration: int
    session_key: NotRequired[str | None]


class TurnBudgetEnforcedEvent(TypedDict):
    """Aggregate tool-result size exceeded the per-turn budget; the
    largest results were spilled to disk."""
    session_key: NotRequired[str | None]
    budget_chars: int
    before_chars: int
    after_chars: int
    spilled_count: int
    tool_count: int


class ToolsParallelismEvent(TypedDict):
    """Per-turn shape of tool-call parallelism. Counts all three forms:
    harness batching (``max_batch_size`` > 1), intra-tool list fan-out
    (``fanout_calls``/``fanout_items``), and background launches
    (``background_launches``, e.g. spawn). ``parallelized`` is true if any
    occurred."""
    session_key: NotRequired[str | None]
    tool_calls: int
    batches: int
    max_batch_size: int
    concurrency_safe_calls: int
    fanout_calls: int
    fanout_items: int
    background_launches: int
    parallelized: bool
    concurrent_tools_enabled: bool


# ===========================================================================
# Compaction events
# ===========================================================================


class CompactionPreemptiveTriggerEvent(TypedDict):
    """Consolidation fired BEFORE the input budget ceiling because the
    pre-emptive ratio kicked in."""
    session_key: str
    estimated_tokens: int
    trigger_tokens: int
    budget_tokens: int
    context_window_tokens: int
    ratio: float


class CompactionGraceExtendedEvent(TypedDict):
    """LLM wall-clock timeout would have fired during active compaction;
    deadline extended by one grace window."""
    base_timeout_s: float
    grace_s: float
    session_key: NotRequired[str | None]


class CompactionLockTimeoutEvent(TypedDict):
    """Per-session compaction lock acquisition timed out — a prior
    compaction is still holding it."""
    session_key: str
    timeout_s: float


# ===========================================================================
# Provider / tool-arg processing
# ===========================================================================


class ProviderParallelToolCallsInjectedEvent(TypedDict):
    """Per-model ``parallel_tool_calls`` override fired.
    Emitted at most once per (model, value, needle) per process."""
    model: str
    value: bool
    match_needle: str


class ToolCallArgumentRepairEvent(TypedDict):
    """Tool-call argument JSON needed pre-processing before
    ``json_repair.loads`` (HTML entities, leading/trailing garbage)."""
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
    a completed turn older than the preservation window."""
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


class TurnMemoryUsageEvent(TypedDict):
    """Per-turn rollup of memory-recall tool activity, emitted once per
    turn at save time (``AgentLoop._state_save``).

    Gives turn-level denominators for silent-miss and prefetch
    substitution analysis without reconstructing turn boundaries from
    the raw event stream: a turn that answered without consulting
    memory shows ``search_calls == 0``. Emitted on every turn,
    including turns with zero tool calls — those rows ARE the signal.
    """

    search_calls: int          # memory_search invocations this turn
    drill_calls: int           # memory_drill invocations this turn
    tool_calls_total: int      # all tool calls this turn (denominator)
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


# ===========================================================================
# Agent mode
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
    engine: NotRequired[str]  # "rg" (ripgrep pre-filter) | "python" (walk)
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


class ExecuteCodeEvent(TypedDict):
    """One execute_code run (programmatic tool calling sandbox).

    ``tool_calls`` is the per-tool call histogram — the primary signal for
    how the sandbox is actually used (real batch vs trivial 0-1 call script).
    """
    status: str  # success | error | timeout
    tool_calls_made: int
    tool_calls: NotRequired[dict[str, int]]
    code_chars: NotRequired[int]
    duration_ms: float
    stdout_chars: int
    exit_code: NotRequired[int | None]


class PostEditCheckEvent(TypedDict):
    """A post-edit linter ran after write_file/edit_file (see
    ``durin/agent/tools/post_edit_check.py``)."""
    path: str
    checker: str
    exit_code: int | None
    issue_lines: int
    duration_ms: float
    skipped_reason: NotRequired[str]


class ProcessSpawnEvent(TypedDict):
    """A background process was started via ``exec(background=true)``."""
    proc_id: str
    pid: int | None
    command_chars: int


class ProcessExitEvent(TypedDict):
    """A tracked background process exited (reader observed EOF)."""
    proc_id: str
    pid: int | None
    exit_code: int | None
    runtime_s: float
    output_chars: int


class ProcessKillEvent(TypedDict):
    """The ``process`` tool (or agent shutdown) killed a background
    process group."""
    proc_id: str
    pid: int | None
    force: bool


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
    offset: NotRequired[int]
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


class ToolNoteDecisionEvent(TypedDict):
    """A decision/finding was recorded via the note_decision tool (concern B)."""

    total: int


class DecisionLogCappedEvent(TypedDict):
    """Oldest decision-log entries dropped to keep the task-state anchor within caps."""

    dropped: int
    source: str  # "tool" | "auto"


class DecisionLogExtractedEvent(TypedDict):
    """Decisions auto-extracted from a compacted span into the task-state anchor (concern B)."""

    count: int
    session_key: str


class AskUserQuestionAskedEvent(TypedDict):
    """``ask_user_question`` tool surfaced a structured prompt to the
    user; the turn is paused awaiting their selection."""
    question_id: str
    question_chars: int
    option_count: int


class AskUserAnswerReceivedEvent(TypedDict):
    """Blocking ask_user (V2): the user's answer arrived in-turn and the
    tool resumed the same turn with it."""
    question_id: str
    wait_ms: int


class AskUserAnswerTimeoutEvent(TypedDict):
    """Blocking ask_user (V2): no answer within the window — the tool
    degraded to V1 yield semantics."""
    question_id: str
    timeout_s: int


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
    """memory_search invocation. Logged once per call (not per result).

    Diagnostic fields (`strategy`, `duration_ms`, `total_candidates`)
    emit on every call. `keywords` carries the LLM-supplied hint string
    (None when omitted). `recovered_from` + `recovery_duration_ms` only
    emit on degraded runs — matches the tool response shape, which omits
    them on clean runs.
    """

    query: str
    scope: str
    level: str
    result_count: int
    strategy: str
    duration_ms: float
    total_candidates: int
    skill_result_count: NotRequired[int]
    keywords: NotRequired[str | None]
    # Hits collapsed to pointer lines because their rendered content was
    # already in the caller's hot layer. 0 when nothing deduped or when
    # dedup is off (subagent scope).
    in_context_deduped: NotRequired[int]
    recovered_from: NotRequired[list[str]]
    recovery_duration_ms: NotRequired[float]
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


class MemoryForgetEvent(TypedDict):
    """memory_forget invocation that archived an entry + dropped its index rows."""

    uri: str
    class_name: str
    reason: str
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

    Entity-aware ranking telemetry piggy-backs on this event instead of
    duplicating into ``memory.recall.entity_aware`` so consumers can
    correlate distance-based vs entity-boosted retrieval on a single record.
    """

    query: str
    scope: str
    embedding_model: str
    hit_count: int
    duration_ms: float
    # Entity-aware ranking fields. NotRequired so the schema accepts older
    # events that pre-date the entity-aware wiring.
    ranking: NotRequired[str]                 # "default" | "entity_aware"
    query_entities_count: NotRequired[int]
    reordered: NotRequired[bool]              # True if top-1 changed pre/post rerank
    top_1_id_before: NotRequired[str]
    top_1_id_after: NotRequired[str]
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryDreamStartEvent(TypedDict):
    """A dream pass began. Emitted by the new extract / refine passes
    (``dream_passes.py``). ``kind`` is ``"extract"`` or ``"refine"``."""

    kind: str
    session_key: NotRequired[str | None]


class MemoryDreamEndEvent(TypedDict):
    """A dream pass completed. Emitted by the new extract / refine passes.

    ``kind`` is ``"extract"`` or ``"refine"``; ``duration_ms`` is always
    present. The extract pass sets ``entities_consolidated`` /
    ``entities_failed`` / ``sessions`` / ``yielded`` (``yielded=True`` when the
    ``max_seconds_per_run`` cap was hit and the cursor will resume next time);
    the refine pass sets ``merged`` / ``kept`` / ``candidates``.
    """

    kind: str
    duration_ms: int
    entities_consolidated: NotRequired[int]
    entities_failed: NotRequired[int]
    sessions: NotRequired[int]
    yielded: NotRequired[bool]
    merged: NotRequired[int]
    kept: NotRequired[int]
    candidates: NotRequired[int]


class MemoryAbsorbJudgedEvent(TypedDict):
    """LLM-judge ran on an alias-overlap candidate pair.

    Emitted for every candidate that survived the cross-type filter
    and the run-scoped quarantine — i.e. every pair that actually reached
    the LLM. Use for tuning ``confidence_threshold`` against the
    empirical distribution of confidences.
    """

    canonical: str  # ref of the page picked as canonical (slug winner)
    absorbed: str   # ref of the page that would be absorbed
    verdict: str    # "same" | "different" | "unclear"
    confidence: int  # 0-100
    duration_ms: float
    entity_type: NotRequired[str]  # both pages share the type (cross-type pairs are filtered)
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryAbsorbAutoMergedEvent(TypedDict):
    """Auto-absorb actually ran a merge.

    Emitted after :meth:`EntityAbsorption.absorb` succeeds via the
    auto-trigger path. ``sha`` points to the merge commit (empty
    when the absorb was a no-op because the absorbed page was already
    archived — rare, only happens under racy concurrent triggers).
    """

    canonical: str
    absorbed: str
    confidence: int
    sha: str  # empty when absorb returned None (idempotent no-op)
    entity_type: NotRequired[str]  # both pages share the type (cross-type pairs are filtered)
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryAbsorbSkippedEvent(TypedDict):
    """Auto-absorb considered a candidate but did not merge.

    Reasons:

    - ``"cross_type"``: candidate refs span different entity types
      (e.g. person:marcelo vs project:marcelo) — filtered before judge.
    - ``"tombstoned"``: the user previously rejected this pair
      (recorded in ``.refine_tombstones.json``).
    - ``"load_failed"``: one or both entity pages could not be loaded from disk.
    - ``"user_managed"``: either page is ``author == "user_authored"``.
    - ``"quarantine"``: at least one page was created at/after the run start
      (the run never merges its own fresh output).
    - ``"judge_error"``: the judge raised ``JudgeError`` (unparseable verdict
      after all retries) and the pair was skipped for this run.

    Declined verdicts ("different" / "unclear" / below-threshold "same") are
    not skips — they show up in ``memory.absorb.judged`` instead.
    """

    canonical: str
    absorbed: str
    confidence: NotRequired[int]  # never emitted today — skips fire before a usable verdict
    reason: str
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryAbsorbRevertedEvent(TypedDict):
    """A previously auto-absorbed merge was reverted.

    Emitted from ``durin memory revert`` when the target commit's
    trailers include ``Reason: auto``. This is the "regret rate"
    signal — high revert rate suggests the threshold is too low or
    the judge is too permissive for this workspace's content.
    """

    canonical: str
    absorbed: str
    original_sha: str  # the auto-merge commit being undone


class MemoryAbsorbEscalatedEvent(TypedDict):
    """A borderline pair was handed to the Tier-2 sub-agent judge.

    Emitted after the bounded sub-agent returns a verdict, before the
    merge/keep decision. Use to track escalation rate and Tier-2 outcomes.
    """

    canonical: str
    absorbed: str
    verdict: str   # "same" | "different" | "unclear"
    confidence: int  # 0-100


class MemoryAbsorbEscalationCappedEvent(TypedDict):
    """A borderline pair was NOT escalated because the run hit its per-run
    Tier-2 ceiling. The pair keeps the cheap verdict. Use to detect a run
    that needs a higher ceiling or fewer borderline pairs."""

    canonical: str
    absorbed: str


class MemoryStoreBlockedNearDuplicateEvent(TypedDict):
    """memory_store dedup pre-persist refused a write because the embedding
    distance to an existing entry fell below the configured threshold. The
    model receives a warning and may re-call with ``force=True`` to bypass;
    this event records the underlying decision so duplicate rates can be
    measured over time.
    """

    candidate_class_name: str
    existing_id: str
    existing_class_name: str
    distance: float
    threshold: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryDreamPatchAppliedEvent(TypedDict):
    """The extract dream applied attributes to one entity (one emit per entity).

    Reuses the legacy event name so existing dashboards keep counting
    consolidations. ``source_ref`` is the session-turn marker the attributes
    were extracted from.
    """

    entity_ref: str
    ops_applied: int  # number of attributes applied
    trigger: str  # "extract"
    committed: bool
    source_ref: str


class MemoryDreamDiscoverEvent(TypedDict):
    """The extract dream's mention-discovery stage processed one session.

    It found durable facts about entities the agent did NOT upsert and
    wrote them as dream-authored pages. ``proposed`` is what the LLM
    returned; ``written`` is what was committed (new/updated entities);
    ``skipped`` were dropped as already-handled or tombstoned. Lets
    dashboards measure discovery precision over time.
    """

    proposed: int
    written: int
    skipped: int
    refs: list[str]  # the entity refs written


class MemoryDreamLearningsEvent(TypedDict):
    """The extract dream's learnings-sweep stage processed one session.

    It found durable preferences and corrections in conversation turns and
    wrote them as feedback/stance/practice entities. ``proposed`` is what the
    LLM returned (before type-guard filtering); ``written`` is what was
    committed. Lets dashboards measure learnings precision over time.
    """

    proposed: int
    written: int
    refs: list[str]  # the entity refs written


class MemoryDreamRunSummaryEvent(TypedDict):
    """A whole dream run finished — one summary entry per run.

    Emitted by the cron dream handler after all passes complete so the Dream
    feed always shows that a run happened and what it did, even when nothing
    changed: an empty run still leaves a "ran — no new changes" entry instead of
    silently updating only the last-run timestamp. Counts are the headline pass
    stats; per-item detail comes from the individual memory.dream.* events.
    """

    sessions: int          # new sessions the extract pass processed
    entities: int          # entity attribute updates written this run
    merged: int            # entities auto-merged by the refine pass
    skills_created: int    # NEW skills authored by the skill-extract pass
    skills_improved: int   # existing skills edited by the curation pass


class MemoryEntityRelationCapWarnedEvent(TypedDict):
    """An entity write took its relation count across the soft cap (50). The
    write proceeded (alert-only); this event lets dashboards spot mega-hub
    formation before sub-paging becomes necessary.
    """

    entity_ref: str
    current_count: int  # relations before this write
    new_count: int  # relations after this write
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryEntityRelationCapRejectedEvent(TypedDict):
    """An entity write crossed the hard relation cap (200). This is
    ALERT-ONLY — the write is NOT blocked and no relation is dropped;
    the event is the operator signal that an entity has grown a
    pathological number of relations.
    """

    entity_ref: str
    current_count: int  # relations before this write
    new_count: int  # relations after this write
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryIndexWriteEvent(TypedDict):
    """One upsert into the FTS5 lexical index.

    Fires per file written, so dashboards can detect bursty writes
    (e.g., during dream consolidations or drift repairs) vs
    steady-state agent activity. ``index`` is either ``"fts"``
    (lexical) or ``"lancedb"`` (vector); only ``"fts"`` is emitted
    today since `reindex_one_file` only writes the FTS row.

    ``trigger`` + ``duration_ms`` enable dashboards to measure
    index write latency and split watcher steady state from
    dream/drift bursts.
    """

    uri: str
    index: str  # "fts" | "lancedb"
    op: str  # "upsert" | "delete"
    trigger: str  # "watcher" | "dream_apply" | "drift_repair"
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryIndexRebuildEvent(TypedDict):
    """A full index rebuild completed.

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
    """The on-disk index disagrees with the markdown source.

    Emitted by the health-check cron when it spots a uri whose
    ``fts_meta.mtime`` lags behind the file's mtime, or a file under
    ``memory/`` that has no row in the index. ``reason`` captures the
    detection signal so dashboards can split "missing row" vs "stale
    mtime" trends.

    ``delta_seconds`` carries the staleness magnitude
    (``current_file_mtime - indexed_mtime``) but only on
    ``reason='mtime_lag'`` — the other two reasons have no
    indexed_mtime to compare against. Dashboards graph p50/p95 of
    ``delta_seconds`` to detect watcher gap regressions. Note that
    recovery latency (write_time - detect_time) and staleness magnitude
    are different metrics.
    """

    uri: str
    reason: str  # "missing_row" | "mtime_lag" | "row_for_missing_file"
    delta_seconds: NotRequired[float]
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryRecallLexicalEvent(TypedDict):
    """One FTS5 lexical search ran.

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
    """Cross-source RRF fusion completed.

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


class MemoryRecallGrepVerifyEvent(TypedDict):
    """Grep-verify boost step completed.

    Emitted when the pipeline literally re-verified vector-sourced
    hits that the lexical top-50 missed. ``candidates`` is how many
    hits were checked; ``verified`` how many literally matched and
    received the lexical-grade contribution.
    """

    candidates: int
    verified: int
    duration_ms: float
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryRecallRerankEvent(TypedDict):
    """Cross-encoder rerank step completed.

    Emitted whenever the opt-in reranker ran. ``output_count``
    reflects the candidates carried forward (the reordered top-50).
    ``blend_alpha`` is the CE weight in the RRF/CE blend (0.0 when the
    CE fell through); ``fallback`` is True when the CE failed to score
    and the RRF order was kept verbatim.
    """

    input_count: int
    output_count: int
    duration_ms: float
    blend_alpha: NotRequired[float]
    fallback: NotRequired[bool]
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryRecallFailureEvent(TypedDict):
    """A search-path component failed and the pipeline recovered or degraded.

    Emitted once per `run_search_pipeline` invocation where at least
    one of the safe wrappers (`_safe_vector_search`,
    `_safe_lexical_search`, `_safe_grep_fallback`) caught an
    exception. ``component`` carries the comma-separated list of
    affected sources; ``degraded_to`` describes which sources still
    produced hits.

    ``recovery_succeeded`` is True iff the pipeline returned a
    non-empty result set despite the failure — i.e. the surviving
    sources covered the loss. False means everything failed AND no
    hits surfaced (rare; usually one source's failure is masked by
    the others).
    """

    component: str  # comma-joined affected sources
    recovery_attempted: bool
    recovery_succeeded: bool
    recovery_duration_ms: float
    degraded_to: str  # one of: vector_only | lexical_only | grep_only | none | full
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemorySkillMissEvent(TypedDict):
    """A `kinds="skill"` memory_search returned zero results.

    The skill-retrieval analogue of ``memory.search.failure``: emitted
    once per skill-targeted query that surfaced nothing. ``had_skill_candidate``
    is True when the workspace DOES contain skills on disk — i.e. a real
    silent-miss worth investigating (skills exist but none were retrieved),
    versus an expected empty (no skills authored yet).
    """

    query: str
    result_count: int
    had_skill_candidate: bool
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryHealthCheckEvent(TypedDict):
    """One health-check tick completed.

    ``status`` is the aggregate label (``ok`` / ``degraded`` /
    ``critical``); ``components`` carries per-probe status
    (``fts`` / ``lance`` / future additions).

    ``tick_id`` (per-tick UUID hex) and ``duration_ms`` (wall-clock of
    the tick) are required fields enabling log correlation and latency
    tracking.
    """

    tick_id: str
    status: str
    components: dict[str, str]
    drift_count: int
    duration_ms: float
    errors: NotRequired[dict[str, str]]
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryHealthCriticalEvent(TypedDict):
    """A component crossed the consecutive-failure threshold.

    Emitted once per component per failure burst. Reset on the next
    successful tick.

    ``manual_recovery_hint`` carries the CLI command an operator runs
    to rebuild the failed component (informational; nothing executes
    it automatically).
    """

    component: str
    consecutive_failures: int
    last_error: str
    manual_recovery_hint: str
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryFallbackToolUsedEvent(TypedDict):
    """Agent invoked a non-memory tool (grep / list_dir / read_file /
    etc.) while a memory-enabled workspace was active.

    The agent's fallback is rational — try the curated tool, fall back
    to raw search — but a high rate masks a memory_search recall gap.
    This event lets dashboards quantify the fallback rate longitudinally
    without re-instrumenting per-tool.

    ``is_bench_relevant`` is True when the tool is one of the
    filesystem-scanning fallbacks (grep / list_dir / read_file /
    edit_file / exec / write_file). Other non-memory tools (web_*,
    message, etc.) emit too but with the flag False so analysis
    can filter.
    """

    tool_name: str
    is_bench_relevant: bool
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryHotLayerFailureEvent(TypedDict):
    """Hot-layer assembly failed for one component.

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


class MemoryDreamSkillExtractEvent(TypedDict):
    """The extract dream's skill pass wrote/updated procedural skills."""

    skills_touched: int
    duration_ms: NotRequired[int]


class MemoryDreamSkillSignalsEvent(TypedDict):
    """The extract dream's skill-signal stage (stage 3) logged correction/gap
    observations from a session's turns in hindsight. ``proposed`` is what the
    LLM returned; ``logged`` is what was written to the observation queue. Lets
    dashboards measure skill-signal precision over time."""

    proposed: int
    logged: int
    skills: NotRequired[list[str]]


class MemoryDreamMaxSecondsReachedEvent(TypedDict):
    """An extract pass hit ``memory.dream.max_seconds_per_run`` and yielded;
    the per-session cursor resumes the remainder on the next trigger."""

    kind: str  # "extract"
    max_seconds: int
    elapsed_ms: int
    sessions_done: int


class MemoryDreamThrottledEvent(TypedDict):
    """A reactive dream trigger (post_compaction / session_close) was skipped
    by the in-process gate — ``reason`` is ``"locked"`` (a pass was already
    running) or ``"throttled"`` (one ran within min_seconds_between_runs)."""

    trigger: str
    reason: str


class MemoryDreamAlwaysOnEvent(TypedDict):
    """The always_on distillation pass curated the pinned guidance set.

    ``selected`` items are kept always_on (fit the token budget); ``pruned``
    were ranked but didn't fit; ``dropped`` were removed by the contradiction
    judge; ``tokens`` is the budget consumed by the selected set.
    """

    selected: int
    pruned: int
    dropped: int
    tokens: int
    duration_ms: int


class MemoryDreamFlaggedEvent(TypedDict):
    """The refine dream flagged a pair the Tier-2 agent investigated but did not
    confirm as the same entity, or a borderline pair capped before Tier-2 ever
    ran (see ``memory.absorb.escalation_capped``) and kept on the cheap
    Tier-1 verdict instead.  ``canonical`` and ``absorbed`` are the two refs
    the judge examined; the pair is stored in ``.flagged_pairs.json`` for future
    review.  Fires inside ``add_flagged`` so it is always consistent with the
    on-disk record."""

    canonical: str
    absorbed: str


class MemoryDreamParseFailureEvent(TypedDict):
    """A dream-pass LLM response could not be parsed at all.

    Distinct from a valid-but-empty response: this fires only when the
    output is unloadable JSON or the wrong top-level type after fence
    stripping and repair. Without it, "model returned garbage" is
    indistinguishable from "nothing to extract" — the pass silently
    yields nothing and the per-session cursor still advances.
    """

    stage: str  # "extract" | "discover" | "learnings" | "derived_from" | "curation" | "suggestions"
    source: NotRequired[str | None]  # entity ref or session stem
    raw_head: str  # first 200 chars of the raw response
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class AuxInvokeFailureEvent(TypedDict):
    """A purpose-resolved auxiliary LLM invoke raised.

    The aux consumers (dream passes, skill security judge, composition gate)
    are failure-open by design — a broken judge or dream model degrades
    gracefully per call. This event is what keeps that degradation visible:
    it names the (provider, model) pair that failed so a misconfigured
    specific-model knob surfaces instead of failing silently forever
    (pairs with `durin doctor`'s "specific models" check).
    """

    purpose: str  # "memory" | "judge"
    provider: NotRequired[str | None]
    model: NotRequired[str | None]
    error_head: str  # first 200 chars of the raised error


class MemoryDreamVectorUnavailableEvent(TypedDict):
    """A dream run started with vector memory enabled but no vector backend.

    Emitted once per run by ``dream_vector_index`` when lancedb is not
    importable while ``memory.enabled`` is true: semantic dedup (refine +
    discovery) degrades to alias matching for that run. Without this event,
    "no duplicates found" and "no vectors to find them with" are
    indistinguishable. A deliberate ``memory.enabled = false`` stays silent.
    """

    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class MemoryUpsertEntityEvent(TypedDict):
    """memory_upsert_entity tool write (entity page authored/extended)."""

    ref: str
    committed: bool
    retries: int


# ===========================================================================
# Skill loop (use / observe / curate / suggest)
# ===========================================================================


class SkillUsedEvent(TypedDict):
    """The agent touched a skill during a turn (view/read/edit).

    Emitted once per entry in ``extract_skill_calls`` output, right after
    ``_state_save`` records it into ``session.metadata["skill_calls"]``.
    """

    skill: str
    op: str  # "view" | "read" | "edit"
    turn: int
    iteration: NotRequired[int]
    session_key: NotRequired[str | None]


class SkillObservationLoggedEvent(TypedDict):
    """A live skill observation was appended, or bumped an existing OPEN
    record (dedup). ``count`` is the record's count after this call."""

    skill: str
    kind: str
    dedup_bumped: bool
    count: int


class SkillCurationActionEvent(TypedDict):
    """One curation action was applied (or attempted) during a curation run."""

    action: str  # "evolve" | "fuse" | "retire" | "principle" | "retire_principle" | "backfill"
    skill: NotRequired[str]
    applied: bool


class SkillCurationRunEvent(TypedDict):
    """One curation pass completed — summary counts.

    ``deferred > 0`` means the day's delta exceeded ``budget``; the rest
    carries over to a later run (visible throttle signal).
    """

    reviewed: int
    applied: int
    deferred: int
    backfilled: NotRequired[int]


class SkillSuggestionResolvedEvent(TypedDict):
    """A manual-skill curation suggestion was accepted or rejected by the user."""

    skill: str
    action: str
    resolution: str  # "accepted" | "rejected"


class WorkflowImproveRecommendedEvent(TypedDict):
    """The improve pass queued a prompt edit for a MANUAL-mode workflow."""

    workflow: str
    target_id: str
    rec_id: str
    reason: NotRequired[str]
    runs: int  # terminal runs whose diagnostic motivated the proposal


class WorkflowImproveAppliedEvent(TypedDict):
    """The improve pass APPLIED a prompt edit to an AUTO-mode workflow.

    The edit is versioned (actor=dream) and held pending validation: the
    workflow's next terminal runs decide whether it sticks or auto-reverts.
    """

    workflow: str
    target_id: str
    rec_id: str
    reason: NotRequired[str]
    runs: int


class WorkflowImproveRevertedEvent(TypedDict):
    """An auto-applied edit worsened its node's diagnostic and was reverted.

    Without this event an auto-revert is invisible — the definition silently
    returns to its previous text and only git shows why.
    """

    workflow: str
    target_id: str
    rec_id: str
    baseline_rate: float
    new_rate: float


class WorkflowImproveStructuralEvent(TypedDict):
    """The model proposed an edit OUTSIDE the prompt-only scope.

    Never applied in any mode — it lands annotated in the recommendations
    queue for the user to treat deliberately. This event is the visibility
    that a structural idea exists and awaits review.
    """

    workflow: str
    rec_id: str
    why_rejected: str
    runs: int


# ===========================================================================
# Catalog — single source of truth
# ===========================================================================


EVENTS: dict[str, type] = {
    # Loop control
    "circuit_breaker.idle_timeout": CircuitBreakerIdleTimeoutEvent,
    "mid_turn_precheck.overflow": MidTurnPrecheckOverflowEvent,
    "mid_turn_precheck.recovered": MidTurnPrecheckRecoveredEvent,
    "overflow_retry.forced_consolidation": OverflowRetryForcedConsolidationEvent,
    "turn.latency": TurnLatencyEvent,
    "unknown_tool.loop_guard": UnknownToolLoopGuardEvent,
    "post_compaction_loop.tripped": PostCompactionLoopTrippedEvent,
    "turn_budget.enforced": TurnBudgetEnforcedEvent,
    "tools.parallelism": ToolsParallelismEvent,
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
    "turn.memory_usage": TurnMemoryUsageEvent,
    "history_media.pruned": HistoryMediaPrunedEvent,
    # Agent mode
    "agent_mode.turn_start": AgentModeTurnStartEvent,
    "agent_mode.switch": AgentModeSwitchEvent,
    "agent_mode.tool_denied": AgentModeToolDeniedEvent,
    "plan_mode.presented": PlanModePresentedEvent,
    # Tool-level instrumentation
    "tool.read_file": ToolReadFileEvent,
    "tool.edit_file": ToolEditFileEvent,
    "tool.grep": ToolGrepEvent,
    "tool.exec.spill": ToolExecSpillEvent,
    "tool.post_edit_check": PostEditCheckEvent,
    "tool.execute_code": ExecuteCodeEvent,
    "tool.repo_overview": ToolRepoOverviewEvent,
    "tool.list_dir": ToolListDirEvent,
    "tool.web_search": ToolWebSearchEvent,
    "tool.web_fetch": ToolWebFetchEvent,
    "tool.todo_write": ToolTodoWriteEvent,
    "tool.note_decision": ToolNoteDecisionEvent,
    "decision_log.capped": DecisionLogCappedEvent,
    "decision_log.extracted": DecisionLogExtractedEvent,
    "ask_user.question_asked": AskUserQuestionAskedEvent,
    "ask_user.answer_received": AskUserAnswerReceivedEvent,
    "ask_user.answer_timeout": AskUserAnswerTimeoutEvent,
    "ask_vision.start": AskVisionStartEvent,
    "ask_vision.error": AskVisionErrorEvent,
    "ask_vision.end": AskVisionEndEvent,
    "ask_audio.start": AskAudioStartEvent,
    "ask_audio.error": AskAudioErrorEvent,
    "ask_audio.end": AskAudioEndEvent,
    "sleep.start": SleepStartEvent,
    "sleep.cancelled": SleepCancelledEvent,
    "sleep.end": SleepEndEvent,
    # Background processes (exec background=true / process tool)
    "process.spawn": ProcessSpawnEvent,
    "process.exit": ProcessExitEvent,
    "process.kill": ProcessKillEvent,
    # Memory subsystem (Phase 1)
    "memory.recall": MemoryRecallEvent,
    "memory.store": MemoryStoreEvent,
    "memory.ingest": MemoryIngestEvent,
    "memory.forget": MemoryForgetEvent,
    "memory.upsert_entity": MemoryUpsertEntityEvent,
    "memory.entity_relation_cap_warned": MemoryEntityRelationCapWarnedEvent,
    "memory.entity_relation_cap_rejected": MemoryEntityRelationCapRejectedEvent,
    # Memory subsystem (Phase 2 — embedding)
    "memory.embedding.load": MemoryEmbeddingLoadEvent,
    "memory.embedding.embed": MemoryEmbeddingEmbedEvent,
    "memory.recall.vector": MemoryRecallVectorEvent,
    "memory.store.blocked_near_duplicate": MemoryStoreBlockedNearDuplicateEvent,
    "memory.dream.start": MemoryDreamStartEvent,
    "memory.dream.end": MemoryDreamEndEvent,
    "memory.dream.patch_applied": MemoryDreamPatchAppliedEvent,
    "memory.dream.discover": MemoryDreamDiscoverEvent,
    "memory.dream.learnings": MemoryDreamLearningsEvent,
    "memory.dream.run_summary": MemoryDreamRunSummaryEvent,
    "memory.dream.skill_extract": MemoryDreamSkillExtractEvent,
    "memory.dream.skill_signals": MemoryDreamSkillSignalsEvent,
    "memory.dream.max_seconds_reached": MemoryDreamMaxSecondsReachedEvent,
    "memory.dream.throttled": MemoryDreamThrottledEvent,
    "memory.dream.always_on": MemoryDreamAlwaysOnEvent,
    "memory.dream.flagged": MemoryDreamFlaggedEvent,
    "memory.dream.parse_failure": MemoryDreamParseFailureEvent,
    "aux.invoke_failure": AuxInvokeFailureEvent,
    "memory.dream.vector_unavailable": MemoryDreamVectorUnavailableEvent,
    "memory.absorb.judged": MemoryAbsorbJudgedEvent,
    "memory.absorb.auto_merged": MemoryAbsorbAutoMergedEvent,
    "memory.absorb.skipped": MemoryAbsorbSkippedEvent,
    "memory.absorb.reverted": MemoryAbsorbRevertedEvent,
    "memory.absorb.escalated": MemoryAbsorbEscalatedEvent,
    "memory.absorb.escalation_capped": MemoryAbsorbEscalationCappedEvent,
    "memory.hot_layer.failure": MemoryHotLayerFailureEvent,
    "memory.index.write": MemoryIndexWriteEvent,
    "memory.index.rebuild": MemoryIndexRebuildEvent,
    "memory.index.staleness_detected": MemoryIndexStalenessDetectedEvent,
    "memory.recall.lexical": MemoryRecallLexicalEvent,
    "memory.recall.rrf": MemoryRecallRRFEvent,
    "memory.recall.grep_verify": MemoryRecallGrepVerifyEvent,
    "memory.recall.rerank": MemoryRecallRerankEvent,
    "memory.search.failure": MemoryRecallFailureEvent,
    "memory.skill_miss": MemorySkillMissEvent,
    "memory.health_check": MemoryHealthCheckEvent,
    "memory.health.critical": MemoryHealthCriticalEvent,
    "memory.fallback_tool_used": MemoryFallbackToolUsedEvent,
    # Skill loop (use / observe / curate / suggest)
    "skill.used": SkillUsedEvent,
    "skill.observation_logged": SkillObservationLoggedEvent,
    "skill.curation_action": SkillCurationActionEvent,
    "workflow.improve.recommended": WorkflowImproveRecommendedEvent,
    "workflow.improve.applied": WorkflowImproveAppliedEvent,
    "workflow.improve.reverted": WorkflowImproveRevertedEvent,
    "workflow.improve.structural": WorkflowImproveStructuralEvent,
    "skill.curation_run": SkillCurationRunEvent,
    "skill.suggestion_resolved": SkillSuggestionResolvedEvent,
}


__all__ = [
    "EVENTS",
    # Loop control
    "CircuitBreakerIdleTimeoutEvent",
    "MidTurnPrecheckOverflowEvent",
    "MidTurnPrecheckRecoveredEvent",
    "OverflowRetryForcedConsolidationEvent",
    "TurnLatencyEvent",
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
    "PostEditCheckEvent",
    "ExecuteCodeEvent",
    "ProcessSpawnEvent",
    "ProcessExitEvent",
    "ProcessKillEvent",
    "ToolRepoOverviewEvent",
    "ToolListDirEvent",
    "ToolWebSearchEvent",
    "ToolWebFetchEvent",
    "ToolTodoWriteEvent",
    "ToolNoteDecisionEvent",
    "DecisionLogCappedEvent",
    "DecisionLogExtractedEvent",
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
    "MemoryDreamDiscoverEvent",
    "MemoryDreamLearningsEvent",
    "MemoryDreamFlaggedEvent",
    "MemoryEntityRelationCapWarnedEvent",
    "MemoryEntityRelationCapRejectedEvent",
    "MemoryHealthCheckEvent",
    "MemoryFallbackToolUsedEvent",
    "MemoryHealthCriticalEvent",
    "MemoryHotLayerFailureEvent",
    "MemoryRecallLexicalEvent",
    "MemoryRecallRRFEvent",
    "MemoryRecallRerankEvent",
    "MemoryRecallFailureEvent",
    "MemorySkillMissEvent",
    "MemoryIndexWriteEvent",
    "MemoryIndexRebuildEvent",
    "MemoryIndexStalenessDetectedEvent",
    # Skill loop
    "SkillUsedEvent",
    "SkillObservationLoggedEvent",
    "SkillCurationActionEvent",
    "SkillCurationRunEvent",
    "SkillSuggestionResolvedEvent",
]
