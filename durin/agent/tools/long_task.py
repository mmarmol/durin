"""Sustained goal tools on the main agent (Codex-style).

The essential rules — call promptly, and phrase the goal idempotently (end-state +
done-ness, self-contained, safe to re-read after compaction) — are stated inline in the
tool descriptions, so the tools are self-sufficient. The built-in **long-goal** skill is
an OPTIONAL deeper reference (project-shaped work, research-first); it is not required to
use these tools.

``long_task`` registers an objective on the session (JSON-serializable metadata).
Objectives are mirrored each turn into the Runtime Context block (see
``durin.session.goal_state.goal_state_runtime_lines``) so compaction cannot hide them —
an active goal in full, a completed one as a short ``Goal (completed)`` / ``Outcome``
trace, because a session rarely ends when its goal does and the objective is exactly
the context that must not be dropped while work continues.
Work proceeds in ordinary agent turns (same runner, compaction as configured).
Call ``complete_goal`` when the sustained objective should stop being tracked:
finished successfully, or cancelled / superseded / redirected—in every case the recap should match reality.

There is **no** sub-agent orchestrator and **no** special WebSocket ``agent_ui`` stream.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from durin.bus.events import OutboundMessage
from durin.session.decision_log import add_decision
from durin.session.goal_state import (
    GOAL_STATE_KEY,
    discard_legacy_goal_state_key,
    goal_headline,
    goal_state_raw,
    goal_state_ws_blob,
    parse_goal_state,
)

if TYPE_CHECKING:
    from durin.session.manager import SessionManager


def _iso_now() -> str:
    return datetime.now().isoformat()


class _GoalToolsMixin(ContextAware):
    """Shared routing context + Session lookup."""

    def __init__(
        self,
        sessions: SessionManager,
        bus: Any | None = None,
        decision_log_max_entries: int | None = None,
        decision_log_max_chars: int | None = None,
    ) -> None:
        self._sessions = sessions
        self._bus = bus
        self._request_ctx: RequestContext | None = None
        # The finished-goal breadcrumb lands in the decision log, so it has to
        # respect the same configured caps note_decision does.
        self._decision_max_entries = decision_log_max_entries
        self._decision_max_chars = decision_log_max_chars

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    def _session(self):
        if self._request_ctx is None:
            return None
        key = self._request_ctx.session_key
        if not key:
            return None
        return self._sessions.get_or_create(key)

    def _record_completed_goal(self, sess: Any, goal: dict[str, Any]) -> None:
        """Leave a compact trace of a finished goal in the decision log.

        Called when the goal blob is about to be replaced — the decision log
        survives that, the blob does not. Uses the same headline the runtime
        anchor renders so the two can never describe the same goal differently.
        """
        headline = goal_headline(goal)
        recap = str(goal.get("recap") or "").strip()
        if not (headline or recap):
            return
        caps: dict[str, int] = {}
        if self._decision_max_entries is not None:
            caps["max_entries"] = self._decision_max_entries
        if self._decision_max_chars is not None:
            caps["max_chars"] = self._decision_max_chars
        add_decision(
            sess.metadata,
            f"Goal completed — {headline or 'goal'}"
            + (f". Outcome: {recap}" if recap else "."),
            source="auto",
            ts=str(goal.get("completed_at") or ""),
            **caps,
        )

    async def _publish_goal_state_ws(self, metadata: dict[str, Any]) -> None:
        """Fan-out authoritative goal snapshot for this WebSocket chat only."""
        bus = self._bus
        rc = self._request_ctx
        if bus is None or rc is None or rc.channel != "websocket":
            return
        cid = (rc.chat_id or "").strip()
        if not cid:
            return
        await bus.publish_outbound(
            OutboundMessage(
                channel="websocket",
                chat_id=cid,
                content="",
                metadata={
                    "_goal_state_sync": True,
                    "goal_state": goal_state_ws_blob(metadata),
                },
            ),
        )


@tool_parameters(
    tool_parameters_schema(
        goal=StringSchema(
            "Sustained objective for this chat thread. Call this PROMPTLY once the user's intent is "
            "clear — do not delay it to over-plan, research, or decide execution details. Write the "
            "goal so it survives compaction and resume (it is re-read cold across turns): state the "
            "desired END STATE plus how you will know it is done, not fragile step-by-step narration; "
            "repeat the constraints that matter (paths, names, counts, scope in/out); and phrase any "
            "mutation as check-then-act or 'ensure …' so re-reading the goal never triggers duplicate "
            "or destructive work. (The built-in long-goal skill has optional deeper guidance — "
            "project-shaped work, research-first — but is not required.)",
            max_length=12_000,
        ),
        ui_summary=StringSchema(
            "Optional one-line label for session lists / logs (≤120 chars).",
            max_length=120,
            nullable=True,
        ),
        max_turns=IntegerSchema(
            description=(
                "Optional turn budget for this goal. Progress is mirrored "
                "in Runtime Context as 'Turn budget: used/max'; when "
                "exceeded you are reminded to wrap up via complete_goal "
                "or renegotiate with the user. Surfacing only — nothing "
                "is blocked."
            ),
            minimum=1,
            nullable=True,
        ),
        required=["goal"],
    )
)
class LongTaskTool(Tool, _GoalToolsMixin):
    """Begin or replace focus on a long-running objective stored on the session."""

    def __init__(
        self,
        sessions: Any,
        bus: Any | None = None,
        decision_log_max_entries: int | None = None,
        decision_log_max_chars: int | None = None,
    ) -> None:
        _GoalToolsMixin.__init__(
            self, sessions, bus, decision_log_max_entries, decision_log_max_chars,
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sess = getattr(ctx, "sessions", None)
        assert sess is not None  # guarded by enabled()
        defaults = getattr(getattr(getattr(ctx, "app_config", None), "agents", None), "defaults", None)
        return cls(
            sessions=sess,
            bus=getattr(ctx, "bus", None),
            decision_log_max_entries=getattr(defaults, "decision_log_max_entries", None),
            decision_log_max_chars=getattr(defaults, "decision_log_max_chars", None),
        )

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "long_task"

    @property
    def description(self) -> str:
        return (
            "Mark this thread as a sustained long-running task, kept in focus across turns: the active "
            "goal is mirrored in Runtime Context every turn and survives compaction. Call this as soon "
            "as the user's intent is clear — write a good idempotent goal (see the goal parameter for "
            "how), but do not delay the call with long planning or research. Use normal tools until "
            "done, then call complete_goal when the objective is satisfied, cancelled, or replaced. "
            "If a goal is already active, finish it or call complete_goal before registering another."
        )

    async def execute(
        self,
        goal: str,
        ui_summary: str | None = None,
        max_turns: int | None = None,
        **kwargs: Any,
    ) -> str:
        sess = self._session()
        if sess is None:
            return (
                "Error: long_task requires an active chat session (missing routing context)."
            )
        prior = parse_goal_state(goal_state_raw(sess.metadata))
        if isinstance(prior, dict) and prior.get("status") == "active":
            return (
                "Error: a sustained goal is already active. "
                "Use complete_goal when finished, or ask the user before replacing it."
            )

        # A finished goal keeps rendering in the task-state anchor until this
        # overwrite drops it. Leave a compact breadcrumb in the decision log on
        # the way out, so what the session already accomplished is not silently
        # replaced. Written here rather than at completion time because until
        # this moment the anchor still carries the full text — recording it
        # earlier would duplicate it in every prompt.
        if isinstance(prior, dict) and prior.get("status") == "completed":
            self._record_completed_goal(sess, prior)

        summary = (ui_summary or "").strip()[:120]
        blob = {
            "status": "active",
            "objective": goal.strip(),
            "ui_summary": summary,
            "started_at": _iso_now(),
        }
        if isinstance(max_turns, int) and max_turns > 0:
            blob["max_turns"] = max_turns
            blob["turns_used"] = 0
        sess.metadata[GOAL_STATE_KEY] = blob
        discard_legacy_goal_state_key(sess.metadata)
        self._sessions.save(sess)
        await self._publish_goal_state_ws(sess.metadata)
        extra = f"\nSummary line: {summary}" if summary else ""
        return (
            "Goal recorded. Keep working toward the objective using ordinary tools. "
            "When fully done (verified against what was asked), call complete_goal with a "
            f"short recap.{extra}"
        )


@tool_parameters(
    tool_parameters_schema(
        recap=StringSchema(
            "Brief recap for the user (plain text). When the goal succeeded, confirm outcomes; "
            "if the user cancelled, pivoted, or replaced the objective, say so honestly.",
            max_length=8000,
            nullable=True,
        ),
        required=[],
    )
)
class CompleteGoalTool(Tool, _GoalToolsMixin):
    """Mark the active sustained goal finished after all required work is verified."""

    def __init__(self, sessions: Any, bus: Any | None = None) -> None:
        _GoalToolsMixin.__init__(self, sessions, bus)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sess = getattr(ctx, "sessions", None)
        assert sess is not None
        return cls(sessions=sess, bus=getattr(ctx, "bus", None))

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "complete_goal"

    @property
    def description(self) -> str:
        return (
            "End bookkeeping for the active sustained goal. "
            "Use when the objective is fully achieved and verified—recap what was delivered. "
            "Also call when the user cancels, redirects, or replaces the goal: recap must reflect "
            "what actually happened (not necessarily success). "
            "If no goal is active, the tool reports that and leaves metadata unchanged."
        )

    async def execute(self, recap: str | None = None, **kwargs: Any) -> str:
        sess = self._session()
        if sess is None:
            return "Error: complete_goal requires an active chat session."
        prior = parse_goal_state(goal_state_raw(sess.metadata))
        if not isinstance(prior, dict) or prior.get("status") != "active":
            return "No active goal to complete."

        ended = _iso_now()
        sess.metadata[GOAL_STATE_KEY] = {
            **prior,
            "status": "completed",
            "completed_at": ended,
            "recap": (recap or "").strip(),
        }
        # The objective is NOT copied elsewhere here: a completed goal keeps
        # rendering in the task-state anchor, so it stays in context. The
        # decision-log breadcrumb is written later, by long_task, at the moment
        # the blob is actually replaced.
        discard_legacy_goal_state_key(sess.metadata)
        self._sessions.save(sess)
        await self._publish_goal_state_ws(sess.metadata)
        tail = (recap or "").strip()
        if tail:
            return f"Goal marked complete ({ended}). Recap:\n{tail}"
        return f"Goal marked complete ({ended})."

