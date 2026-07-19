"""Spawn tool for creating background subagents."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

if TYPE_CHECKING:
    from durin.agent.subagent import SubagentManager


@tool_parameters(
    tool_parameters_schema(
        task=StringSchema("The task for the subagent to complete"),
        label=StringSchema("Optional short label for the task (for display)"),
        required=["task"],
    )
)
class SpawnTool(Tool, ContextAware):
    """Tool to spawn a subagent for background task execution."""

    # Core-only: nested spawn would allow unbounded recursive fan-out.
    _scopes = {"core"}

    def __init__(self, manager: "SubagentManager"):
        self._manager = manager
        self._origin_channel: ContextVar[str] = ContextVar("spawn_origin_channel", default="cli")
        self._origin_chat_id: ContextVar[str] = ContextVar("spawn_origin_chat_id", default="direct")
        self._session_key: ContextVar[str] = ContextVar("spawn_session_key", default="cli:direct")
        self._origin_message_id: ContextVar[str | None] = ContextVar(
            "spawn_origin_message_id",
            default=None,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(manager=ctx.subagent_manager)

    def set_context(self, ctx: RequestContext) -> None:
        """Set the origin context for subagent announcements."""
        self._origin_channel.set(ctx.channel)
        self._origin_chat_id.set(ctx.chat_id)
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")
        self._origin_message_id.set(ctx.message_id)

    @property
    def launches_background(self) -> bool:
        # A spawn call returns immediately while the subagent runs concurrently
        # in the background, so it counts as parallelism in turn telemetry.
        return True

    @property
    def name(self) -> str:
        return "spawn"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to handle a task in the background. "
            "Use this for complex or time-consuming tasks that can run independently. "
            "The subagent will complete the task and report back when done. "
            "When you have two or more independent tasks, spawn them together — "
            "emit several spawn calls in the same turn — so the subagents run in "
            "parallel rather than one after another. For example, kick off three "
            "separate research threads at once instead of waiting for each to "
            "finish before starting the next. "
            "For deliverables or existing projects, inspect the workspace first "
            "and use a dedicated subdirectory when helpful. "
            "The subagent gets the standard background tool set: files, shell, "
            "search, web, memory search and memory writes (entity upsert, "
            "document ingest), plus the vision/audio interpretation bridges "
            "when aux models are configured. It has NO interactive or "
            "orchestration tools — it cannot ask the user questions, send "
            "channel messages, spawn further subagents, or run workflows — so "
            "do not delegate work that needs those; it also inherits your "
            "current mode's tool restrictions (e.g. plan mode stays read-only)."
        )

    async def execute(self, task: str, label: str | None = None, **kwargs: Any) -> str:
        """Spawn a subagent to execute the given task."""
        running = self._manager.get_running_count()
        limit = self._manager.max_concurrent_subagents
        if running >= limit:
            return (
                f"Cannot spawn subagent: concurrency limit reached "
                f"({running}/{limit} running). Wait for a running subagent "
                f"to complete before spawning a new one."
            )
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=self._origin_channel.get(),
            origin_chat_id=self._origin_chat_id.get(),
            session_key=self._session_key.get(),
            origin_message_id=self._origin_message_id.get(),
        )
