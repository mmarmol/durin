"""memory_store tool — write a memory entry under memory/<class>/<id>.md."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema
from durin.memory.paths import MEMORY_CLASSES
from durin.memory.provenance import author_scope
from durin.memory.store import StoreError, store_memory

_PARAMETERS = tool_parameters_schema(
    content=StringSchema(
        "Markdown body of the memory entry — the full text to remember."
    ),
    class_name=StringSchema(
        "Memory class. Default: episodic. "
        "stable=identity/corrections, episodic=working/recent, "
        "corpus=queryable archive, pending=prospective.",
        enum=list(MEMORY_CLASSES),
    ),
    headline=StringSchema(
        "Optional ~10-word headline. Auto-generated from content if omitted."
    ),
    summary=StringSchema(
        "Optional ~50-word summary returned by memory_search(level='warm')."
    ),
    source_refs=ArraySchema(
        StringSchema("markdown link"),
        description=(
            "Optional markdown links pointing back to the originating turn(s) or "
            "ingested doc section(s), e.g. "
            "[turn 42](../sessions/abc.md#turn-42)."
        ),
    ),
    entities=ArraySchema(
        StringSchema("entity name"),
        description="Optional list of named entities this memory references.",
    ),
    required=["content"],
    description=(
        "Persist a memory entry under memory/<class>/<id>.md. Author is "
        "stamped automatically from the agent's current write-origin "
        "(agent_created when called by the agent, user_authored otherwise)."
    ),
)


@tool_parameters(_PARAMETERS)
class MemoryStoreTool(Tool):
    """memory_store tool — persist distilled learnings as memory entries."""

    config_key = "memory"

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "memory_store"

    @property
    def description(self) -> str:
        return (
            "Persist a memory entry under memory/<class>/<id>.md with full "
            "frontmatter (headline + summary + body + source_refs + entities + "
            "author + valid_from). Idempotent: same (class, content) writes "
            "the same id. Author defaults to agent_created when invoked by "
            "the agent."
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        content = str(kwargs.get("content", "")).strip()
        if not content:
            return {"error": "content is required"}

        class_name = str(kwargs.get("class_name") or "episodic")
        headline = kwargs.get("headline") or None
        summary = str(kwargs.get("summary") or "")
        source_refs = kwargs.get("source_refs") or []
        entities = kwargs.get("entities") or []

        # The agent invoking this tool is the author — mark it explicitly so
        # the curator and dream can later distinguish agent-created entries
        # from user-edited ones.
        try:
            with author_scope("agent_created"):
                result = store_memory(
                    self._workspace,
                    content=content,
                    class_name=class_name,
                    headline=headline,
                    summary=summary,
                    source_refs=list(source_refs),
                    entities=list(entities),
                )
        except StoreError as exc:
            return {"error": str(exc)}
        except OSError as exc:
            return {"error": f"io error: {exc}"}

        return result
