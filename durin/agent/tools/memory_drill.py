"""memory_drill tool — resolve a markdown URI to its addressed section."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.drill import DrillError, drill

_PARAMETERS = tool_parameters_schema(
    uri=StringSchema(
        "Markdown URI such as 'sessions/<key>.md#turn-42', "
        "'ingested/<id>/source.md#section-3', or 'memory/<class>/<id>'."
    ),
    required=["uri"],
    description="Read the section addressed by a markdown anchor URI.",
)


@tool_parameters(_PARAMETERS)
class MemoryDrillTool(Tool):
    """memory_drill tool — return a specific section of a memory source."""

    config_key = "memory"

    @property
    def read_only(self) -> bool:
        return True

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_drill"

    @property
    def description(self) -> str:
        return (
            "Resolve a markdown URI (path#anchor) to the addressed section. "
            "Use after memory_search returns a URI to fetch just that turn or "
            "section instead of the whole file. Returns the section text from "
            "the matching `## <anchor>` header up to the next same-or-higher "
            "level header. No anchor → whole file content."
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        uri = str(kwargs.get("uri") or "").strip()
        if not uri:
            return {"error": "uri is required"}

        try:
            text = drill(self._workspace, uri)
        except DrillError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"io error: {exc}"}

        return {"uri": uri, "content": text}
