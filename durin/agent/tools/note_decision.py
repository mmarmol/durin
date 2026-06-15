"""``note_decision`` tool — append a key decision/finding to the task-state anchor.

Real-time writer for durin/session/decision_log.py. The auto-extraction at
compaction (durin/agent/memory.py) is the floor; this tool lets the model flag
decisions as it makes them. Both go through ``add_decision`` (dedup + cap).
Caps + the enable toggle come from ``AgentDefaults`` via ``ctx.app_config``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.session.decision_log import (
    _DEFAULT_MAX_CHARS,
    _DEFAULT_MAX_ENTRIES,
    add_decision,
)

if TYPE_CHECKING:
    from durin.session.manager import SessionManager


def _defaults(ctx: Any) -> Any | None:
    app_config = getattr(ctx, "app_config", None)
    if app_config is None:
        return None
    return getattr(getattr(app_config, "agents", None), "defaults", None)


@tool_parameters(
    tool_parameters_schema(
        text=StringSchema(
            "The decision or finding to remember (one sentence; include the "
            "why when it matters). E.g. 'Chose separate extract_decisions call "
            "to avoid degrading the facts-extraction prompt'.",
            min_length=1,
            max_length=400,
        ),
        required=["text"],
    )
)
class NoteDecisionTool(Tool, ContextAware):
    """Append a key decision/finding to the durable task-state anchor."""

    _scopes = {"core"}

    def __init__(self, sessions: "SessionManager", max_entries: int, max_chars: int) -> None:
        self._sessions = sessions
        self._max_entries = max_entries
        self._max_chars = max_chars
        self._request_ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    @classmethod
    def create(cls, ctx: Any) -> "NoteDecisionTool":
        sessions = getattr(ctx, "sessions", None)
        assert sessions is not None  # guarded by enabled()
        defaults = _defaults(ctx)
        return cls(
            sessions=sessions,
            max_entries=getattr(defaults, "decision_log_max_entries", _DEFAULT_MAX_ENTRIES),
            max_chars=getattr(defaults, "decision_log_max_chars", _DEFAULT_MAX_CHARS),
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        if getattr(ctx, "sessions", None) is None:
            return False
        defaults = _defaults(ctx)
        return bool(getattr(defaults, "decision_log_enabled", True))

    @property
    def name(self) -> str:
        return "note_decision"

    @property
    def description(self) -> str:
        return (
            "Record a key decision or critical finding for the current task "
            "(one sentence; include the why). It is echoed in your runtime "
            "context every turn and survives compaction, so you won't lose "
            "track of *why* you did things when older messages scroll out of "
            "context. Use it when you make a non-obvious choice, discover an "
            "important constraint/fact, or hit a blocker — not for routine "
            "progress (that's the todo list)."
        )

    def _session(self) -> Any | None:
        if self._request_ctx is None:
            return None
        key = self._request_ctx.session_key
        if not key:
            return None
        return self._sessions.get_or_create(key)

    async def execute(self, text: str | None = None, **kwargs: Any) -> str:
        session = self._session()
        if session is None or session.metadata is None:
            return "Error: note_decision requires an active chat session."
        text = str(text or "").strip()
        if not text:
            return "Error: `text` must be a non-empty string."
        entries, dropped = add_decision(
            session.metadata,
            text,
            source="tool",
            ts=datetime.now(timezone.utc).isoformat(),
            max_entries=self._max_entries,
            max_chars=self._max_chars,
        )
        self._sessions.save(session)
        emit_tool_event("tool.note_decision", {"total": len(entries)})
        if dropped:
            emit_tool_event("decision_log.capped", {"dropped": dropped, "source": "tool"})
        return f"Recorded. Task-state decisions: {len(entries)}."
