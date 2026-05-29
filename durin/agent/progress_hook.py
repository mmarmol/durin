"""Agent hook that adapts runner events into channel progress UI."""

from __future__ import annotations

import inspect
import json
from typing import Any, Awaitable, Callable

from loguru import logger

from contextlib import suppress

from durin.agent.hook import AgentHook, AgentHookContext
from durin.telemetry.logger import current_telemetry
from durin.utils.helpers import IncrementalThinkExtractor, strip_think
from durin.utils.progress_events import (
    build_tool_event_finish_payloads,
    build_tool_event_start_payload,
    invoke_on_progress,
    on_progress_accepts_tool_events,
)
from durin.utils.tool_hints import format_tool_hints


class AgentProgressHook(AgentHook):
    """Translate runner lifecycle events into user-visible progress signals."""

    def __init__(
        self,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
        *,
        channel: str = "cli",
        chat_id: str = "direct",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        tool_hint_max_length: int = 40,
        set_tool_context: Callable[..., None] | None = None,
        on_iteration: Callable[[int], None] | None = None,
        on_cache_usage: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(reraise=True)
        self._on_progress = on_progress
        self._on_stream = on_stream
        self._on_stream_end = on_stream_end
        self._channel = channel
        self._chat_id = chat_id
        self._message_id = message_id
        self._metadata = metadata or {}
        self._session_key = session_key
        self._tool_hint_max_length = tool_hint_max_length
        self._set_tool_context = set_tool_context
        self._on_iteration = on_iteration
        self._on_cache_usage = on_cache_usage
        self._stream_buf = ""
        self._think_extractor = IncrementalThinkExtractor()
        self._reasoning_open = False

    def wants_streaming(self) -> bool:
        return self._on_stream is not None

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        if not text:
            return None
        return strip_think(text) or None

    def _tool_hint(self, tool_calls: list[Any]) -> str:
        return format_tool_hints(tool_calls, max_length=self._tool_hint_max_length)

    @staticmethod
    def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
        try:
            sig = inspect.signature(cb)
        except (TypeError, ValueError):
            return False
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return True
        return name in sig.parameters

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        prev_clean = strip_think(self._stream_buf)
        self._stream_buf += delta
        new_clean = strip_think(self._stream_buf)
        incremental = new_clean[len(prev_clean) :]

        if await self._think_extractor.feed(self._stream_buf, self.emit_reasoning):
            context.streamed_reasoning = True

        if incremental:
            # Answer text has started; close the reasoning segment so the UI can
            # lock the bubble before the answer renders below it.
            await self.emit_reasoning_end()
            if self._on_stream:
                await self._on_stream(incremental)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self.emit_reasoning_end()
        if self._on_stream_end:
            await self._on_stream_end(resuming=resuming)
        self._stream_buf = ""
        self._think_extractor.reset()

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self._on_iteration:
            self._on_iteration(context.iteration)
        logger.debug(
            "Starting agent loop iteration {} for session {}",
            context.iteration,
            self._session_key,
        )

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        if self._on_progress:
            if not self._on_stream and not context.streamed_content:
                thought = self._strip_think(context.response.content if context.response else None)
                if thought:
                    await self._on_progress(thought)
            tool_hint = self._strip_think(self._tool_hint(context.tool_calls))
            tool_events = [build_tool_event_start_payload(tc) for tc in context.tool_calls]
            await invoke_on_progress(
                self._on_progress,
                tool_hint,
                tool_hint=True,
                tool_events=tool_events,
            )
        for tc in context.tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)
            logger.info("Tool call: {}({})", tc.name, args_str[:200])
        _emit_non_memory_fallback_events(context.tool_calls)
        if self._set_tool_context:
            self._set_tool_context(
                self._channel,
                self._chat_id,
                self._message_id,
                self._metadata,
                session_key=self._session_key,
            )

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """Publish a reasoning chunk; channel plugins decide whether to render."""
        if (
            self._on_progress
            and reasoning_content
            and self._on_progress_accepts(self._on_progress, "reasoning")
        ):
            self._reasoning_open = True
            await self._on_progress(reasoning_content, reasoning=True)

    async def emit_reasoning_end(self) -> None:
        """Close the current reasoning stream segment, if any was open."""
        if self._reasoning_open and self._on_progress:
            self._reasoning_open = False
            await self._on_progress("", reasoning_end=True)
        else:
            self._reasoning_open = False

    async def after_iteration(self, context: AgentHookContext) -> None:
        if (
            self._on_progress
            and context.tool_calls
            and context.tool_events
            and on_progress_accepts_tool_events(self._on_progress)
        ):
            tool_events = build_tool_event_finish_payloads(context)
            if tool_events:
                await invoke_on_progress(
                    self._on_progress,
                    "",
                    tool_hint=False,
                    tool_events=tool_events,
                )
        u = context.usage or {}
        prompt_tokens = int(u.get("prompt_tokens", 0) or 0)
        cached_tokens = int(u.get("cached_tokens", 0) or 0)
        completion_tokens = int(u.get("completion_tokens", 0) or 0)
        logger.debug(
            "LLM usage: prompt={} completion={} cached={}",
            prompt_tokens, completion_tokens, cached_tokens,
        )
        # Structured cache-savings telemetry so users can grep "how much
        # am I saving from server-side prompt caching?". Emitted every
        # turn — for providers without caching (or cold cache) the
        # ``cached_tokens`` field is 0 and the ratio is 0%, which is
        # itself useful signal (tells you the provider/model isn't
        # caching at all).
        if prompt_tokens > 0:
            ratio_pct = round(100.0 * cached_tokens / prompt_tokens, 1)
            payload = {
                "iteration": context.iteration,
                "prompt_tokens": prompt_tokens,
                "cached_tokens": cached_tokens,
                "completion_tokens": completion_tokens,
                "cache_ratio_pct": ratio_pct,
            }
            logger_obj = current_telemetry()
            if logger_obj is not None:
                with suppress(Exception):
                    logger_obj.log("cache.usage", payload)
            if self._on_cache_usage is not None:
                with suppress(Exception):
                    self._on_cache_usage(payload)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        return self._strip_think(content)


# Audit H17 (2026-05-29): per-tool-call telemetry for "non-memory
# tools used in a memory-enabled session". The bench-100 v8 analysis
# showed 27/102 traces (26%) fell back to grep / list_dir / read_file
# after the memory tools exhausted their useful results. Without this
# event there's no observable signal to track that pattern over
# time — the runtime logs it but a downstream dashboard needs a
# structured row to count.
_MEMORY_TOOLS = frozenset({
    "memory_search", "memory_drill",
    "memory_store", "memory_ingest",
})
_BENCH_RELEVANT_FALLBACK_TOOLS = frozenset({
    "grep", "list_dir", "read_file", "edit_file", "exec", "write_file",
})


def _emit_non_memory_fallback_events(tool_calls: list[Any]) -> None:
    """Emit one ``memory.fallback_tool_used`` event per non-memory
    tool call in this iteration.

    A non-memory tool call doesn't ALWAYS imply a fallback — the
    agent may legitimately need to read a config file or run a shell
    command. The event payload carries ``tool_name`` so downstream
    analysis can filter to the bench-relevant ones (grep/list_dir/
    read_file) and correlate with QAs that exhausted memory_search.

    Best-effort: failures degrade silently so the hook never breaks
    a tool dispatch.
    """
    from durin.agent.tools._telemetry import emit_tool_event

    for tc in tool_calls:
        name = getattr(tc, "name", "") or ""
        if not name or name in _MEMORY_TOOLS:
            continue
        try:
            emit_tool_event(
                "memory.fallback_tool_used",
                {
                    "tool_name": name,
                    "is_bench_relevant": (
                        name in _BENCH_RELEVANT_FALLBACK_TOOLS
                    ),
                },
            )
        except Exception:  # noqa: BLE001
            continue
