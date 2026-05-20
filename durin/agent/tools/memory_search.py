"""memory_search tool — Phase-1 grep over memory entries and session views."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.search import search_memory

_PARAMETERS = tool_parameters_schema(
    query=StringSchema(
        "Text to search for. Case-insensitive substring match in Phase 1."
    ),
    scope=StringSchema(
        "Where to search. 'all' (default) covers both undreamed sources and "
        "dreamed memory entries.",
        enum=["all", "dreamed", "undreamed"],
    ),
    level=StringSchema(
        "How much content to return per result. 'warm' (default) returns "
        "headlines + summaries; 'cold' returns full bodies.",
        enum=["warm", "cold"],
    ),
    required=["query"],
    description=(
        "Search the agent's memory. Returns markdown URIs the agent can "
        "drill into via memory_drill."
    ),
)


@tool_parameters(_PARAMETERS)
class MemorySearchTool(Tool):
    """memory_search tool — locate memories and source turns by substring."""

    config_key = "memory"

    @property
    def read_only(self) -> bool:
        return True

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search the agent's memory. scope='dreamed' covers memory/<class>/*.md "
            "(consolidated learnings); scope='undreamed' covers sessions/<key>.md "
            "and ingested/<id>/; scope='all' is both. level='warm' returns "
            "headlines and summaries (cheap); level='cold' adds full bodies. "
            "Returns markdown URIs usable with memory_drill."
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        query = str(kwargs.get("query") or "").strip()
        scope = str(kwargs.get("scope") or "all")
        level = str(kwargs.get("level") or "warm")

        if not query:
            return {"error": "query is required"}
        if scope not in ("all", "dreamed", "undreamed"):
            return {"error": f"invalid scope {scope!r}"}
        if level not in ("warm", "cold"):
            return {"error": f"invalid level {level!r}"}

        results = search_memory(self._workspace, query, scope=scope, level=level)  # type: ignore[arg-type]
        return {
            "results": [r.to_dict() for r in results],
            "total": len(results),
        }
