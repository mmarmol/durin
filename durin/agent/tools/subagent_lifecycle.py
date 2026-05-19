"""Subagent lifecycle tools — list, status, stop, output.

Companion to :class:`durin.agent.tools.spawn.SpawnTool`. Once the model
has spawned a background task, these tools let it observe and steer the
task without waiting for the synthetic announcement to land:

- ``subagent_list``   — what's running / recently finished in this session
- ``subagent_status`` — detailed phase / iteration / tool history for one
- ``subagent_stop``   — cancel a running subagent
- ``subagent_output`` — fetch the final (or partial) output of a task

Security boundary
-----------------

Every operation is scoped by ``session_key``. A subagent is reachable
only by the conversation that spawned it; cross-session lookup returns
``"unknown"`` even when the id exists in another session. The model
therefore cannot snoop on other conversations' tasks even if it
manages to enumerate plausible task ids.

Why retention?
--------------

The manager keeps the last ``_max_status_history`` statuses (100 by
default) after completion so the model can still ask "what did task X
return?" some turns later — useful when a long-running subagent
finishes during another tool call and the announce arrives interleaved.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from durin.agent.subagent import SubagentManager


_MAX_TOOL_HISTORY_PER_STATUS = 8
_MAX_FINAL_PREVIEW = 4000


def _format_age(started_at: float, ended_at: float | None) -> str:
    end = ended_at if ended_at is not None else time.monotonic()
    age_s = max(0.0, end - started_at)
    if age_s < 60:
        return f"{age_s:.1f}s"
    if age_s < 3600:
        return f"{age_s / 60:.1f}m"
    return f"{age_s / 3600:.1f}h"


def _state_label(status: Any, is_running: bool) -> str:
    """Compact one-word state for list rendering."""
    if is_running:
        return "running"
    if status.error or status.stop_reason in ("error", "tool_error"):
        return "error"
    if status.stop_reason == "cancelled":
        return "cancelled"
    if status.phase == "done":
        return "done"
    return status.phase or "unknown"


class _SubagentToolBase(ContextAware):
    """Shared SubagentManager handle + session_key resolution.

    ``create`` and ``enabled`` are deliberately NOT defined here — the
    MRO for ``class XxxTool(Tool, _SubagentToolBase)`` would still pick
    up ``Tool``'s defaults instead. Each concrete subclass defines them
    explicitly. Same pattern as :mod:`durin.agent.tools.long_task`.
    """

    def __init__(self, manager: "SubagentManager") -> None:
        self._manager = manager
        self._request_ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    def _session_key(self) -> str | None:
        ctx = self._request_ctx
        if ctx is None:
            return None
        if ctx.session_key:
            return ctx.session_key
        if ctx.channel and ctx.chat_id:
            return f"{ctx.channel}:{ctx.chat_id}"
        return None


@tool_parameters(tool_parameters_schema(required=[]))
class SubagentListTool(Tool, _SubagentToolBase):
    """List all subagents spawned by this session (running + recent history)."""

    _scopes = {"core"}

    def __init__(self, manager: "SubagentManager") -> None:
        _SubagentToolBase.__init__(self, manager)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "subagent_manager", None) is not None

    @property
    def name(self) -> str:
        return "subagent_list"

    @property
    def description(self) -> str:
        return (
            "List subagents this session has spawned, including recently "
            "finished ones. Returns id, label, state, iteration, age, and "
            "tool-call count for each. Use to check what's still running "
            "before spawning more, or to find an id you forgot."
        )

    async def execute(self, **kwargs: Any) -> str:
        sess_key = self._session_key()
        if sess_key is None:
            return "Error: no session context available for subagent_list."
        statuses = self._manager.list_for_session(sess_key)
        if not statuses:
            return "No subagents have been spawned in this session."

        running = sum(1 for s in statuses if self._manager._is_running(s.task_id))
        header = (
            f"{len(statuses)} subagent(s) in this session "
            f"({running} running, {len(statuses) - running} finished):"
        )
        lines = [header]
        for s in statuses:
            is_running = self._manager._is_running(s.task_id)
            state = _state_label(s, is_running)
            age = _format_age(s.started_at, s.ended_at)
            tool_calls = len(s.tool_events or [])
            lines.append(
                f"  [{s.task_id}] {state:<9}  iter={s.iteration:<2}  "
                f"tools={tool_calls:<2}  age={age:<6}  {s.label}"
            )
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema(
            description=(
                "The id returned by `spawn` (or shown in `subagent_list`)."
            ),
            min_length=1,
            max_length=64,
        ),
        required=["task_id"],
    )
)
class SubagentStatusTool(Tool, _SubagentToolBase):
    """Detailed status snapshot for one subagent."""

    _scopes = {"core"}

    def __init__(self, manager: "SubagentManager") -> None:
        _SubagentToolBase.__init__(self, manager)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "subagent_manager", None) is not None

    @property
    def name(self) -> str:
        return "subagent_status"

    @property
    def description(self) -> str:
        return (
            "Get a detailed status snapshot for one subagent (phase, "
            "iteration count, recent tool calls, token usage, error if "
            "any). Use to check on a single task in flight — for the "
            "full list use `subagent_list`."
        )

    async def execute(self, task_id: str | None = None, **kwargs: Any) -> str:
        sess_key = self._session_key()
        if sess_key is None:
            return "Error: no session context available."
        if not task_id:
            return "Error: `task_id` is required."
        status = self._manager.get_status_for(task_id, sess_key)
        if status is None:
            return f"Error: unknown task_id {task_id!r} in this session."

        is_running = self._manager._is_running(task_id)
        state = _state_label(status, is_running)
        age = _format_age(status.started_at, status.ended_at)
        out: list[str] = [
            f"Subagent [{status.task_id}] — {status.label}",
            f"  state:     {state}",
            f"  phase:     {status.phase}",
            f"  iteration: {status.iteration}",
            f"  age:       {age}",
        ]
        if status.usage:
            usage_parts = ", ".join(
                f"{k}={v}" for k, v in sorted(status.usage.items())
            )
            out.append(f"  usage:     {usage_parts}")
        if status.tool_events:
            tail = status.tool_events[-_MAX_TOOL_HISTORY_PER_STATUS:]
            out.append(f"  tool calls ({len(status.tool_events)} total, showing last {len(tail)}):")
            for ev in tail:
                if not isinstance(ev, dict):
                    continue
                name = ev.get("name", "?")
                st = ev.get("status", "?")
                detail = (ev.get("detail") or "").replace("\n", " ").strip()
                if len(detail) > 80:
                    detail = detail[:77] + "..."
                out.append(f"    - {name} [{st}] {detail}")
        if status.error:
            out.append(f"  error:     {status.error[:200]}")
        if not is_running and status.stop_reason:
            out.append(f"  stop:      {status.stop_reason}")
        return "\n".join(out)


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema(
            description="The id of the subagent to cancel.",
            min_length=1,
            max_length=64,
        ),
        required=["task_id"],
    )
)
class SubagentStopTool(Tool, _SubagentToolBase):
    """Cancel a running subagent."""

    _scopes = {"core"}

    def __init__(self, manager: "SubagentManager") -> None:
        _SubagentToolBase.__init__(self, manager)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "subagent_manager", None) is not None

    @property
    def name(self) -> str:
        return "subagent_stop"

    @property
    def description(self) -> str:
        return (
            "Cancel a running subagent by id. Returns a clear status: "
            "'stopped' if it was running, 'not_running' if it had already "
            "finished, 'unknown' if no such task in this session. The "
            "subagent's announce message will still arrive (with a "
            "cancellation note); the cancel is best-effort."
        )

    async def execute(self, task_id: str | None = None, **kwargs: Any) -> str:
        sess_key = self._session_key()
        if sess_key is None:
            return "Error: no session context available."
        if not task_id:
            return "Error: `task_id` is required."
        outcome = await self._manager.stop_task(task_id, sess_key)
        if outcome == "stopped":
            return f"Subagent [{task_id}] cancelled."
        if outcome == "not_running":
            return f"Subagent [{task_id}] had already finished — nothing to cancel."
        return f"Error: unknown task_id {task_id!r} in this session."


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema(
            description="The id of the subagent to monitor.",
            min_length=1,
            max_length=64,
        ),
        after_event=IntegerSchema(
            description=(
                "Skip events at indices < this number — pass the "
                "``next_cursor`` returned by the previous monitor call "
                "to receive only what's new since you last polled. "
                "Defaults to 0 (receive everything)."
            ),
            minimum=0,
            nullable=True,
        ),
        required=["task_id"],
    )
)
class SubagentMonitorTool(Tool, _SubagentToolBase):
    """Incremental progress poll for a subagent (diff against a cursor)."""

    _scopes = {"core"}

    def __init__(self, manager: "SubagentManager") -> None:
        _SubagentToolBase.__init__(self, manager)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "subagent_manager", None) is not None

    @property
    def name(self) -> str:
        return "subagent_monitor"

    @property
    def description(self) -> str:
        return (
            "Poll a subagent for progress incrementally. Returns the "
            "current phase, iteration, and **only the tool events new "
            "since `after_event`** (pass the prior call's `next_cursor` "
            "to skip what you already saw). When the subagent has "
            "finished, the response also includes its final output, so "
            "a single follow-up call wraps things up without needing "
            "subagent_output. Use this in a poll-sleep-poll pattern for "
            "long background tasks; for a single snapshot, use "
            "`subagent_status` instead."
        )

    async def execute(
        self,
        task_id: str | None = None,
        after_event: int | None = None,
        **kwargs: Any,
    ) -> str:
        sess_key = self._session_key()
        if sess_key is None:
            return "Error: no session context available."
        if not task_id:
            return "Error: `task_id` is required."
        cursor = int(after_event or 0)
        info = self._manager.monitor_since(task_id, sess_key, after_event=cursor)
        if info is None:
            return f"Error: unknown task_id {task_id!r} in this session."

        state = "running" if info["is_running"] else "finished"
        header = (
            f"Subagent [{task_id}] — {info['label']}\n"
            f"  state:        {state}\n"
            f"  phase:        {info['phase']}\n"
            f"  iteration:    {info['iteration']}\n"
            f"  events_total: {info['events_total']}\n"
            f"  next_cursor:  {info['next_cursor']}"
        )
        lines = [header]
        events_since = info["events_since"] or []
        if events_since:
            lines.append(f"  new events since cursor={cursor} ({len(events_since)}):")
            for ev in events_since[-_MAX_TOOL_HISTORY_PER_STATUS:]:
                if not isinstance(ev, dict):
                    continue
                name = ev.get("name", "?")
                st = ev.get("status", "?")
                detail = (ev.get("detail") or "").replace("\n", " ").strip()
                if len(detail) > 80:
                    detail = detail[:77] + "..."
                lines.append(f"    - {name} [{st}] {detail}")
        else:
            lines.append("  no new events since the previous cursor.")
        if info["finished"]:
            stop = info.get("stop_reason") or "completed"
            lines.append(f"\n  finished (stop_reason={stop}).")
            final = info.get("final_content") or info.get("error") or "(no output recorded)"
            if len(final) > _MAX_FINAL_PREVIEW:
                final = final[: _MAX_FINAL_PREVIEW - 80].rstrip() + (
                    f"\n… (truncated; use subagent_output for the "
                    f"complete {len(final)}-char response.)"
                )
            lines.append(f"  final output:\n{final}")
        return "\n".join(lines)


@tool_parameters(
    tool_parameters_schema(
        task_id=StringSchema(
            description="The id of the subagent whose output you want.",
            min_length=1,
            max_length=64,
        ),
        required=["task_id"],
    )
)
class SubagentOutputTool(Tool, _SubagentToolBase):
    """Fetch the final (or partial) output of a subagent."""

    _scopes = {"core"}

    def __init__(self, manager: "SubagentManager") -> None:
        _SubagentToolBase.__init__(self, manager)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "subagent_manager", None) is not None

    @property
    def name(self) -> str:
        return "subagent_output"

    @property
    def description(self) -> str:
        return (
            "Return the final (or partial-on-error) output of a subagent "
            "by id. Useful when a subagent finished during another tool "
            "call and you want the result text without waiting for the "
            "announce. If the subagent is still running, returns 'still "
            "running' — call `subagent_status` to check progress."
        )

    async def execute(self, task_id: str | None = None, **kwargs: Any) -> str:
        sess_key = self._session_key()
        if sess_key is None:
            return "Error: no session context available."
        if not task_id:
            return "Error: `task_id` is required."
        info = self._manager.get_output_for(task_id, sess_key)
        if info is None:
            return f"Error: unknown task_id {task_id!r} in this session."
        if info["is_running"]:
            return f"Subagent [{task_id}] is still running (phase: {info['phase']})."
        final = info["final_content"] or info["error"] or "(no output recorded)"
        if len(final) > _MAX_FINAL_PREVIEW:
            final = final[: _MAX_FINAL_PREVIEW - 80].rstrip() + (
                f"\n… (truncated; full output was {len(final)} chars; "
                "use the subagent's announce message for the complete text.)"
            )
        header = f"Subagent [{task_id}] — stop_reason={info['stop_reason'] or 'completed'}:"
        return f"{header}\n\n{final}"
