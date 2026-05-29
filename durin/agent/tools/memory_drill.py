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
    description=(
        # Canonical text per `docs/memory/06_prompts_and_instructions.md` §3.4.
        "Read the full content of a memory item by URI.\n\n"
        "Use this ONLY when the corresponding memory_search result block "
        "is marked `preview N/M` in its section header — N chars were "
        "shown, M chars exist — i.e. more body is available beyond what "
        "you already have. Drill in that case to fetch the rest.\n\n"
        "Do NOT drill when the block is marked `complete`: the search "
        "already showed you the entire body and drill will return the "
        "same text, wasting tokens and an LLM round-trip. Blocks without "
        "an explicit completeness qualifier (rare; legacy / lexical-only "
        "hits) are best-guess — drill only if the visible content seems "
        "truncated.\n\n"
        "This tool is read-only. For related context about an entity "
        "(recent observations, sessions mentioning it), use memory_search "
        "with the entity's name or URI as the query instead — drill on "
        "a single URI never expands beyond that URI."
    ),
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
        # Canonical text per `docs/memory/06_prompts_and_instructions.md` §3.4.
        # Reads via `Tool.to_schema()` → `function.description` in the
        # OpenAI spec — what the LLM sees. Audit B1 (2026-05-28) caught
        # the prior short text drifted from the canonical doc.
        return _PARAMETERS["description"]

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
