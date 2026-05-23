"""memory_store tool — write a memory entry under memory/<class>/<id>.md."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import logging
from typing import Optional

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema
from durin.memory.paths import MEMORY_CLASSES
from durin.memory.provenance import author_scope
from durin.memory.store import StoreError, store_memory
from durin.memory.storage import load_entry
from durin.memory.vector_index import VectorIndex, vector_index_available

logger = logging.getLogger(__name__)

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
        StringSchema("entity reference in '<type>:<value>' form"),
        description=(
            "Optional list of typed entity references this memory mentions. "
            "Each item MUST follow the form '<type>:<value>' where type is "
            "lowercase [a-z][a-z0-9_]* and value is non-empty. Suggested "
            "types (open vocabulary — new types welcome when content "
            "demands): person, place, project, topic, event, artifact, "
            "stance, practice. Examples: 'person:marcelo', "
            "'project:durin', 'topic:embeddings', 'artifact:settings.py'."
        ),
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

    def __init__(
        self,
        workspace: str | Path,
        embedding_model: str | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        # Lazily constructed once on first use; None means "disabled".
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False

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
        # Vector retrieval is opt-in (memory.enabled). When off, pass no
        # embedding model so `_get_vector_index` stays None and the tool
        # degrades to markdown-only memory.
        model = None
        try:
            if ctx.config.memory.enabled:
                model = ctx.config.memory.embedding.model
        except (AttributeError, TypeError):
            model = None
        return cls(workspace=ctx.workspace, embedding_model=model)

    def _get_vector_index(self) -> Optional[VectorIndex]:
        """Lazy construct the VectorIndex once; returns None if disabled."""
        if self._vector_index_attempted:
            return self._vector_index
        self._vector_index_attempted = True
        if not self._embedding_model or not vector_index_available():
            return None
        try:
            from durin.memory.embedding import FastembedProvider

            provider = FastembedProvider(model=self._embedding_model)
            self._vector_index = VectorIndex(self._workspace, provider)
        except Exception as exc:
            logger.warning("vector index init failed: %s", exc)
            self._vector_index = None
        return self._vector_index

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

        emit_tool_event(
            "memory.store",
            {
                "entry_id": result["id"],
                "class_name": result["class"],
                "author": result["author"],
                "headline": result["headline"],
            },
        )

        # Best-effort vector upsert. A failure here must not break the
        # write path — the markdown file is the source of truth and the
        # index can always be rebuilt from it.
        vi = self._get_vector_index()
        if vi is not None:
            try:
                entry_path = Path(result["path"])
                entry = load_entry(entry_path)
                vi.upsert(entry, result["class"], entry_path)
            except Exception as exc:
                logger.warning("vector upsert failed for %s: %s", result["id"], exc)

        return result
