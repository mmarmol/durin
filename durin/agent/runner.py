"""Shared execution loop for tool-using agents."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from durin.agent.hook import AgentHook, AgentHookContext
from durin.agent.tools.registry import ToolRegistry
from durin.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from durin.telemetry.logger import current_telemetry
from durin.utils.helpers import (
    IncrementalThinkExtractor,
    build_assistant_message,
    estimate_message_tokens,
    estimate_prompt_tokens_chain,
    extract_reasoning,
    find_legal_message_start,
    maybe_persist_tool_result,
    parse_persisted_reference,
    strip_think,
    truncate_text,
)
from durin.utils.history_image_prune import prune_processed_history_images
from durin.utils.prompt_templates import render_template
from durin.utils.runtime import (
    EMPTY_FINAL_RESPONSE_MESSAGE,
    build_finalization_retry_message,
    build_length_recovery_message,
    build_reasoning_truncation_message,
    ensure_nonempty_tool_result,
    is_blank_text,
    repeated_external_lookup_error,
    repeated_workspace_violation_error,
)
from durin.utils.tool_result_validation import validate_tool_result_blocks

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"
_PERSISTED_OVERFLOW_PLACEHOLDER = (
    "[Turn skipped before the model ran: the prompt exceeded the input budget "
    "even after emergency trimming. The next turn retries with a compacted context.]"
)
_MAX_EMPTY_RETRIES = 2
_MAX_LENGTH_RECOVERIES = 3
_MAX_INJECTIONS_PER_TURN = 3
_MAX_INJECTION_CYCLES = 5
_SNIP_SAFETY_BUFFER = 1024
# Cap on how much output headroom is *reserved* from the input budget. A
# model's configured ``max_tokens`` is a ceiling on what a turn MAY emit, not
# what every turn does emit; reserving the full ceiling (e.g. 128k on a 202k
# window) collapses the usable input budget. We reserve at most this many
# tokens for output when sizing the input, then send a dynamic ``max_tokens``
# that fills whatever room the prompt actually leaves (up to the ceiling).
_MAX_OUTPUT_RESERVATION = 32_768
# Emergency-trim floor: an oversized tool result is shrunk no smaller than
# this many characters (plus a marker) before we give up on a turn.
_EMERGENCY_TRIM_FLOOR_CHARS = 800
_EMERGENCY_TRIM_MARKER = "\n…[truncated: context budget]"
_EMERGENCY_TRIM_MAX_PASSES = 64
_MICROCOMPACT_KEEP_RECENT = 10
_MICROCOMPACT_MIN_CHARS = 500
# Microcompaction only pays for itself when the prompt is actually
# crowding the window; below this fraction of the input budget the
# model keeps full tool results (they may hold answers to questions
# the user hasn't asked yet).
_MICROCOMPACT_PRESSURE_RATIO = 0.5
_MICROCOMPACT_HEAD_CHARS = 120

def _output_reservation(max_output: int) -> int:
    """Tokens to hold back for output when sizing the input budget.

    Capped at ``_MAX_OUTPUT_RESERVATION`` so a large configured output
    ceiling does not collapse the usable input budget. Never below 1.
    """
    try:
        ceiling = int(max_output)
    except (TypeError, ValueError):
        ceiling = 4096
    return min(max(1, ceiling), _MAX_OUTPUT_RESERVATION)


# Idle-timeout circuit breaker: stops execution after consecutive
# tool-call timeouts.
#
# Provider-level retries already absorb individual timeouts. But the runner
# can still loop on consecutive timeout responses across iterations when
# user injections keep continuing the run after each failure — burning
# tokens on a clearly-stalled provider. The breaker opens after this many
# consecutive iterations end in an idle/wall-clock timeout WITHOUT any
# forward progress (content or tool_calls) in between, terminating the run
# with a distinct stop_reason so callers can distinguish from generic errors.
#
# Default 1: tolerate one timeout, trip on the second. Override with
# DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS.
_DEFAULT_MAX_CONSECUTIVE_IDLE_TIMEOUTS = 1


def _max_consecutive_idle_timeouts() -> int:
    raw = os.getenv("DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS")
    if raw is None:
        return _DEFAULT_MAX_CONSECUTIVE_IDLE_TIMEOUTS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_CONSECUTIVE_IDLE_TIMEOUTS
    return max(0, value)


# Unknown-tool loop guard: stops execution when the model repeatedly
# calls tools that do not exist.
#
# Hash-based loop detection blocks repeats of the exact same
# ``(tool_name, arguments)`` pair after a known failure. But a hallucinated
# tool name (model invents ``search_web`` when the real tool is
# ``web_search``) often comes with DIFFERENT args each iteration as the
# model retries with variations, so that detection doesn't catch it. This
# counter tracks calls to unknown names per-turn; after the threshold, the
# turn terminates with a distinct stop_reason rather than burning more
# iterations on a name that will never resolve.
#
# Default 2 → third consecutive call to the same unknown name trips.
# Counter doesn't reset within a turn — if the model has tried this
# name twice and it's wrong, the third try wastes tokens.
_DEFAULT_MAX_UNKNOWN_TOOL_ATTEMPTS = 2


def _max_unknown_tool_attempts() -> int:
    raw = os.getenv("DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS")
    if raw is None:
        return _DEFAULT_MAX_UNKNOWN_TOOL_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_UNKNOWN_TOOL_ATTEMPTS
    return max(0, value)


# Compaction grace window: when context consolidation is in flight,
# extend the LLM timeout by this duration to allow compaction to complete.
#
# When the outer LLM wall-clock timeout fires while consolidation is in
# flight for this session, extending the deadline by ``DURIN_COMPACTION_GRACE_S``
# avoids killing the request just because compaction is rebuilding the
# context (typically a slow LLM call). Grace is applied at most once per
# request; subsequent timeouts use the regular deadline.
_DEFAULT_COMPACTION_GRACE_SECONDS = 30.0


def _compaction_grace_seconds() -> float:
    raw = os.getenv("DURIN_COMPACTION_GRACE_S")
    if raw is None:
        return _DEFAULT_COMPACTION_GRACE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_COMPACTION_GRACE_SECONDS
    return max(0.0, value)


# Per-turn aggregate tool-result budget.
#
# ``max_tool_result_chars`` already caps each individual tool result; large
# outputs spill to disk via ``maybe_persist_tool_result``. But when an LLM
# emits N parallel tool calls each returning <max_chars, the aggregate can
# still overflow the context window. After all tool results are collected
# in a turn, if the sum exceeds this budget, we spill the largest
# not-yet-persisted results to disk in priority order until the aggregate
# is under budget.
_DEFAULT_TURN_BUDGET_CHARS = 200_000
_PERSISTED_MARKER = "[tool output persisted]"


def _turn_budget_chars() -> int:
    raw = os.getenv("DURIN_TURN_BUDGET_CHARS")
    if raw is None:
        return _DEFAULT_TURN_BUDGET_CHARS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TURN_BUDGET_CHARS
    # 0 disables the budget; negative values clamp to 0.
    return max(0, value)
_COMPACTABLE_TOOLS = frozenset({
    "read_file", "exec", "grep",
    "web_search", "web_fetch", "list_dir",
})
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"


# 1A — Loop-detection plumbing.
#
# Frontier-reasoning models occasionally fixate: they emit the SAME tool call
# with the SAME arguments multiple turns in a row even after that exact call
# already produced a hard failure (lookup error, exception, "Error: ..." string).
# The model "sees" the failure in the message history but anchors on its plan.
#
# The fix is purely state tracking — not cognitive manipulation. We keep a
# turn-scoped set of (tool_name, normalized_args) signatures that previously
# failed. On a repeat hit we short-circuit and inject a synthetic result asking
# the model to take a strictly different approach. This forces the agent out of
# the local minimum WITHOUT spending another tool execution.
#
# Reset per turn (new run() call) — we never block calls across turns because
# the environment state may have changed (e.g., a file the model edited will
# now behave differently for a repeated read).

def _tool_call_signature(name: str, arguments: Any) -> str:
    """Stable signature for (tool_name, arguments).

    JSON dump with sort_keys=True so dict order doesn't produce false negatives.
    Falls back to repr() if arguments are not JSON-serializable (rare).
    """
    try:
        payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        payload = repr(arguments)
    digest = hashlib.sha256(f"{name}\x1f{payload}".encode("utf-8", "replace")).hexdigest()
    return digest[:16]  # 64 bits of collision space is plenty per-turn


_LOOP_BLOCK_MESSAGE = (
    "BLOCKED: this exact tool call (same name and arguments) already failed "
    "earlier in this turn. Repeating it will not change the outcome — the "
    "environment state for these arguments has not changed. Take a strictly "
    "different approach: either modify the arguments, choose a different tool, "
    "or reconsider whether this tool is the right one for the task."
)



@dataclass(slots=True)
class AgentRunSpec:
    """Configuration for a single agent execution."""

    initial_messages: list[dict[str, Any]]
    tools: ToolRegistry
    model: str
    max_iterations: int
    max_tool_result_chars: int
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    # Optional sampling params (None = don't send). top_p is standard; top_k /
    # repeat_penalty are non-standard and routed into the request's extra_body.
    top_p: float | None = None
    top_k: int | None = None
    repeat_penalty: float | None = None
    hook: AgentHook | None = None
    error_message: str | None = _DEFAULT_ERROR_MESSAGE
    max_iterations_message: str | None = None
    concurrent_tools: bool = False
    fail_on_tool_error: bool = False
    workspace: Path | None = None
    session_key: str | None = None
    context_window_tokens: int | None = None
    context_block_limit: int | None = None
    provider_retry_mode: str = "standard"
    progress_callback: Any | None = None
    stream_progress_deltas: bool = True
    retry_wait_callback: Any | None = None
    checkpoint_callback: Any | None = None
    injection_callback: Any | None = None
    llm_timeout_s: float | None = None
    # Permission-as-data agent modes. When provided, the runner calls this
    # each iteration to obtain the active mode and filters the tool
    # definitions sent to the LLM. Returns None → no filtering (equivalent
    # to BUILD_MODE = full access). See durin/agent/agent_mode.py.
    mode_provider: Any | None = None
    # Inspired by pi's ``transformContext``: an optional callback that
    # receives the full message list right before it is sent to the
    # provider and returns the list to actually use. Lets callers do
    # token-budget pruning, late-stage system reminders, or filtering
    # without re-architecting the loop. Called once per LLM request,
    # not once per agent.run(); receives a *copy* of the list so the
    # callback can mutate freely. If it returns ``None`` or raises,
    # the untransformed list is used (best-effort, never breaks the
    # loop).
    context_transform: Any | None = None  # Callable[[list[dict]], list[dict] | None]
    # Compaction grace window. Optional callable that returns True iff context
    # consolidation is currently running for the session backing this run.
    # When the outer wall-clock LLM timeout would have fired, the runner
    # extends the deadline once by ``DURIN_COMPACTION_GRACE_S`` seconds if
    # this returns True — protecting slow LLM calls during context reshaping.
    # Grace is used at most once per request; subsequent timeouts fail with
    # the regular timeout response.
    is_compacting: Any | None = None  # Callable[[], bool]
    # Optional shared ``PostCompactionLoopGuard`` instance. The consolidator
    # arms it per-session after a successful compaction; the runner
    # ``observe()``s every tool execution within the window. When the guard
    # trips, the turn terminates with ``stop_reason="post_compaction_loop"``.
    # Leave as ``None`` to skip this layer (tests / non-loop callers don't
    # need it).
    post_compaction_guard: Any | None = None
    # Per-turn provider snapshot. The gateway runs a single shared AgentRunner;
    # _apply_provider_snapshot in loop.py mutates self.provider on every
    # concurrent session's /model swap. Carrying the provider here lets run()
    # resolve it once and use that local reference for the entire turn, making
    # the turn immune to concurrent swaps. None → fall back to self.provider
    # for backward compatibility.
    provider: LLMProvider | None = None


@dataclass(slots=True)
class AgentRunResult:
    """Outcome of a shared agent execution."""

    final_content: str | None
    messages: list[dict[str, Any]]
    tools_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "completed"
    error: str | None = None
    tool_events: list[dict[str, str]] = field(default_factory=list)
    had_injections: bool = False
    # Total wall-clock spent in provider/model round-trips across all
    # iterations of this run. The loop subtracts this (and tool durations)
    # from the turn wall-clock to attribute "local processing" time.
    llm_ms: float = 0.0


class AgentRunner:
    """Run a tool-capable LLM loop without product-layer concerns."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """Append injected user messages while preserving role alternation."""
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
            ):
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
        steer_only: bool = False,
    ) -> tuple[bool, int]:
        """Drain pending injections. Returns (should_continue, updated_cycles).

        ``steer_only=True`` marks a mid-turn checkpoint: only explicit steers
        and system-origin results are drained; plain user messages stay queued
        until the turn's final response so they don't derail work in flight.

        If injections are found and we haven't exceeded _MAX_INJECTION_CYCLES,
        append them to *messages* (and emit a checkpoint if *assistant_message*
        and *iteration* are both provided) and return (True, cycles+1) so the
        caller continues the iteration loop.  Otherwise return (False, cycles).
        """
        if injection_cycles >= _MAX_INJECTION_CYCLES:
            return False, injection_cycles
        injections = await self._drain_injections(spec, steer_only=steer_only)
        if not injections:
            return False, injection_cycles
        injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        logger.info(
            "Injected {} follow-up message(s) {} ({}/{})",
            len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
        )
        return True, injection_cycles

    async def _drain_injections(
        self, spec: AgentRunSpec, *, steer_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Drain pending user messages via the injection callback.

        Returns normalized user messages (capped by
        ``_MAX_INJECTIONS_PER_TURN``), or an empty list when there is
        nothing to inject. Messages beyond the cap are logged so they
        are not silently lost. ``steer_only`` is forwarded to callbacks
        that support it (mid-turn checkpoints drain only steers and
        system results; a callback without the parameter keeps the legacy
        inject-everything behavior).
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            parameters = signature.parameters
            has_var_keyword = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            kwargs: dict[str, Any] = {}
            if "limit" in parameters or has_var_keyword:
                kwargs["limit"] = _MAX_INJECTIONS_PER_TURN
            if "steer_only" in parameters or has_var_keyword:
                kwargs["steer_only"] = steer_only
            items = await spec.injection_callback(**kwargs)
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                injected_messages.append(item)
                continue
            text = getattr(item, "content", str(item))
            if text.strip():
                injected_messages.append({"role": "user", "content": text})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        # Resolve the per-turn provider snapshot once. A concurrent session's
        # /model swap mutates self.provider; pinning it here makes this turn
        # immune to that mutation.
        provider = spec.provider or self.provider
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}
        empty_content_retries = 0
        length_recovery_count = 0
        had_injections = False
        injection_cycles = 0

        # Idle-timeout circuit breaker state.
        # Increments on every iteration whose response is an idle/wall-clock
        # timeout error; resets on any iteration that produced forward
        # progress (tool_calls or non-empty content). When it exceeds the
        # configured threshold the loop terminates with
        # ``stop_reason="circuit_breaker_idle_timeout"``.
        consecutive_idle_timeouts = 0
        max_idle_timeouts = _max_consecutive_idle_timeouts()
        # Accumulates provider/model round-trip wall-clock across iterations.
        total_llm_ms = 0.0

        # 1A — Loop-detection state. Tracks signatures of tool calls that already
        # failed in this turn. We block repeats to break model fixation. Scope is
        # the current turn only (NOT cross-turn) — re-entry to the loop in a new
        # turn starts fresh because environment state may have changed.
        seen_failed_calls: set[str] = set()

        # Unknown-tool loop guard. Counter per hallucinated tool name
        # across this turn. Trips when any name's count exceeds
        # ``max_unknown_tool_attempts``.
        unknown_tool_attempts: dict[str, int] = {}
        max_unknown_tool_attempts = _max_unknown_tool_attempts()

        # Record the mode active at the start of this turn so we can
        # correlate behavior + outcomes with mode. Mid-run switches are
        # captured separately via `agent_mode.switch` telemetry from
        # tools/CLI.
        if spec.mode_provider is not None:
            try:
                _start_mode = spec.mode_provider()
            except Exception:
                _start_mode = None
            if _start_mode is not None:
                _start_logger = current_telemetry()
                if _start_logger is not None:
                    with suppress(Exception):
                        _start_logger.log("agent_mode.turn_start", {
                            "mode": _start_mode.name,
                        })

        for iteration in range(spec.max_iterations):
            try:
                # Keep the persisted conversation untouched. Context governance
                # may repair or compact historical messages for the model, but
                # those synthetic edits must not shift the append boundary used
                # later when the caller saves only the new turn.
                messages_for_model = self._drop_orphan_tool_results(messages)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                # Prune images/audio from completed turns older than the
                # preservation window so accumulated media doesn't ride
                # along forever. Runs BEFORE microcompact / snip so those
                # steps see the reduced size. Stats are collected via an
                # out-dict so we can emit telemetry only when the pruner
                # actually removed something.
                _prune_stats: dict[str, int] = {}
                messages_for_model = prune_processed_history_images(
                    messages_for_model, stats=_prune_stats,
                )
                if _prune_stats.get("image_blocks_removed", 0) > 0 or _prune_stats.get("audio_blocks_removed", 0) > 0:
                    _prune_logger = current_telemetry()
                    if _prune_logger is not None:
                        with suppress(Exception):
                            _prune_logger.log("history_media.pruned", {
                                "image_blocks_removed": _prune_stats.get("image_blocks_removed", 0),
                                "audio_blocks_removed": _prune_stats.get("audio_blocks_removed", 0),
                                "preserve_turns": _prune_stats.get("preserve_turns", 0),
                                "iteration": iteration,
                                "session_key": spec.session_key,
                            })
                messages_for_model = self._microcompact(spec, messages_for_model, provider)
                messages_for_model = self._apply_tool_result_budget(spec, messages_for_model)
                messages_for_model = self._snip_history(spec, messages_for_model, provider)
                # Snipping may have created new orphans; clean them up.
                messages_for_model = self._drop_orphan_tool_results(messages_for_model)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
            except Exception:
                logger.exception(
                    "Context governance failed on turn {} for {}; applying minimal repair",
                    iteration,
                    spec.session_key or "default",
                )
                try:
                    messages_for_model = self._drop_orphan_tool_results(messages)
                    messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                except Exception:
                    messages_for_model = messages

            # Mid-turn precheck (OpenClaw-inspired Tier 2 A2). After the
            # sanitize pipeline ran, estimate whether the prompt we're about
            # to send still fits. ``_snip_history`` trims from the head but
            # ``find_legal_message_start`` may force-keep messages that still
            # exceed the budget; a single oversized tool result late in the
            # conversation can survive snipping. The reused estimate also
            # sizes the dynamic output budget below, so a high ``max_tokens``
            # ceiling never starves input.
            effective_max_tokens: int | None = None
            budget_decision = self._estimate_and_budget(spec, messages_for_model, provider)
            if budget_decision is not None:
                estimate_tokens, budget_tokens = budget_decision
                if estimate_tokens > budget_tokens:
                    # B: emergency-trim oversized tool results on the
                    # model-facing copy and proceed when that brings the
                    # prompt under budget — instead of silently dropping the
                    # in-flight request. The canonical history is untouched.
                    trimmed, fit, trimmed_estimate = self._emergency_trim_for_budget(
                        spec, messages_for_model, budget_tokens, provider,
                    )
                    if fit:
                        messages_for_model = trimmed
                        estimate_tokens = trimmed_estimate if trimmed_estimate is not None else budget_tokens
                        _rec_logger = current_telemetry()
                        if _rec_logger is not None:
                            with suppress(Exception):
                                _rec_logger.log("mid_turn_precheck.recovered", {
                                    "iteration": iteration,
                                    "session_key": spec.session_key,
                                    "estimated_tokens": estimate_tokens,
                                    "budget_tokens": budget_tokens,
                                })
                    else:
                        # Genuinely unrecoverable: abort before the LLM call
                        # with a distinct stop_reason and an overflow-specific
                        # placeholder (NOT "model error"). A1 re-bases the
                        # context for the next turn.
                        final_content = (
                            "Error: prompt overflow before LLM call "
                            f"(estimated {estimate_tokens} tokens, budget {budget_tokens}). "
                            "The next turn will retry with a freshly-compacted context."
                        )
                        stop_reason = "mid_turn_precheck_overflow"
                        error = final_content
                        self._append_overflow_placeholder(messages)
                        context = AgentHookContext(iteration=iteration, messages=messages)
                        context.final_content = final_content
                        context.error = error
                        context.stop_reason = stop_reason
                        _mt_logger = current_telemetry()
                        if _mt_logger is not None:
                            with suppress(Exception):
                                _mt_logger.log("mid_turn_precheck.overflow", {
                                    "iteration": iteration,
                                    "session_key": spec.session_key,
                                    "estimated_tokens": estimate_tokens,
                                    "budget_tokens": budget_tokens,
                                })
                        logger.warning(
                            "Mid-turn precheck overflow on turn {} for {}: "
                            "estimated={} tokens, budget={} tokens",
                            iteration,
                            spec.session_key or "default",
                            estimate_tokens,
                            budget_tokens,
                        )
                        await hook.after_iteration(context)
                        break
                effective_max_tokens = self._effective_max_tokens(spec, estimate_tokens, provider)

            context = AgentHookContext(
                iteration=iteration,
                messages=messages,
            )
            await hook.before_iteration(context)
            _llm_started = time.monotonic()
            response = await self._request_model(
                spec, messages_for_model, hook, context, provider,
                max_tokens_override=effective_max_tokens,
            )
            total_llm_ms += (time.monotonic() - _llm_started) * 1000.0
            raw_usage = self._usage_dict(response.usage)
            context.response = response
            context.usage = dict(raw_usage)
            context.tool_calls = list(response.tool_calls)
            self._accumulate_usage(usage, raw_usage)

            # Idle-timeout circuit breaker: track consecutive timeout responses
            # across iterations. The provider already retries internally on
            # transient timeouts; a timeout reaching the runner means those
            # retries were exhausted. Tolerating one such event is reasonable
            # (next iteration may succeed after an injection or context repair)
            # but multiple in a row burn tokens against a stalled endpoint.
            is_idle_timeout_response = (
                response.finish_reason == "error"
                and (response.error_kind or "").lower() == "timeout"
            )
            if is_idle_timeout_response:
                consecutive_idle_timeouts += 1
                if consecutive_idle_timeouts > max_idle_timeouts:
                    final_content = (
                        f"Error: LLM stalled — {consecutive_idle_timeouts} "
                        f"consecutive idle timeouts (threshold {max_idle_timeouts})."
                    )
                    stop_reason = "circuit_breaker_idle_timeout"
                    error = final_content
                    self._append_model_error_placeholder(messages)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    _cb_logger = current_telemetry()
                    if _cb_logger is not None:
                        with suppress(Exception):
                            _cb_logger.log("circuit_breaker.idle_timeout", {
                                "consecutive_timeouts": consecutive_idle_timeouts,
                                "threshold": max_idle_timeouts,
                                "iteration": iteration,
                                "session_key": spec.session_key,
                            })
                    logger.warning(
                        "Idle-timeout circuit breaker opened on turn {} for {} "
                        "({} consecutive timeouts, threshold {})",
                        iteration,
                        spec.session_key or "default",
                        consecutive_idle_timeouts,
                        max_idle_timeouts,
                    )
                    await hook.after_iteration(context)
                    break
            elif response.has_tool_calls or not is_blank_text(response.content):
                consecutive_idle_timeouts = 0

            reasoning_text, cleaned_content = extract_reasoning(
                response.reasoning_content,
                response.thinking_blocks,
                response.content,
            )
            response.content = cleaned_content
            if reasoning_text and not context.streamed_reasoning:
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                context.streamed_reasoning = True

            if response.should_execute_tools:
                context.tool_calls = list(response.tool_calls)

                # B2 — Unknown-tool loop guard. Count calls to hallucinated
                # tool names across the turn; trip when one name has been
                # tried > threshold times. The registry would return a
                # "Tool 'X' not found" error every time anyway — we just
                # surface it as a distinct stop_reason instead of letting
                # the model burn more iterations on a doomed name.
                # Only runs against registries that expose a real
                # ``tool_names`` list/tuple/set — tests that mock the
                # registry without populating this naturally skip the check.
                known_names = getattr(spec.tools, "tool_names", None)
                unknown_offender: str | None = None
                if isinstance(known_names, (list, tuple, set, frozenset)):
                    for tc in response.tool_calls:
                        if tc.name not in known_names:
                            count = unknown_tool_attempts.get(tc.name, 0) + 1
                            unknown_tool_attempts[tc.name] = count
                            if count > max_unknown_tool_attempts:
                                unknown_offender = tc.name
                                break
                if unknown_offender is not None:
                    available = ", ".join(known_names) if known_names else ""
                    final_content = (
                        f"Error: model called unknown tool '{unknown_offender}' "
                        f"{unknown_tool_attempts[unknown_offender]} times this turn "
                        f"(threshold {max_unknown_tool_attempts}). "
                        f"Available tools: {available}"
                    )
                    stop_reason = "unknown_tool_loop_guard"
                    error = final_content
                    self._append_model_error_placeholder(messages)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    _ut_logger = current_telemetry()
                    if _ut_logger is not None:
                        with suppress(Exception):
                            _ut_logger.log("unknown_tool.loop_guard", {
                                "tool_name": unknown_offender,
                                "attempts": unknown_tool_attempts[unknown_offender],
                                "threshold": max_unknown_tool_attempts,
                                "iteration": iteration,
                                "session_key": spec.session_key,
                            })
                    logger.warning(
                        "Unknown-tool loop guard tripped on turn {} for {}: "
                        "tool='{}', attempts={}, threshold={}",
                        iteration,
                        spec.session_key or "default",
                        unknown_offender,
                        unknown_tool_attempts[unknown_offender],
                        max_unknown_tool_attempts,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    break

                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                    prompt_tokens=raw_usage.get("prompt_tokens"),
                )
                messages.append(assistant_message)
                tools_used.extend(tc.name for tc in response.tool_calls)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events, fatal_error = await self._execute_tools(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                    workspace_violation_counts,
                    seen_failed_calls,
                )
                tool_events.extend(new_events)
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                post_compact_trip: tuple[str, int] | None = None
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self._normalize_tool_result(
                            spec,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                    # Observe this tool call through the post-compaction
                    # guard. Only fires if the consolidator armed the guard
                    # recently AND the same triple is seen window_size times
                    # within the window.
                    if (
                        spec.post_compaction_guard is not None
                        and post_compact_trip is None
                    ):
                        try:
                            from durin.utils.post_compaction_guard import (
                                Observation,
                                hash_args,
                                hash_result,
                            )
                            obs = Observation(
                                tool_name=tool_call.name,
                                args_hash=hash_args(tool_call.arguments),
                                result_hash=hash_result(tool_message["content"]),
                            )
                            verdict = spec.post_compaction_guard.observe(
                                spec.session_key, obs,
                            )
                            # Strict ``is True`` so a MagicMock guard (used
                            # in tests that don't care about C2) doesn't
                            # accidentally trip via truthy mock attributes.
                            if verdict.should_abort is True:
                                post_compact_trip = (verdict.tool_name, verdict.repeat_count)
                        except Exception:
                            logger.exception(
                                "Post-compaction guard observe failed; skipping",
                            )
                # Per-turn aggregate budget: when many medium tool results
                # combine to exceed the configured budget, spill the largest
                # not-yet-persisted ones to disk. Mutates the appended
                # messages in place (they're the same dicts that were just
                # added to ``messages``).
                try:
                    self._enforce_turn_budget(spec, completed_tool_results)
                except Exception:
                    logger.exception(
                        "Turn-budget enforcement failed for {}; continuing with raw results",
                        spec.session_key or "default",
                    )
                if fatal_error is not None:
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                if post_compact_trip is not None:
                    pc_name, pc_count = post_compact_trip
                    final_content = (
                        f"Error: tool '{pc_name}' repeated {pc_count} times with "
                        f"identical args + result post-compaction. The compaction "
                        "did not break the loop. Aborting to prevent runaway "
                        "resource use."
                    )
                    stop_reason = "post_compaction_loop"
                    error = final_content
                    self._append_model_error_placeholder(messages)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    _pc_logger = current_telemetry()
                    if _pc_logger is not None:
                        with suppress(Exception):
                            _pc_logger.log("post_compaction_loop.tripped", {
                                "tool_name": pc_name,
                                "repeat_count": pc_count,
                                "iteration": iteration,
                                "session_key": spec.session_key,
                            })
                    logger.warning(
                        "Post-compaction loop guard tripped on turn {} for {}: "
                        "tool='{}', repeats={}",
                        iteration,
                        spec.session_key or "default",
                        pc_name,
                        pc_count,
                    )
                    await hook.after_iteration(context)
                    break
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )
                empty_content_retries = 0
                length_recovery_count = 0
                # Checkpoint 1: drain injections after tools, before the next
                # LLM call. Mid-turn, so steers and system results only —
                # plain user messages stay queued until the final response.
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                    steer_only=True,
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)

            # 2B — Reasoning-phase truncation recovery.
            #
            # Reasoning models (glm-5.1, o-series, Claude thinking) can hit the
            # output-token cap WHILE STILL DELIBERATING in `reasoning_content`,
            # never reaching the visible `content` phase. The signature is:
            #   finish_reason == "length" AND content is blank AND
            #   reasoning_content is non-empty.
            #
            # The default empty-content retry path would re-send the same prompt
            # and likely loop. Instead, append the partial reasoning to context
            # and inject a specific cue asking the model to wrap up its
            # thinking quickly and emit the final answer. This keeps the
            # model's chain-of-thought continuous and forces convergence.
            reasoning_truncated_mid_thought = (
                response.finish_reason == "length"
                and is_blank_text(clean)
                and bool(response.reasoning_content)
                and not response.tool_calls
            )
            if reasoning_truncated_mid_thought:
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Reasoning truncated mid-thought on turn {} for {} ({}/{}); "
                        "cueing the model to wrap up.",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    # Preserve the partial reasoning so the model can pick up
                    # its train of thought on the next turn — losing it would
                    # waste the tokens already spent.
                    messages.append(build_assistant_message(
                        "",  # no visible content yet
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append(build_reasoning_truncation_message())
                    await hook.after_iteration(context)
                    continue
                # If we already used our recovery budget, fall through to the
                # normal empty-content path (which terminates with an error).

            if response.finish_reason != "error" and is_blank_text(clean):
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                response = await self._request_finalization_retry(spec, messages_for_model, provider)
                retry_usage = self._usage_dict(response.usage)
                self._accumulate_usage(usage, retry_usage)
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                clean = hook.finalize_content(context, response.content)

            if response.finish_reason == "length" and not is_blank_text(clean):
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append(build_length_recovery_message())
                    await hook.after_iteration(context)
                    continue

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                    prompt_tokens=raw_usage.get("prompt_tokens"),
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response",
                iteration=iteration,
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":
                final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            messages.append(assistant_message or build_assistant_message(
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
                prompt_tokens=raw_usage.get("prompt_tokens"),
            ))
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            stop_reason = "max_iterations"
            if spec.max_iterations_message:
                final_content = spec.max_iterations_message.format(
                    max_iterations=spec.max_iterations,
                )
            else:
                final_content = render_template(
                    "agent/max_iterations_message.md",
                    strip=True,
                    max_iterations=spec.max_iterations,
                )
            self._append_final_message(messages, final_content)
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We ignore should_continue here because the for-loop has already
            # exhausted all iterations.
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
            llm_ms=total_llm_ms,
        )

    @staticmethod
    def _resolve_max_output(spec: AgentRunSpec, provider: LLMProvider | None) -> int:
        """The configured output ceiling for this run (max_tokens), falling
        back to the provider's generation default when the spec leaves it
        unset."""
        if isinstance(spec.max_tokens, int):
            return spec.max_tokens
        provider_max = getattr(getattr(provider, "generation", None), "max_tokens", 4096)
        return provider_max if isinstance(provider_max, int) else 4096

    def _input_budget(self, spec: AgentRunSpec, provider: LLMProvider | None) -> int | None:
        """Input-token budget: ``context_block_limit`` when set, else the
        context window minus a *capped* output reservation and the safety
        buffer. Returns ``None`` when no window is configured.

        The reservation is capped at ``_MAX_OUTPUT_RESERVATION`` so a large
        configured ``max_tokens`` ceiling does not starve the input — the
        ceiling is still honoured for output via the dynamic ``max_tokens``
        sent on the request (see ``_effective_max_tokens``).
        """
        if not spec.context_window_tokens:
            return None
        if spec.context_block_limit:
            return spec.context_block_limit
        reservation = _output_reservation(self._resolve_max_output(spec, provider))
        return spec.context_window_tokens - reservation - _SNIP_SAFETY_BUFFER

    def _estimate_and_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        provider: LLMProvider | None = None,
    ) -> tuple[int, int] | None:
        """Estimate the post-sanitize prompt size and return
        ``(estimated_tokens, budget_tokens)``.

        Returns ``None`` when no budget can be computed (no window, empty
        message list, non-positive budget, or estimator failure) — callers
        then proceed without a precheck. Unlike ``_mid_turn_precheck`` this
        returns the estimate *whether or not* it fits, so the caller can
        reuse it to size the dynamic output budget without re-estimating.
        """
        _provider = provider if provider is not None else self.provider
        if not messages:
            return None
        budget = self._input_budget(spec, _provider)
        if budget is None or budget <= 0:
            return None
        try:
            estimate, _ = estimate_prompt_tokens_chain(
                _provider,
                spec.model,
                messages,
                self._active_tool_definitions(spec),
            )
        except Exception:
            # Token estimation is best-effort; never block a turn on the
            # estimator failing. Let the provider's own 400 handle it.
            logger.exception(
                "Mid-turn precheck estimation failed for {}; skipping",
                spec.session_key or "default",
            )
            return None
        return estimate, budget

    def _mid_turn_precheck(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        provider: LLMProvider | None = None,
    ) -> tuple[int, int] | None:
        """Mid-turn precheck.

        Estimate whether the post-sanitize prompt fits in the budget. Returns
        ``(estimated_tokens, budget_tokens)`` when overflow is detected,
        ``None`` when the prompt fits (the common case — fast path).

        ``_snip_history`` already trims from the head, but
        ``find_legal_message_start`` may force-keep messages that exceed the
        budget (Anthropic role-alternation requirements). A single oversized
        tool result late in the history can survive snipping. Catching this
        here saves an LLM call that would have hit a 400 anyway and ensures
        the caller gets a distinct ``mid_turn_precheck_overflow`` stop_reason
        instead of a generic provider error.
        """
        decision = self._estimate_and_budget(spec, messages, provider)
        if decision is None:
            return None
        estimate, budget = decision
        if estimate <= budget:
            return None
        return estimate, budget

    def _effective_max_tokens(
        self,
        spec: AgentRunSpec,
        estimate: int,
        provider: LLMProvider | None = None,
    ) -> int | None:
        """Dynamic output cap to send on a request: the output ceiling clamped
        to the room the prompt actually leaves in the window.

        The ceiling is ``spec.max_tokens`` when set, else the provider's
        ``generation.max_tokens`` — because the main loop leaves
        ``spec.max_tokens`` unset and the real output ceiling lives on the
        provider. Returns ``None`` only when no window is known (then the
        caller omits ``max_tokens`` and the provider uses its own default).

        When a window is known the result never lets ``estimate + max_tokens``
        exceed ``window - buffer`` — so the full ceiling is sent only when
        there is room, and it shrinks to fit otherwise. This is what keeps the
        capped *input* reservation honest: a large input borrows from the
        output headroom rather than overflowing the window.
        """
        if not spec.context_window_tokens:
            return None
        ceiling = self._resolve_max_output(spec, provider if provider is not None else self.provider)
        room = spec.context_window_tokens - estimate - _SNIP_SAFETY_BUFFER
        return max(1, min(ceiling, room))

    def _emergency_trim_for_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        budget: int,
        provider: LLMProvider | None = None,
    ) -> tuple[list[dict[str, Any]], bool, int | None]:
        """Last-resort recovery for a prompt that overflows after sanitize.

        Works on a *copy* (the canonical history is never mutated): the
        largest string-valued tool result is truncated, then the prompt is
        re-estimated, repeating until it fits or nothing more can be
        trimmed. Truncating content (rather than dropping messages) keeps
        role-alternation intact.

        Returns ``(trimmed_messages, fit, new_estimate)``. ``fit`` is
        ``True`` only when the trimmed prompt is at or under ``budget``;
        ``new_estimate`` is the estimate of the returned list (``None`` when
        there was nothing to trim).
        """
        _provider = provider if provider is not None else self.provider

        def _trimmable(msg: dict[str, Any]) -> bool:
            return (
                msg.get("role") == "tool"
                and isinstance(msg.get("content"), str)
                and len(msg["content"]) > _EMERGENCY_TRIM_FLOOR_CHARS + len(_EMERGENCY_TRIM_MARKER)
            )

        if not any(_trimmable(m) for m in messages):
            return messages, False, None

        work = [dict(m) for m in messages]
        new_estimate: int | None = None
        for _ in range(_EMERGENCY_TRIM_MAX_PASSES):
            decision = self._estimate_and_budget_for(work, spec, _provider)
            if decision is None:
                break
            new_estimate = decision
            if new_estimate <= budget:
                return work, True, new_estimate
            # Truncate the single largest trimmable tool result by half,
            # flooring its length so we don't loop forever on a result that
            # is already small.
            largest_idx = -1
            largest_len = 0
            for idx, msg in enumerate(work):
                if _trimmable(msg) and len(msg["content"]) > largest_len:
                    largest_len = len(msg["content"])
                    largest_idx = idx
            if largest_idx < 0:
                break
            content = work[largest_idx]["content"]
            keep = max(_EMERGENCY_TRIM_FLOOR_CHARS, len(content) // 2)
            work[largest_idx] = dict(work[largest_idx])
            work[largest_idx]["content"] = content[:keep] + _EMERGENCY_TRIM_MARKER
        # Final re-estimate after the loop's last truncation.
        decision = self._estimate_and_budget_for(work, spec, _provider)
        if decision is not None:
            new_estimate = decision
            if new_estimate <= budget:
                return work, True, new_estimate
        return work, False, new_estimate

    def _estimate_and_budget_for(
        self,
        messages: list[dict[str, Any]],
        spec: AgentRunSpec,
        provider: LLMProvider | None,
    ) -> int | None:
        """Estimate the prompt size for ``messages`` (best-effort, returns
        ``None`` on estimator failure)."""
        try:
            estimate, _ = estimate_prompt_tokens_chain(
                provider,
                spec.model,
                messages,
                self._active_tool_definitions(spec),
            )
        except Exception:
            return None
        return estimate

    @staticmethod
    def _active_tool_definitions(spec: AgentRunSpec) -> list[dict[str, Any]]:
        """Return tool definitions filtered by the active agent mode.

        If ``spec.mode_provider`` is ``None`` (no agent-mode wiring) or the
        mode has no restrictions (default BUILD_MODE), returns the cached
        definitions verbatim — the fast path for most turns. When a mode does
        restrict the surface, this returns a filtered slice; the registry
        cache stays valid.
        """
        all_defs = spec.tools.get_definitions()
        if spec.mode_provider is None:
            return all_defs
        try:
            mode = spec.mode_provider()
        except Exception:
            return all_defs
        if mode is None or (mode.allowed is None and not mode.denied):
            return all_defs
        from durin.agent.tools.registry import ToolRegistry

        return [
            d for d in all_defs
            if mode.is_tool_allowed(ToolRegistry._schema_name(d))
        ]

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        max_tokens_override: int | None = None,
    ) -> dict[str, Any]:
        # Apply the optional context_transform hook (pi-style). The hook
        # gets a shallow copy of the message list so it can mutate
        # without surprising upstream code. It can return:
        #   - a new list to use instead
        #   - the same list (mutated in place)
        #   - None to signal "leave the list as-is"
        # Any exception is swallowed and the original list is used — a
        # broken hook must NEVER break the agent loop.
        if spec.context_transform is not None:
            try:
                transformed = spec.context_transform(list(messages))
                if isinstance(transformed, list):
                    messages = transformed
                    # Re-sanitize after the hook. A transform that drops or
                    # trims messages for token budget can leave assistant
                    # tool_use blocks without matching tool_result siblings
                    # (or vice versa); Anthropic and OpenAI both reject such
                    # mismatches with a 400. The pre-call sanitize pipeline
                    # already ran on the untransformed list, so it can't
                    # catch this. Repair here defensively
                    # (re-sanitize after truncation).
                    try:
                        messages = self._drop_orphan_tool_results(messages)
                        messages = self._backfill_missing_tool_results(messages)
                    except Exception:
                        logger.exception(
                            "post-context_transform sanitize failed; sending "
                            "the transformed list as-is",
                        )
            except Exception:
                logger.exception(
                    "context_transform hook raised — falling back to original messages",
                )
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if max_tokens_override is not None:
            # Dynamic output budget reused from the precheck estimate: fills
            # the room the prompt actually left, up to the ceiling. Applied
            # even when ``spec.max_tokens`` is unset (the main-loop case),
            # overriding the provider's generation default so a capped input
            # reservation can't let input+output overflow the window.
            kwargs["max_tokens"] = max_tokens_override
        elif spec.context_window_tokens:
            # No estimate available (finalization-retry / direct callers) but
            # a window is known: send a conservative window-safe cap. Input
            # already fits under ``window - reservation - buffer``, so
            # ``_MAX_OUTPUT_RESERVATION`` of output can never overflow.
            ceiling = spec.max_tokens if isinstance(spec.max_tokens, int) else _MAX_OUTPUT_RESERVATION
            kwargs["max_tokens"] = min(ceiling, _MAX_OUTPUT_RESERVATION)
        elif spec.max_tokens is not None:
            # No window to protect — honour the configured ceiling verbatim.
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        # Sampling params. top_p is a standard OpenAI param; top_k and
        # repeat_penalty are non-standard and ride in extra_body (ollama /
        # LM Studio read them there). extra_body is only created when at least
        # one non-standard param is set, so an all-unset spec emits nothing.
        if spec.top_p is not None:
            kwargs["top_p"] = spec.top_p
        if spec.top_k is not None:
            kwargs.setdefault("extra_body", {})["top_k"] = spec.top_k
        if spec.repeat_penalty is not None:
            kwargs.setdefault("extra_body", {})["repeat_penalty"] = spec.repeat_penalty
        return kwargs

    async def _await_with_compaction_grace(
        self,
        coro: Any,
        *,
        base_timeout: float,
        spec: AgentRunSpec,
    ) -> Any:
        """Await ``coro`` with the outer LLM wall-clock timeout, but extend
        the deadline by one grace window if compaction is in flight for
        the session at the moment the base timeout would have fired.

        Uses ``asyncio.wait({task}, timeout=...)`` (which does NOT cancel
        the task on timeout — unlike ``asyncio.wait_for``) so we can probe
        the compaction state and optionally keep waiting on the same task.
        If grace is also exhausted, the task is cancelled and
        ``asyncio.TimeoutError`` is raised so the caller's existing timeout
        handler maps it to an LLMResponse error_kind="timeout".

        Grace is used at most once per request, only when compaction is
        detected at the moment the base timeout fires.
        """
        task = asyncio.ensure_future(coro)
        try:
            done, _pending = await asyncio.wait({task}, timeout=base_timeout)
            if task in done:
                return task.result()

            grace_s = _compaction_grace_seconds()
            compacting = False
            if grace_s > 0 and spec.is_compacting is not None:
                try:
                    compacting = bool(spec.is_compacting())
                except Exception:
                    logger.exception(
                        "is_compacting callback raised — treating as not compacting",
                    )
                    compacting = False
            if compacting:
                logger.info(
                    "LLM wall-clock timeout fired during active compaction for {}; "
                    "extending deadline by {}s (one-shot grace)",
                    spec.session_key or "default",
                    grace_s,
                )
                _logger = current_telemetry()
                if _logger is not None:
                    with suppress(Exception):
                        _logger.log("compaction.grace_extended", {
                            "base_timeout_s": base_timeout,
                            "grace_s": grace_s,
                            "session_key": spec.session_key,
                        })
                done, _pending = await asyncio.wait({task}, timeout=grace_s)
                if task in done:
                    return task.result()

            task.cancel()
            with suppress(BaseException):
                await task
            raise asyncio.TimeoutError()
        except BaseException:
            if not task.done():
                task.cancel()
                with suppress(BaseException):
                    await task
            raise

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
        provider: LLMProvider | None = None,
        *,
        max_tokens_override: int | None = None,
    ):
        _provider = provider if provider is not None else self.provider
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            # Wall-clock cap on the whole request. An explicit DURIN_LLM_TIMEOUT_S
            # always wins (0 disables). Otherwise: providers whose chat_stream()
            # runs an idle-stall watchdog get a generous 30-min backstop — a hung
            # request is normally caught by stream silence, while an
            # actively-generating call stays alive as long as it needs (long
            # syntheses died at the old 300s default mid-generation). The
            # backstop is not redundant with the watchdog: some gateways send
            # payload-less heartbeat chunks that keep resetting it, so a wedged
            # backend behind such a gateway would otherwise hold the session
            # lock forever — unacceptable for unattended runs (workflows,
            # dream, cron). Providers without the watchdog keep the tighter
            # finite default.
            raw = os.environ.get("DURIN_LLM_TIMEOUT_S", "").strip()
            if raw:
                try:
                    timeout_s = float(raw)
                except (TypeError, ValueError):
                    timeout_s = 300.0
            elif getattr(_provider, "supports_native_streaming", False):
                timeout_s = 1800.0
            else:
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=self._active_tool_definitions(spec),
            max_tokens_override=max_tokens_override,
        )
        wants_streaming = hook.wants_streaming()
        wants_progress_streaming = (
            not wants_streaming
            and spec.stream_progress_deltas
            and spec.progress_callback is not None
            and getattr(_provider, "supports_progress_deltas", False) is True
        )

        progress_state: dict[str, bool] | None = None

        if wants_streaming:
            async def _stream(delta: str) -> None:
                if delta:
                    context.streamed_content = True
                await hook.on_stream(context, delta)

            async def _thinking(delta: str) -> None:
                if not delta:
                    return
                context.streamed_reasoning = True
                await hook.emit_reasoning(delta)

            coro = _provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
            )
        elif wants_progress_streaming:
            stream_buf = ""
            think_extractor = IncrementalThinkExtractor()
            progress_state = {"reasoning_open": False}

            async def _stream_progress(delta: str) -> None:
                nonlocal stream_buf
                if not delta:
                    return
                prev_clean = strip_think(stream_buf)
                stream_buf += delta
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean):]

                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    context.streamed_reasoning = True
                    progress_state["reasoning_open"] = True

                if incremental:
                    if progress_state["reasoning_open"]:
                        await hook.emit_reasoning_end()
                        progress_state["reasoning_open"] = False
                    context.streamed_content = True
                    await spec.progress_callback(incremental)

            coro = _provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream_progress,
            )
        else:
            coro = _provider.chat_with_retry(**kwargs)

        # Streaming requests already have provider-level idle timeouts
        # (DURIN_STREAM_IDLE_TIMEOUT_S). Do not also apply the outer wall-clock
        # LLM timeout here, or healthy long reasoning streams can be killed just
        # because total elapsed time exceeded DURIN_LLM_TIMEOUT_S. Hook-less
        # calls on natively-streaming providers already resolved timeout_s to
        # None above for the same reason.
        outer_timeout_s = None if (wants_streaming or wants_progress_streaming) else timeout_s
        try:
            if outer_timeout_s is None:
                response = await coro
            else:
                response = await self._await_with_compaction_grace(
                    coro,
                    base_timeout=outer_timeout_s,
                    spec=spec,
                )
        except asyncio.TimeoutError:
            if outer_timeout_s is None:
                return LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            return LLMResponse(
                content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        return response

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        provider: LLMProvider | None = None,
    ):
        _provider = provider if provider is not None else self.provider
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        kwargs = self._build_request_kwargs(spec, retry_messages, tools=None)
        return await _provider.chat_with_retry(**kwargs)

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        seen_failed_calls: set[str],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        batches = self._partition_tool_batches(spec, tool_calls)
        self._emit_parallelism_telemetry(spec, tool_calls, batches)
        tool_results: list[tuple[Any, dict[str, Any], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(*(
                    self._run_tool_timed(
                        spec, tool_call, external_lookup_counts, workspace_violation_counts,
                        seen_failed_calls,
                    )
                    for tool_call in batch
                ))
                tool_results.extend(batch_results)
            else:
                batch_results = []
                for tool_call in batch:
                    result = await self._run_tool_timed(
                        spec, tool_call, external_lookup_counts, workspace_violation_counts,
                        seen_failed_calls,
                    )
                    tool_results.append(result)
                    batch_results.append(result)

        results: list[Any] = []
        events: list[dict[str, Any]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    def _emit_parallelism_telemetry(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        batches: list[list[ToolCallRequest]],
    ) -> None:
        """Record the parallelisation shape of one assistant turn.

        Emitted once per turn that issued ≥1 tool call. Captures whether the
        model's independent calls actually ran in parallel so we can measure —
        not assume — how well the parallel-tool instruction lands across
        models. ``concurrency_safe_count`` ≥ 2 with ``max_batch_size`` == 1
        would be a missed-batching signal (parallel-safe tools that did not
        share a turn). Best-effort: telemetry never breaks the turn.
        """
        if not tool_calls:
            return
        _logger = current_telemetry()
        if _logger is None:
            return
        # Best-effort: a malformed tools registry or tool must never break the
        # turn, so the whole computation is guarded.
        with suppress(Exception):
            get_tool = spec.tools.get
            safe_count = 0
            fanout_calls = 0      # calls that fan out over a list (≥2 items)
            fanout_items = 0      # total parallel items across those calls
            background_launches = 0  # calls that launch background work (spawn)
            for tc in tool_calls:
                tool = get_tool(tc.name)
                if tool is None:
                    continue
                if tool.concurrency_safe:
                    safe_count += 1
                if tool.launches_background:
                    background_launches += 1
                size = tool.fanout_size(tc.arguments or {})
                if isinstance(size, int) and size > 1:
                    fanout_calls += 1
                    fanout_items += size
            max_batch = max((len(b) for b in batches), default=0)
            # Parallelism happens three ways: a harness batch of ≥2
            # concurrency-safe calls, a single call that fans out over a list,
            # or ≥2 background launches (spawn). True if ANY of them did.
            parallelized = max_batch > 1 or fanout_calls > 0 or background_launches >= 2
            _logger.log("tools.parallelism", {
                "session_key": spec.session_key,
                "tool_calls": len(tool_calls),
                "batches": len(batches),
                "max_batch_size": max_batch,
                "concurrency_safe_calls": safe_count,
                "fanout_calls": fanout_calls,
                "fanout_items": fanout_items,
                "background_launches": background_launches,
                "parallelized": parallelized,
                "concurrent_tools_enabled": bool(spec.concurrent_tools),
            })

    async def _run_tool_timed(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        seen_failed_calls: set[str],
    ) -> tuple[Any, dict[str, Any], BaseException | None]:
        """Wrap :meth:`_run_tool` with wall-time measurement and ``tool_call_id``
        enrichment so callers can correlate events back to the assistant
        message that emitted the call. Keeps the underlying ``_run_tool``
        signature untouched; the six return paths inside it do not need
        to know about ``tool_call_id`` or ``duration_ms``.
        """
        started = time.monotonic()
        try:
            result, event, error = await self._run_tool(
                spec, tool_call, external_lookup_counts, workspace_violation_counts,
                seen_failed_calls,
            )
        finally:
            duration_ms = (time.monotonic() - started) * 1000.0
        if isinstance(event, dict):
            event = dict(event)
            event.setdefault("tool_call_id", tool_call.id)
            event.setdefault("duration_ms", round(duration_ms, 1))
        return result, event, error

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        seen_failed_calls: set[str],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        hint = "\n\n[Analyze the error above and try a different approach.]"

        # 1A — Loop-detection: if THIS exact (name, args) tuple already failed
        # in this turn, short-circuit before re-running. We don't re-execute the
        # tool — we just remind the model that it has tried this and it failed,
        # so it must pick a different path. Cheap to compute (sha256 prefix),
        # avoids the cost of a doomed second execution, and breaks the common
        # "fixate on plan despite seeing the error" failure mode.
        signature = _tool_call_signature(tool_call.name, tool_call.arguments)
        if signature in seen_failed_calls:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "loop blocked: identical failed call repeated",
            }
            return _LOOP_BLOCK_MESSAGE, event, None

        # Sprint B / L3 — mode-based denial. The filtered tool definitions
        # passed to the LLM already exclude tools not allowed in the current
        # mode, so this branch fires only when the model emits a cached
        # tool name. The denial is informative so the model knows to switch
        # mode (via /build approval, typically) rather than to retry blindly.
        if spec.mode_provider is not None:
            try:
                active_mode = spec.mode_provider()
            except Exception:
                active_mode = None
            if active_mode is not None and not active_mode.is_tool_allowed(tool_call.name):
                msg = (
                    f"Tool '{tool_call.name}' is not available in mode "
                    f"'{active_mode.name}'. The user must run `/build` (or "
                    "the active mode must change) before this tool can be "
                    "called. Do not retry — adjust your approach."
                )
                event = {
                    "name": tool_call.name,
                    "status": "error",
                    "detail": f"denied by mode '{active_mode.name}'",
                }
                logger_obj = current_telemetry()
                if logger_obj is not None:
                    with suppress(Exception):
                        logger_obj.log("agent_mode.tool_denied", {
                            "tool": tool_call.name,
                            "mode": active_mode.name,
                        })
                return msg + hint, event, None

        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            seen_failed_calls.add(signature)
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            if spec.fail_on_tool_error:
                return lookup_error + hint, event, RuntimeError(lookup_error)
            return lookup_error + hint, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            with suppress(Exception):
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
        if prep_error:
            # 1A — record this signature as failed (preparation issue is
            # deterministic given args: repeating with same args will hit it
            # again).
            seen_failed_calls.add(signature)
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            }
            handled = self._classify_violation(
                raw_text=prep_error,
                soft_payload=prep_error + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            return prep_error + hint, event, (
                RuntimeError(prep_error) if spec.fail_on_tool_error else None
            )
        try:
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # 1A — exceptions from the tool itself are deterministic w.r.t. args
            # for the vast majority of tools (a bad path, missing dep, syntax
            # error in a file edit, etc.). Mark this signature as failed so a
            # repeat is short-circuited.
            seen_failed_calls.add(signature)
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            payload = f"Error: {type(exc).__name__}: {exc}"
            handled = self._classify_violation(
                raw_text=str(exc),
                # Preserve legacy exception payloads without the retry hint.
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return payload, event, exc
            return payload, event, None

        if isinstance(result, str) and result.startswith("Error"):
            # 1A — the tool returned an explicit error string. Mark signature
            # as failed. NOTE: this does NOT cover tools that return a valid
            # payload with embedded failure (e.g., `exec("pytest")` returning
            # the test failure output as a normal result). Pytest failures are
            # NOT marked here, so the model can re-run pytest after editing.
            seen_failed_calls.add(signature)
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            handled = self._classify_violation(
                raw_text=result,
                soft_payload=result + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return result + hint, event, RuntimeError(result)
            return result + hint, event, None

        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        return result, {"name": tool_call.name, "status": "ok", "detail": detail}, None

    # SSRF is a hard security block at the tool boundary, but the agent turn
    # should recover conversationally instead of aborting the runtime.
    _SSRF_MARKERS: tuple[str, ...] = (
        "internal/private url detected",
        "private/internal address",
        "private address",
    )
    _SSRF_BOUNDARY_NOTE: str = (
        "This is a non-bypassable security boundary. Stop trying to access "
        "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
        "alternate DNS, redirects, proxies, or another tool. Ask the user for "
        "local files, logs, screenshots, or an explicit safe public URL instead. "
        "If the user explicitly trusts this private URL, ask them to whitelist "
        "the exact IP/CIDR via tools.ssrfWhitelist."
    )

    # Non-SSRF boundary markers returned to the LLM as recoverable tool errors.
    _WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (
        "outside the configured workspace",
        "outside allowed directory",
        "working_dir is outside",
        "working_dir could not be resolved",
        "path outside working dir",
        "path traversal detected",
    )

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._SSRF_MARKERS)

    @classmethod
    def _is_workspace_violation(cls, text: str) -> bool:
        """True when *text* looks like any policy boundary rejection."""
        if not text:
            return False
        lowered = text.lower()
        if cls._is_ssrf_violation(lowered):
            return True
        return any(marker in lowered for marker in cls._WORKSPACE_VIOLATION_MARKERS)

    def _classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, str],
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None] | None:
        """Classify safety-boundary failures, or return ``None`` to pass through."""
        if self._is_ssrf_violation(raw_text):
            logger.warning(
                "Tool {} blocked by SSRF guard; returning non-retryable tool error: {}",
                tool_call.name,
                raw_text.replace("\n", " ").strip()[:200],
            )
            event["detail"] = self._event_detail("ssrf_violation: ", raw_text)
            return self._ssrf_soft_payload(raw_text), event, None

        if self._is_workspace_violation(raw_text):
            escalation = repeated_workspace_violation_error(
                tool_call.name,
                tool_call.arguments,
                workspace_violation_counts,
            )
            event["detail"] = self._event_detail("workspace_violation: ", raw_text)
            if escalation is not None:
                logger.warning(
                    "Tool {} hit workspace boundary repeatedly; escalating hint",
                    tool_call.name,
                )
                event["detail"] = self._event_detail(
                    "workspace_violation_escalated: ",
                    raw_text,
                )
                return escalation, event, None
            return soft_payload, event, None

        return None

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        return (prefix + text.replace("\n", " ").strip())[:limit]

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    @staticmethod
    def _append_overflow_placeholder(messages: list[dict[str, Any]]) -> None:
        """Persist an overflow-specific assistant placeholder.

        Distinct from ``_append_model_error_placeholder`` so the transcript
        (and any downstream reader) can tell "durin aborted the turn because
        the prompt exceeded the input budget" apart from "the LLM errored".
        """
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_OVERFLOW_PLACEHOLDER))

    def _normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        result = ensure_nonempty_tool_result(tool_name, result)
        # Per-block validation runs FIRST so the aggregate cap below sees a
        # list whose blocks each fit. This caps single image/audio payloads
        # before they distort the rest of the context, and trims runaway
        # text blocks before they crowd out their siblings.
        try:
            result = validate_tool_result_blocks(result)
        except Exception:
            logger.exception(
                "Tool result block validation failed for {} in {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
            )
        try:
            content = maybe_persist_tool_result(
                spec.workspace,
                spec.session_key,
                tool_call_id,
                result,
                max_chars=spec.max_tool_result_chars,
            )
        except Exception:
            logger.exception(
                "Tool result persist failed for {} in {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
            )
            content = result
        # Redact stored secret values before the result enters the
        # model context.
        try:
            from durin.security.secrets import redact_secrets

            content = redact_secrets(content)
        except Exception:  # noqa: BLE001
            logger.exception("Secret redaction failed for {}; using raw result", tool_call_id)
        content = self._coerce_tool_content(content)
        if isinstance(content, str) and len(content) > spec.max_tool_result_chars:
            return truncate_text(content, spec.max_tool_result_chars)
        return content

    @staticmethod
    def _coerce_tool_content(content: Any) -> Any:
        """Force a tool result into a provider-safe shape.

        A result must be a string or a list of typed content blocks.
        A tool that returns a raw dict (e.g. ``memory_search`` →
        ``{"results": [...], ...}``) would otherwise be wrapped into a
        single content block with no ``type``, which strict provider
        APIs reject (z.ai 1214: ``content[0].type: cannot be empty``).
        Anything that is not a string or a clean block-list is
        JSON-encoded.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list) and content and all(
            isinstance(block, dict) and block.get("type") for block in content
        ):
            return content
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            return str(content)

    @staticmethod
    def _content_size(content: Any) -> int:
        """Best-effort char-count for a tool result.

        Strings → ``len``. Lists of text blocks → joined text length. Other
        list-of-blocks (image, audio) → JSON-serialized length so a 5 MB
        image-block isn't undercounted as zero. Falls back to ``str()`` on
        unexpected shapes.
        """
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            try:
                return len(json.dumps(content, ensure_ascii=False, default=str))
            except Exception:
                return sum(len(str(b)) for b in content)
        try:
            return len(str(content))
        except Exception:
            return 0

    def _enforce_turn_budget(
        self,
        spec: AgentRunSpec,
        completed_tool_messages: list[dict[str, Any]],
    ) -> None:
        """Per-turn aggregate budget enforcement.

        After all tool results for a turn are collected, if their total
        size exceeds the configured budget, persist the largest
        not-yet-persisted ones to disk (via the existing
        ``maybe_persist_tool_result`` path with threshold=0 to force
        spillover) until the aggregate is back under budget.

        Mutates ``completed_tool_messages[i]["content"]`` in place. Also
        emits a single ``turn_budget.enforced`` telemetry event when the
        budget was exceeded.
        """
        budget = _turn_budget_chars()
        if budget <= 0 or not completed_tool_messages:
            return
        sizes: list[tuple[int, int]] = []  # (idx, size)
        total = 0
        for idx, msg in enumerate(completed_tool_messages):
            size = self._content_size(msg.get("content"))
            total += size
            content = msg.get("content")
            already_persisted = isinstance(content, str) and _PERSISTED_MARKER in content
            if not already_persisted:
                sizes.append((idx, size))
        if total <= budget:
            return
        sizes.sort(key=lambda pair: pair[1], reverse=True)
        original_total = total
        spilled = 0
        for idx, size in sizes:
            if total <= budget:
                break
            msg = completed_tool_messages[idx]
            try:
                # ``maybe_persist_tool_result`` treats max_chars<=0 as "disabled".
                # Pass 1 so it spills any content that's at least 2 chars long
                # — effectively unconditional for the candidates we picked
                # (the smallest realistic culprit is already several KB).
                spilled_content = maybe_persist_tool_result(
                    spec.workspace,
                    spec.session_key,
                    str(msg.get("tool_call_id") or f"tool_{idx}"),
                    msg.get("content"),
                    max_chars=1,
                )
            except Exception:
                logger.exception(
                    "Turn-budget spillover failed for {} in {}",
                    msg.get("tool_call_id"),
                    spec.session_key or "default",
                )
                continue
            new_size = self._content_size(spilled_content)
            if new_size < size:
                msg["content"] = spilled_content
                total = total - size + new_size
                spilled += 1
        if spilled > 0:
            _logger = current_telemetry()
            if _logger is not None:
                with suppress(Exception):
                    _logger.log("turn_budget.enforced", {
                        "session_key": spec.session_key,
                        "budget_chars": budget,
                        "before_chars": original_total,
                        "after_chars": total,
                        "spilled_count": spilled,
                        "tool_count": len(completed_tool_messages),
                    })
            logger.info(
                "Turn budget exceeded for {}: {}/{} chars; spilled {} result(s) "
                "to disk → {} chars",
                spec.session_key or "default",
                original_total,
                budget,
                spilled,
                total,
            )

    @staticmethod
    def _drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop tool results that have no matching assistant tool_call earlier in the history."""
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def _backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert synthetic error results for orphaned tool_use blocks."""
        declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": _BACKFILL_CONTENT,
            })
            offset += 1
        return updated

    def _microcompact(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        provider: LLMProvider | None = None,
    ) -> list[dict[str, Any]]:
        """Replace old compactable tool results with a recoverable one-line reference.

        Results beyond the most recent ``_MICROCOMPACT_KEEP_RECENT`` are
        collapsed to a short placeholder to reclaim context. Unlike a blind
        drop, the placeholder keeps a pointer to the spilled file so the model
        can ``read_file`` to recover the full output: already-spilled results
        keep their existing path, never-spilled results are spilled on the spot.

        Gated on token pressure: below ``_MICROCOMPACT_PRESSURE_RATIO`` of the
        input budget, messages are returned untouched — freeing context that
        the prompt doesn't need yet isn't worth losing full tool results the
        model may still need to answer a later question.
        """
        compactable_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "tool" and msg.get("name") in _COMPACTABLE_TOOLS:
                compactable_indices.append(idx)

        if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
            return messages

        _provider = provider if provider is not None else self.provider
        budget = self._input_budget(spec, _provider)
        if budget is not None and budget > 0:
            try:
                estimate, _ = estimate_prompt_tokens_chain(
                    _provider,
                    spec.model,
                    messages,
                    self._active_tool_definitions(spec),
                )
            except Exception:
                estimate = None
            if estimate is not None and estimate <= budget * _MICROCOMPACT_PRESSURE_RATIO:
                return messages

        stale = compactable_indices[: len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
        updated: list[dict[str, Any]] | None = None
        for idx in stale:
            msg = messages[idx]
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
                continue
            summary = self._microcompact_reference(spec, msg, idx, content)
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary

        return updated if updated is not None else messages

    def _microcompact_reference(
        self,
        spec: AgentRunSpec,
        msg: dict[str, Any],
        idx: int,
        content: str,
    ) -> str:
        """Build the recoverable omission placeholder for one stale tool result."""
        name = msg.get("name", "tool")
        began = ""
        if parse_persisted_reference(content) is None:
            head = " ".join(content[:_MICROCOMPACT_HEAD_CHARS].split())
            began = f' — began: "{head}…"'
        ref = parse_persisted_reference(content)
        if ref is None:
            # Never spilled (result fit under max_tool_result_chars). Spill it
            # now so the omission stays recoverable; max_chars=1 forces it.
            call_id = str(msg.get("tool_call_id") or f"tool_{idx}")
            try:
                spilled = maybe_persist_tool_result(
                    spec.workspace, spec.session_key, call_id, content, max_chars=1,
                )
            except Exception:
                logger.exception("microcompact spill failed for {}", call_id)
                spilled = None
            if isinstance(spilled, str):
                ref = parse_persisted_reference(spilled)
        if ref is not None:
            path, size = ref
            return (
                f"[{name} result trimmed{began} — full output "
                f"({size} chars) at {path}; use read_file to recover]"
            )
        # No workspace to spill to: keep the (lossy) marker, but honest.
        return f"[{name} result trimmed{began} — no longer in context]"

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._normalize_tool_result(
                spec,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        provider: LLMProvider | None = None,
    ) -> list[dict[str, Any]]:
        _provider = provider if provider is not None else self.provider
        if not messages or not spec.context_window_tokens:
            return messages

        budget = self._input_budget(spec, _provider)
        if budget is None or budget <= 0:
            return messages

        estimate, _ = estimate_prompt_tokens_chain(
            _provider,
            spec.model,
            messages,
            self._active_tool_definitions(spec),
        )
        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        remaining_budget = max(128, budget - system_tokens)
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                # Recover nearest user message from outside the kept window;
                # GLM rejects system→assistant (error 1214).  Budget is
                # intentionally exceeded — oversized beats invalid.
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
                # If no user exists at all, _enforce_role_alternation
                # will insert a synthetic one as a safety net.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        """Group tool calls into batches that are safe to execute in parallel.

        1B — Topological ordering safety.
        We never reorder the model's emitted tool calls; instead we walk them
        in sequence and group only CONSECUTIVE `concurrency_safe` tools (read-
        only and non-exclusive) into a parallel batch. Anything else — a
        mutation, an exclusive tool — gets its own singleton batch.

        This prevents race conditions when the model emits something like
        `[edit_file(A), read_file(A)]` or `[write_file(A), exec("pytest")]`:
        the mutation always completes before any read that follows it.
        Reordering globally (all reads first, all writes after) would be
        wrong because the model often depends on ordering — a read before a
        write captures the pre-edit state, a read after captures the post-edit
        state. Preserving order is the only correct default.

        Tools default to ``read_only=False`` in :class:`Tool`, so any tool
        that doesn't explicitly opt into safety is run alone. This is a safe
        default: it costs a missed parallelism opportunity in exchange for
        guaranteed correctness when tool metadata is missing.
        """
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and tool.concurrency_safe)
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches
