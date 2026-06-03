"""skills_list tool — list skills with their security verdict + quarantine.

Auto-discovered into the agent's ``core`` toolset (like ``skill_audit`` /
``skill_edit``). Returns the Skills-Surface read model so the agent can list
active skills (with the §8.C verdict/findings) plus the import quarantine from
any chat, without re-running a scan by hand. Read-only.

Optional ``status`` filter (``active`` | ``quarantined``) narrows the result to
one bucket; omitted returns both.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_DESCRIPTION = "List skills with their security verdict and quarantine status."

_PARAMETERS = tool_parameters_schema(
    status=StringSchema(
        "Optional filter: 'active' (installed skills) or 'quarantined' "
        "(awaiting an import decision). Omit to return both.",
        enum=["active", "quarantined"],
    ),
    description=_DESCRIPTION,
)


@tool_parameters(_PARAMETERS)
class SkillsListTool(Tool):
    """skills_list tool — Skills-Surface inventory + quarantine."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "skills_list"

    @property
    def description(self) -> str:
        return _DESCRIPTION

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> "SkillsListTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_surface import quarantined_skills, skills_inventory

        status = str(kwargs.get("status", "")).strip()
        out: dict[str, Any] = {}
        if status != "quarantined":
            out["active"] = skills_inventory(self._workspace)
        if status != "active":
            out["quarantined"] = quarantined_skills(self._workspace)
        return out
