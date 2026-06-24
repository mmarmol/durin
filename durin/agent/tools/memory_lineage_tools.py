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


from datetime import datetime, timezone as _tz  # noqa: E402

_LINEAGE_PARAMS = tool_parameters_schema(
    ref=StringSchema("Entity ref '<type>:<slug>'."),
    required=["ref"],
    description="Git history of a memory entity: who changed it, when, and the "
                "commit reason (incl. absorb/merge commits). Use to judge lineage "
                "— is this an established entity or a fresh one; was it merged before.",
)


@tool_parameters(_LINEAGE_PARAMS)
class MemoryEntityLineageTool(Tool):
    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_entity_lineage"

    @property
    def description(self) -> str:
        return _LINEAGE_PARAMS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        ref = (kwargs.get("ref") or "").strip()
        type_, _, slug = ref.partition(":")
        rel = f"entities/{type_}/{slug}.md".encode()
        root = self._workspace / "memory"
        try:
            from dulwich.repo import Repo
            repo = Repo(str(root))
            out = []
            for entry in repo.get_walker(paths=[rel], max_entries=20):
                c = entry.commit
                out.append({
                    "sha": c.id.decode()[:10],
                    "when": datetime.fromtimestamp(c.author_time, _tz.utc).isoformat(),
                    "author": c.author.decode("utf-8", "replace"),
                    "message": c.message.decode("utf-8", "replace").strip(),
                })
            return {"ref": ref, "commits": out}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"lineage unavailable: {exc}", "commits": []}
