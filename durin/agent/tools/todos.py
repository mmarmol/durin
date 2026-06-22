"""``todo_write`` tool — flat todo list maintained by the agent.

The model uses a single tool to manage a per-session todo list. Each call
REPLACES the entire list (Claude Code's TodoWrite contract) — keeping the
mutation primitive flat removes a class of bugs around stale partial
updates and makes the runtime-context echo deterministic.

Each item has:

    {
        "content":    str — imperative ("Implement parser")
        "status":     "pending" | "in_progress" | "completed"
        "activeForm": str — present-continuous ("Implementing parser")
    }

The list is mirrored back into the model's runtime context each turn via
:func:`durin.session.todo_state.todos_runtime_lines`, so the model cannot
lose track of it across compaction. The tool result also includes the
fully-rendered checklist so the user (via the assistant's next message)
can see the new state without an extra round-trip.

Design notes:

- One tool, full replacement: simpler than ``TodoAdd``/``TodoUpdate``/
  ``TodoComplete`` triplets, easier for small models to use correctly,
  and matches the reference pattern adopted by other coding agents.
- No persistence beyond the session: todos live on
  ``session.metadata[TODOS_KEY]``, so they survive compaction (the meta
  bag is preserved) but are not exposed via the on-disk session meta
  yet. Memory subsystem (Phase 2) can promote completed todos into the
  per-session history file if we decide that's the right substrate.
- Tool is allowed in plan mode: maintaining a checklist while
  investigating is read-safe; the storage write is to in-memory session
  metadata, not the workspace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import (
    ArraySchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.session.todo_state import (
    TODOS_KEY,
    parse_todos,
    render_todos_markdown,
)

if TYPE_CHECKING:
    from durin.session.manager import SessionManager


_TODO_ITEM_SCHEMA = ObjectSchema(
    properties={
        "content": StringSchema(
            "Imperative description of the task (e.g. 'Run the tests'). "
            "Should read as something to do, not as narration.",
            min_length=1,
            max_length=400,
        ),
        "status": StringSchema(
            "Current state of the item.",
            enum=("pending", "in_progress", "completed"),
        ),
        "activeForm": StringSchema(
            "Present-continuous form shown while the item is in_progress "
            "(e.g. 'Running the tests'). The CLI/UI uses this verbatim.",
            min_length=1,
            max_length=400,
        ),
    },
    required=["content", "status", "activeForm"],
    additional_properties=False,
)


@tool_parameters(
    tool_parameters_schema(
        todos=ArraySchema(
            items=_TODO_ITEM_SCHEMA,
            description=(
                "The complete new todo list. This REPLACES the prior list "
                "in full — include every item you still want tracked, "
                "including completed ones (so the user sees what just "
                "shipped). Exactly one item should be 'in_progress' while "
                "you are actively working; mark items 'completed' the "
                "moment they finish, not at the end of a batch."
            ),
            max_items=50,
        ),
        required=["todos"],
    )
)
class TodoWriteTool(Tool, ContextAware):
    """Replace the session's todo list with the provided items."""

    _scopes = {"core"}

    def __init__(self, sessions: "SessionManager") -> None:
        self._sessions = sessions
        self._request_ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sessions = getattr(ctx, "sessions", None)
        assert sessions is not None  # guarded by enabled()
        return cls(sessions=sessions)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Replace the agent's flat todo list with the provided items. "
            "Use this to plan multi-step work, track progress across "
            "tool calls, and signal what you're currently doing. Each "
            "call REPLACES the full list — include every item you want "
            "kept (including completed ones), keep exactly one item as "
            "'in_progress' while working, and mark items 'completed' "
            "the moment they finish. The list is echoed in your runtime "
            "context each turn so it survives compaction. Skip the tool "
            "for trivial one-step requests; reach for it when there are "
            "≥3 distinct steps, the user asks for tracking, or you've "
            "just received a plan to execute."
        )

    def _session(self) -> Any | None:
        if self._request_ctx is None:
            return None
        key = self._request_ctx.session_key
        if not key:
            return None
        return self._sessions.get_or_create(key)

    async def execute(self, todos: list[Any] | None = None, **kwargs: Any) -> str:
        session = self._session()
        if session is None or session.metadata is None:
            return (
                "Error: todo_write requires an active chat session "
                "(missing routing context)."
            )

        normalized = parse_todos(todos)
        if normalized is None:
            return (
                "Error: `todos` must be a list of objects with "
                "`content`, `status`, and `activeForm` fields."
            )

        # Enforce the "at most one in_progress" contract softly — keep
        # only the first in_progress entry, demote the rest to pending.
        # We log this rather than rejecting so the model gets useful
        # output even if it slipped.
        in_progress_count = sum(1 for t in normalized if t["status"] == "in_progress")
        coerced = False
        if in_progress_count > 1:
            seen_active = False
            for t in normalized:
                if t["status"] == "in_progress":
                    if seen_active:
                        t["status"] = "pending"
                        coerced = True
                    else:
                        seen_active = True

        session.metadata[TODOS_KEY] = normalized
        self._sessions.save(session)

        # Counts let dashboards see how the todo list evolved — useful
        # for spotting "model creates 10 todos but only marks 1 done"
        # patterns.
        status_counts = {
            "pending": sum(1 for t in normalized if t["status"] == "pending"),
            "in_progress": sum(1 for t in normalized if t["status"] == "in_progress"),
            "completed": sum(1 for t in normalized if t["status"] == "completed"),
        }
        emit_tool_event("tool.todo_write", {
            "total": len(normalized),
            "pending": status_counts["pending"],
            "in_progress": status_counts["in_progress"],
            "completed": status_counts["completed"],
            "coerced_multiple_in_progress": coerced,
        })

        rendered = render_todos_markdown(normalized)
        suffix = ""
        if coerced:
            suffix = (
                "\n\n_Note: multiple items were marked `in_progress`; "
                "only the first kept that status, the rest moved to "
                "`pending`. Keep exactly one item active at a time._"
            )
        # The channel renders the checklist from the tool arguments (webui
        # block / TUI bubble) — no re-presentation instruction needed. The
        # rendered list stays in the result for the model's own context.
        return f"Todo list updated:\n\n{rendered}{suffix}"
