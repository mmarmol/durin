"""skill_edit tool — evolve a skill in-loop with a versioned, rationale'd edit."""
from __future__ import annotations

import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the skill to edit (directory name)."),
    old=StringSchema(
        "Exact text to replace. Must be unique in the file. Use an empty "
        "string to append `new` to the end (or create the file)."
    ),
    new=StringSchema("Replacement text."),
    rationale=StringSchema(
        "Why this change improves the skill — recorded as the commit message. "
        "Prefer native durin tools over generic ones and automate repetition."
    ),
    file=StringSchema(
        "File within the skill dir to edit. Defaults to 'SKILL.md'. May target "
        "a script under scripts/."
    ),
    confirm=BooleanSchema(
        description="Required true to apply an edit to a skill in `manual` mode "
        "(after the user approves the proposed diff)."
    ),
    required=["name", "old", "new", "rationale"],
    description=(
        "Edit one of durin's own skills and version the change (reversible). "
        "Use when, mid-task, you discover a better approach than a skill "
        "describes, or a skill has a bug/pitfall worth recording. Editing a "
        "builtin forks it into the workspace first; editing a `manual` skill "
        "returns a proposed diff that needs the user's confirmation."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillEditTool(Tool, ContextAware):
    """skill_edit tool."""

    # Core-only: self-modification of skills stays a primary-agent decision.
    _scopes = {"core"}

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()
        self._session: ContextVar[str | None] = ContextVar("skill_edit_session", default=None)
        self._model: ContextVar[str | None] = ContextVar("skill_edit_model", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._session.set(ctx.session_key)
        self._model.set((ctx.metadata or {}).get("model"))

    @property
    def name(self) -> str:
        return "skill_edit"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillEditTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_store import Attribution, apply_skill_edit

        name = str(kwargs.get("name", "")).strip()
        if not name:
            return {"error": "name is required"}
        rationale = str(kwargs.get("rationale", ""))
        attribution = Attribution(actor="agent", session=self._session.get(), agent=self._model.get())
        result = apply_skill_edit(
            self._workspace,
            name,
            old=str(kwargs.get("old", "")),
            new=str(kwargs.get("new", "")),
            rationale=rationale,
            file=str(kwargs.get("file") or "SKILL.md"),
            confirm=bool(kwargs.get("confirm", False)),
            attribution=attribution,
        )
        # A direct in-loop edit of an `auto` skill is itself a structural
        # improvement signal — feed the curation queue so the daily pass can
        # validate/generalize it, without relying on the agent also calling
        # skill_observe. Manual skills only return a proposed diff (not applied)
        # and curation never reviews them, so we log only applied auto edits.
        if isinstance(result, dict) and result.get("ok") and result.get("mode") == "auto":
            self._log_edit_observation(name, rationale)
        return result

    def _log_edit_observation(self, name: str, rationale: str) -> None:
        from durin.agent.skill_observations import log_observation
        try:
            log_observation(
                self._workspace,
                skill=name,
                kind="improvement",
                issue=f"Skill '{name}' was edited in-loop during a task.",
                improvement=rationale.strip() or "(edited in-loop; no rationale given)",
                session=self._session.get(),
            )
        except Exception:  # noqa: BLE001 — observation logging must never break the edit
            logger.exception("skill_edit: failed to log improvement observation for %s", name)
