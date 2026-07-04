"""convert_to_markdown tool — read a local document as clean markdown.

Converts a document (pdf, docx, pptx, xlsx, …) to markdown via the shared
``durin.memory.doc_convert`` helper and returns the full text into the current
turn. LLMs reason far better over clean markdown than over raw PDF/HTML bytes,
so this is the "just let me read it" primitive.

Transient by design: the tool touches no memory and writes nothing durable.
Oversized results are handled by the runner's standard truncate-and-spill
path, not here. To persist a document, use ``memory_ingest`` instead.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.doc_convert import (
    SUPPORTED_SUFFIXES,
    ConvertedDoc,
    DocConvertError,
    convert_file_to_markdown,
)

_HEADING_RE = re.compile(r"^#{1,6} .+$", re.MULTILINE)
_MAX_OUTLINE_ENTRIES = 100

_PARAMETERS = tool_parameters_schema(
    path=StringSchema(
        "Absolute path (or workspace-relative path) to the document to convert."
    ),
    required=["path"],
)


@tool_parameters(_PARAMETERS)
class ConvertToMarkdownTool(Tool):
    """convert_to_markdown tool — one-shot document → markdown conversion."""

    _scopes = {"core"}

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    @property
    def name(self) -> str:
        return "convert_to_markdown"

    @property
    def description(self) -> str:
        return (
            "Convert a local document to clean markdown and return the full "
            "text in this turn, so you can read and reason over it. Supported "
            "formats: " + ", ".join(s.lstrip(".") for s in SUPPORTED_SUFFIXES)
            + ". Use this when the user points at a document they want read, "
            "summarized, or discussed NOW. It does NOT save anything to "
            "memory — for that, use `memory_ingest`."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> Any:
        path_str = str(kwargs.get("path", "")).strip()
        if not path_str:
            return {"error": "path is required"}

        source = Path(path_str).expanduser()
        if not source.is_absolute():
            source = (self._workspace / source).resolve()
        if not source.is_file():
            return {"error": f"file not found: {source}"}

        try:
            converted: ConvertedDoc = await asyncio.to_thread(
                convert_file_to_markdown, source
            )
        except DocConvertError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"io error: {exc}"}

        markdown = converted.markdown
        outline = _HEADING_RE.findall(markdown)[:_MAX_OUTLINE_ENTRIES]

        emit_tool_event(
            "tool.convert_to_markdown",
            {
                "format": converted.suffix,
                "size_chars": len(markdown),
                "outline_entries": len(outline),
            },
        )

        # Metadata first, `markdown` last: the runner head-truncates oversized
        # results, so the summary fields must survive the head while the
        # (spilled) full text absorbs the cut.
        return {
            "path": str(source),
            "format": converted.suffix,
            "size_chars": len(markdown),
            "outline": outline,
            "markdown": markdown,
        }
