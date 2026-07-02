"""skill_write tool — the sanctioned skill-authoring tool.

Auto-discovered into the main agent's ``core`` toolset (like E1's
``skill_edit``), so it is available in-loop as well as to the 2h dream. It
routes through E1's service layer (:func:`dream_create_skill`) instead of a raw
WriteFileTool, so every authored skill is a first-class citizen: provenance
source='dream', mode='auto', and committed to the skills subtree.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the new skill (directory name)."),
    content=StringSchema("Full SKILL.md body to author for the new skill."),
    rationale=StringSchema(
        "Why this skill is worth creating — recorded as the commit message. "
        "Use for a recurring pattern that no existing skill covers."
    ),
    required=["name", "content", "rationale"],
    description=(
        "Create a new skill (a step-by-step procedure to follow later). Writes "
        "skills/<name>/SKILL.md through the sanctioned store: versioned, committed, and "
        "attributed to whoever calls it (you in-session, or the dream). Include YAML "
        "frontmatter with `name` and `description` — the description is the only text "
        "used to decide when the skill surfaces, so state its trigger conditions. Use "
        "for a recurring pattern no existing skill covers; search first."
    ),
)


@tool_parameters(_PARAMETERS)
class SkillWriteTool(Tool, ContextAware):
    """skill_write tool — the sanctioned skill-authoring tool.

    Used by the dream and available in-loop; routes through skills_store.
    """

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()
        self._session: ContextVar[str | None] = ContextVar("skill_write_session", default=None)
        self._model: ContextVar[str | None] = ContextVar("skill_write_model", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._session.set(ctx.session_key)
        self._model.set((ctx.metadata or {}).get("model"))

    @property
    def name(self) -> str:
        return "skill_write"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillWriteTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> str:
        from durin.agent.skills_store import Attribution, dream_create_skill

        attribution = Attribution(actor="agent", session=self._session.get(), agent=self._model.get())
        result = dream_create_skill(
            self._workspace,
            str(kwargs.get("name", "")),
            str(kwargs.get("content", "")),
            str(kwargs.get("rationale", "")),
            attribution=attribution,
        )
        return json.dumps(result, ensure_ascii=False)
