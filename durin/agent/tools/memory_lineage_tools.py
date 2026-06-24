from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.entity_page import EntityPage

_READ_PARAMS = tool_parameters_schema(
    ref=StringSchema("Entity ref '<type>:<slug>' (e.g. 'place:torrent')."),
    required=["ref"],
    description="Read a memory entity's FULL page (frontmatter + attributes + "
                "relations + provenance + body). Use to inspect an entity in detail.",
)


def _page_path(workspace: Path, ref: str) -> Path:
    type_, _, slug = ref.partition(":")
    return Path(workspace) / "memory" / "entities" / type_ / f"{slug}.md"


@tool_parameters(_READ_PARAMS)
class MemoryReadEntityTool(Tool):
    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_read_entity"

    @property
    def description(self) -> str:
        return _READ_PARAMS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        ref = (kwargs.get("ref") or "").strip()
        if ":" not in ref:
            return {"error": "ref must be '<type>:<slug>'"}
        path = _page_path(self._workspace, ref)
        if not path.exists():
            return {"error": f"no entity {ref}"}
        page = EntityPage.from_file(path)
        if page is None:
            return {"error": f"unreadable {ref}"}
        return {"ref": ref, "markdown": page.to_markdown()}
