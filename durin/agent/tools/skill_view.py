"""skill_view tool — load a skill as a managed capability (not a raw file read).

Auto-discovered into the agent's ``core`` toolset (like ``skills_list`` /
``skill_audit``). Returns a skill's instructions (frontmatter stripped) plus a
map of its bundled files and any missing setup, instead of the model reading the
``SKILL.md`` by path. Loading a skill this way is also the clean usage signal
that feeds the hot-tier working set — ``extract_skill_calls`` records a
``skill_view`` as ``op="view"`` (durin/agent/skill_usage.py).

With ``file_path`` set, returns one bundled sub-file (references/scripts/
templates/assets) for progressive disclosure of multi-file skills. Read-only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_DESCRIPTION = (
    "Load a skill by name to use it: returns its instructions, a map of its "
    "bundled files (references/scripts/templates/assets) with a resolved "
    "directory, and any missing setup. Prefer this over reading the SKILL.md "
    "file directly. Pass file_path to read one bundled file instead of the "
    "main instructions."
)

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("The skill's name, as shown in the skills catalog."),
    file_path=StringSchema(
        "Optional: a bundled file to read instead of the main instructions, "
        "relative to the skill directory (e.g. 'references/api.md', "
        "'scripts/run.py').",
        nullable=True,
    ),
    required=["name"],
    description=_DESCRIPTION,
)


@tool_parameters(_PARAMETERS)
class SkillViewTool(Tool):
    """skill_view tool — load a skill's instructions, bundle map, and readiness."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "skill_view"

    @property
    def description(self) -> str:
        return _DESCRIPTION

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> "SkillViewTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills import SkillsLoader

        name = str(kwargs.get("name", "")).strip()
        raw_file = kwargs.get("file_path")
        file_path = str(raw_file).strip() if raw_file else None
        if not name:
            return {"error": "skill_view requires a 'name'."}
        loader = SkillsLoader(workspace=self._workspace)
        payload = loader.view_skill(name, file_path=file_path)
        if payload is None:
            available = sorted(
                e["name"] for e in loader.list_skills(filter_unavailable=False)
            )
            return {"error": f"No skill named '{name}'.", "available": available}
        return payload
