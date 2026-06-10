"""Plan mode tools — Sprint B / L3 (docs/architecture/loop.md §3).

Two LLM-callable tools that work with the agent-mode system in
``durin/agent/agent_mode.py``:

- ``enter_plan_mode`` — the model can voluntarily switch into plan mode if it
  decides it needs to think more carefully. Equivalent to the user running
  ``/plan``. Most of the time the user activates plan mode via the slash
  command — this tool exists so frontier models that internalize "let me
  plan first" can also do it from within a turn.

- ``exit_plan_mode`` — the model calls this when it has a complete plan
  ready for user review. The plan is **written to disk** under
  ``<workspace>/.durin/plans/plan_<timestamp>.md`` and the path is exposed
  in the tool result. The session REMAINS in plan mode until the user runs
  ``/build`` to approve. While in plan, the user can open the plan file,
  edit it freely (e.g. tweak step 3), then run ``/build`` — the agent
  resumes with the edited file. This is the human-in-the-loop gate,
  channel-agnostic (no UI dialog needed), and supports edit-before-approve.

Design notes (file-based plan storage):

The original Sprint B used the ``plan`` argument as the message body of the
tool result, with no disk persistence. That MVP traded daily-driver
ergonomics for ~3h of implementation simplicity (see logbook entry "Plan
storage redesign — May 2026" for the post-mortem). File-based gives:

- Persistence across context compaction
- Edit-before-approve via any editor (plan is a normal markdown file)
- Multi-turn refinement (model can read & re-write the file)
- Post-mortem review (``ls .durin/plans/`` shows history)
- Token efficiency (plan lives on disk, not in message history)

The naming mirrors Claude Code / OpenClaude so the model recognizes the
pattern from its training. Differences from Claude Code: (a) ``/build`` is
the approval gesture instead of a UI permission dialog, and (b) the plan
file path is returned in the tool result rather than being implicit.
"""

from __future__ import annotations

import re
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.telemetry.logger import current_telemetry

if TYPE_CHECKING:
    from durin.session.manager import SessionManager


_PLAN_DIR = ".durin/plans"
_ACTIVE_PLAN_PATH_KEY = "active_plan_path"

# Plan files are organized per session so that concurrent sessions don't
# collide and so the user can locate the plans for a specific chat. The
# session key (e.g. "cli:direct" or "websocket:chat42") is sanitized into
# a filesystem-safe directory name; the plan id is a timestamp with
# millisecond suffix, giving natural chronological ordering inside the
# per-session directory.
_SESSION_SLUG_RE = re.compile(r"[^\w\-]+")


def _session_slug(session_key: str | None) -> str:
    """Filesystem-safe slug for *session_key*; defaults to ``default``."""
    if not session_key:
        return "default"
    slug = _SESSION_SLUG_RE.sub("_", session_key).strip("_")
    return slug[:80] or "default"


def _resolve_plan_dir(workspace: Path | None, session_key: str | None) -> Path:
    """Return ``<workspace>/.durin/plans/<session_slug>`` for the session.

    The per-session subdirectory keeps plans cleanly grouped: ``ls`` on the
    session dir shows just that conversation's plans, and the path itself
    encodes both ``session`` and ``plan`` identifiers (see the user's
    note "usa el id de session y un id de plan"). Falls back to
    ``/tmp/durin_plans/<session_slug>`` when no workspace is set.
    """
    slug = _session_slug(session_key)
    base: Path
    if workspace is not None:
        with suppress(OSError, RuntimeError):
            base = Path(workspace).resolve() / _PLAN_DIR / slug
            return base
    return Path("/tmp/durin_plans") / slug


def _new_plan_id() -> str:
    """Generate a plan id: timestamp with ms suffix for chronological ordering."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ms = int(time.time() * 1000) % 1000
    return f"{ts}_{ms:03d}"


def _new_plan_path(plan_dir: Path) -> Path:
    """Generate a fresh plan file path inside *plan_dir*."""
    return plan_dir / f"plan_{_new_plan_id()}.md"


class _PlanModeToolBase(ContextAware):
    """Shared session resolution + telemetry helper."""

    def __init__(self, sessions: "SessionManager", workspace: Path | None = None) -> None:
        self._sessions = sessions
        self._workspace = workspace
        self._request_ctx: RequestContext | None = None

    def set_context(self, ctx: RequestContext) -> None:
        self._request_ctx = ctx

    def _session(self) -> Any | None:
        if self._request_ctx is None:
            return None
        key = self._request_ctx.session_key
        if not key:
            return None
        return self._sessions.get_or_create(key)

    @staticmethod
    def _emit(event_type: str, data: dict[str, Any]) -> None:
        logger_obj = current_telemetry()
        if logger_obj is None:
            return
        with suppress(Exception):
            logger_obj.log(event_type, data)


@tool_parameters(
    tool_parameters_schema(
        reason=StringSchema(
            "Optional short note on why you're entering plan mode "
            "(for telemetry — does not change behavior).",
            max_length=200,
            nullable=True,
        ),
    )
)
class EnterPlanModeTool(Tool, _PlanModeToolBase):
    """Switch the current session into plan mode (read-only)."""

    _scopes = {"core"}

    def __init__(self, sessions: "SessionManager", workspace: Path | None = None) -> None:
        _PlanModeToolBase.__init__(self, sessions, workspace=workspace)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sessions = getattr(ctx, "sessions", None)
        assert sessions is not None
        workspace = getattr(ctx, "workspace", None)
        ws_path = Path(workspace) if workspace else None
        return cls(sessions=sessions, workspace=ws_path)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "enter_plan_mode"

    @property
    def description(self) -> str:
        return (
            "Switch the agent into PLAN MODE — read-only. Use this when you "
            "want to investigate and plan before acting, without touching "
            "the workspace. Once in plan mode, you cannot edit files, run "
            "shell commands, or otherwise modify state until you (or the "
            "user) exits plan mode."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, reason: str | None = None, **kwargs: Any) -> str:
        from durin.agent.agent_mode import (
            PLAN_MODE,
            enter_plan_mode,
            get_active_mode_name,
        )

        session = self._session()
        if session is None or session.metadata is None:
            return (
                "Error: cannot enter plan mode — no active session metadata."
            )

        current = get_active_mode_name(session)
        if current == PLAN_MODE.name:
            return "Already in PLAN MODE."

        previous = enter_plan_mode(session)
        self._emit("agent_mode.switch", {
            "from": previous,
            "to": PLAN_MODE.name,
            "trigger": "tool",
            "reason": (reason or "")[:200] if reason else None,
        })
        return (
            f"Entered PLAN MODE (was: {previous}). You are now read-only. "
            "Investigate, then call `exit_plan_mode` with your plan when ready."
        )


@tool_parameters(
    tool_parameters_schema(
        plan=StringSchema(
            "The complete plan in Markdown. Include the goal, the steps "
            "you will take (numbered or as a checklist), any files you "
            "intend to modify, and any open questions or assumptions. "
            "The user will see this verbatim and either run `/build` to "
            "approve, or send more messages to refine the plan.",
            max_length=20_000,
        ),
        required=["plan"],
    )
)
class ExitPlanModeTool(Tool, _PlanModeToolBase):
    """Write a finished plan to disk and yield to the user for /build approval.

    File-based storage. The plan is written to
    ``<workspace>/.durin/plans/plan_<timestamp>.md`` and the path is
    returned in the tool result. The session remains in plan mode — the
    user must run ``/build`` to approve and resume execution. While in
    plan, the user can edit the file directly; the agent will read the
    edited version on resume.
    """

    _scopes = {"core"}

    def __init__(self, sessions: "SessionManager", workspace: Path | None = None) -> None:
        _PlanModeToolBase.__init__(self, sessions, workspace=workspace)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sessions = getattr(ctx, "sessions", None)
        assert sessions is not None
        workspace = getattr(ctx, "workspace", None)
        ws_path = Path(workspace) if workspace else None
        return cls(sessions=sessions, workspace=ws_path)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "exit_plan_mode"

    @property
    def description(self) -> str:
        return (
            "Write your finished plan to disk and yield to the user for "
            "approval. The plan is saved to "
            "`<workspace>/.durin/plans/plan_<timestamp>.md`; the path is "
            "returned so you can refer to it. The session REMAINS in plan "
            "mode until the user runs `/build` to approve, so the user "
            "can review and edit the plan file in any editor before "
            "approving. Do NOT use this for research tasks — only when "
            "you are ready to propose an actionable plan."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(self, plan: str | None = None, **kwargs: Any) -> str:
        from durin.agent.agent_mode import (
            PLAN_MODE,
            get_active_mode_name,
        )

        if not plan or not plan.strip():
            return (
                "Error: `plan` argument is required and must be a non-empty "
                "Markdown string describing the plan."
            )

        session = self._session()
        if session is None or session.metadata is None:
            return (
                "Error: cannot present plan — no active session metadata."
            )

        current = get_active_mode_name(session)
        if current != PLAN_MODE.name:
            return (
                f"Error: `exit_plan_mode` can only be called while in plan "
                f"mode (current mode: {current}). If your plan is already "
                "approved, continue execution directly."
            )

        # Write the plan to disk. The directory lives inside the workspace
        # so the ReadFileTool can pick it up without extra allowed-dir
        # wiring; a /tmp fallback handles workspace-less tests. The
        # per-session subdirectory keeps plans from concurrent chats
        # separated.
        session_key = self._request_ctx.session_key if self._request_ctx else None
        plan_dir = _resolve_plan_dir(self._workspace, session_key)
        try:
            plan_dir.mkdir(parents=True, exist_ok=True)
            plan_path = _new_plan_path(plan_dir)
            plan_path.write_text(plan, encoding="utf-8")
        except OSError as e:
            return (
                f"Error: could not write plan file to {plan_dir}: {e}"
            )

        # Make the path available to /build (and the next iteration) via
        # session metadata. The user can edit the file before approving;
        # /build picks up the latest content from disk.
        session.metadata[_ACTIVE_PLAN_PATH_KEY] = str(plan_path)

        # Prefer a workspace-relative path in the tool result when possible
        # so the model uses a stable handle that ReadFileTool also accepts.
        display = str(plan_path)
        if self._workspace is not None:
            with suppress(ValueError):
                display = str(plan_path.relative_to(Path(self._workspace).resolve()))

        # Expose the plan to channels: rich channels render a plan card from
        # tool_events; dumb channels get the serialized fallback at turn end
        # (durin/agent/user_payloads.py). /build clears this payload.
        from durin.agent.user_payloads import PENDING_PLAN_KEY
        session.metadata[PENDING_PLAN_KEY] = {"path": display, "plan": plan}

        self._emit("plan_mode.presented", {
            "plan_chars": len(plan),
            "from_mode": current,
            "plan_path": str(plan_path),
        })

        # Record the plan as a `type=plan` event in the session meta file —
        # memory subsystem will read this to know "what plans happened in
        # this session". Best-effort: a failure here must not break the
        # tool result.
        with suppress(Exception):
            from durin.session.session_meta import (
                append_event,
                extract_markdown_title,
                make_plan_event,
                meta_path_for,
            )

            sessions_dir = self._workspace / "sessions" if self._workspace else None
            if sessions_dir is not None and session_key:
                mp = meta_path_for(session_key, sessions_dir)
                title = extract_markdown_title(plan)
                # Use the filename stem as a stable plan_id (matches the
                # timestamp encoded in _new_plan_path).
                plan_id = plan_path.stem
                evt = make_plan_event(
                    plan_id=plan_id,
                    plan_path=str(plan_path),
                    title=title,
                )
                append_event(mp, session_key, evt)

        return (
            f"Plan saved to **{display}**.\n\n"
            "The plan has been presented to the user by the channel (plan "
            "card or message) — do NOT paste the full plan into your reply. "
            "Your next assistant message should be 1-3 lines: state that the "
            "plan is ready for review and ask the user to run `/build` to "
            "approve (in the language the user has been writing in). You may "
            f"mention they can edit `{display}` before approving.\n\n"
            "The session remains in PLAN MODE. Do not emit more tool calls — "
            "the turn is yielded after your short message."
        )
