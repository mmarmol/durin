"""skill_discard tool — delete a draft skill."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the draft skill under skill-drafts/ to delete."),
    required=["name"],
    description="Discard a draft skill (removes skill-drafts/<name>/). Does not touch active skills.",
)


@tool_parameters(_PARAMETERS)
class SkillDiscardTool(Tool):
    """skill_discard tool."""

    _scopes = {"core"}

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "skill_discard"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "SkillDiscardTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> str:
        import asyncio

        from durin.agent.skills_store import discard_draft_skill

        result = await asyncio.to_thread(discard_draft_skill, self._workspace, str(kwargs.get("name", "")))
        return json.dumps(result, ensure_ascii=False)
