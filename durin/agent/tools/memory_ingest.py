"""memory_ingest tool — persist external artifacts as memory sources.

Phase 1.5 of the memory subsystem. The tool only handles persistence:
file copy + meta.json placeholder. LLM-derived enrichment (summary,
entities, relations) lands later via dream (Phase 3) or a follow-up
``memory_store`` call from the agent that just read the returned
content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.ingestion import IngestError, ingest_artifact

_PARAMETERS = tool_parameters_schema(
    path=StringSchema(
        "Absolute path (or workspace-relative path) to a markdown or "
        "plain-text file the user wants the agent to remember."
    ),
    required=["path"],
    description=(
        "Persist a user-supplied document into the agent's memory store. "
        "V1: markdown and plain-text only — binaries are rejected."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryIngestTool(Tool):
    """memory_ingest tool — persist a document for later recall."""

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_ingest"

    @property
    def description(self) -> str:
        return (
            "Persist a markdown or plain-text file into the agent's memory "
            "store. The file is copied to a stable location keyed by content "
            "hash; the file's content is returned in the result so the agent "
            "can read it in the same turn. Summary/entities/relations are "
            "populated later by dream or by a follow-up memory_store call."
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        path_str = str(kwargs.get("path", "")).strip()
        if not path_str:
            return {"error": "path is required"}

        source = Path(path_str).expanduser()
        if not source.is_absolute():
            source = (self._workspace / source).resolve()

        try:
            result = ingest_artifact(self._workspace, source)
        except IngestError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"io error: {exc}"}

        emit_tool_event(
            "memory.ingest",
            {
                "entry_id": result["id"],
                "size_bytes": result["size_bytes"],
                "suffix": Path(result["source"]).suffix,
            },
        )
        return {
            "id": result["id"],
            "saved_to": result["source"],
            "meta_path": result["meta_path"],
            "size_bytes": result["size_bytes"],
            "content": result["content"],
        }
