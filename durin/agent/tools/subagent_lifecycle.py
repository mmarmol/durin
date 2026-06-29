"""Sub-agent drill-in tools — monitor, output.

Companion to :class:`durin.agent.tools.spawn.SpawnTool` and the unified
``tasks`` tool. Listing, single-snapshot status, and cancellation of background
work (sub-agents and workflow runs) live on ``tasks``; these two are the
sub-agent-specific affordances that have no cross-type analog:

- ``subagent_monitor`` — incremental progress poll (diff against a cursor), for
  a poll-sleep-poll loop on a long sub-agent.
- ``subagent_output`` — the full (or partial-on-error) final output text.

Security boundary
-----------------

Every operation is scoped by ``session_key``. A sub-agent is reachable only by
the conversation that spawned it; a cross-session lookup returns ``"unknown"``
even when the id exists in another session.

Why retention?
--------------

The manager keeps the last ``_max_status_history`` statuses (100 by default)
after completion so the model can still ask "what did task X return?" some turns
later — useful when a long-running sub-agent finishes during another tool call
and the announce arrives interleaved.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from durin.agent.subagent import SubagentManager


_MAX_TOOL_HISTORY_PER_STATUS = 8
_MAX_FINAL_PREVIEW = 4000


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
            "`tasks(action='status', id=...)` instead."
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
            "running' — call `tasks(action='status', id=...)` to check progress."
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
