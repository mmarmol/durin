"""memory_search tool — Phase-1 grep over memory entries and session views."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import logging
import time
from typing import Optional

from durin.agent.tools._telemetry import emit_tool_event
from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema
from durin.memory.search import Result, search_memory
from durin.memory.vector_index import VectorIndex, vector_index_available

logger = logging.getLogger(__name__)

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

    def __init__(
        self,
        workspace: str | Path,
        embedding_model: str | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser()
        self._embedding_model = embedding_model
        self._vector_index: Optional[VectorIndex] = None
        self._vector_index_attempted = False

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
        # Vector retrieval is opt-in (memory.enabled); see memory_store.
        model = None
        try:
            if ctx.config.memory.enabled:
                model = ctx.config.memory.embedding.model
        except (AttributeError, TypeError):
            model = None
        return cls(workspace=ctx.workspace, embedding_model=model)

    def _get_vector_index(self) -> Optional[VectorIndex]:
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
        query = str(kwargs.get("query") or "").strip()
        scope = str(kwargs.get("scope") or "all")
        level = str(kwargs.get("level") or "warm")

        if not query:
            return {"error": "query is required"}
        if scope not in ("all", "dreamed", "undreamed"):
            return {"error": f"invalid scope {scope!r}"}
        if level not in ("warm", "cold"):
            return {"error": f"invalid level {level!r}"}

        # Vector path: only for warm-tier searches that include the
        # dreamed scope (the index only holds memory entries, not raw
        # sessions or ingested artifacts). Falls back to grep on any
        # failure so the tool never returns nothing because the index
        # was broken.
        results: list[Result] = []
        strategy = "grep"
        vi = self._get_vector_index()
        if level == "warm" and scope in ("dreamed", "all") and vi is not None:
            try:
                t0 = time.monotonic()
                vector_rows = vi.search(query, top_k=10)
                duration_ms = (time.monotonic() - t0) * 1000.0
                vector_results = [_vector_row_to_result(row) for row in vector_rows]
                emit_tool_event(
                    "memory.recall.vector",
                    {
                        "query": query,
                        "scope": scope,
                        "embedding_model": self._embedding_model or "",
                        "hit_count": len(vector_results),
                        "duration_ms": duration_ms,
                    },
                )
                if scope == "dreamed":
                    results = vector_results
                    strategy = "vector"
                else:
                    # scope=all: vector covers memory entries; grep adds
                    # sessions + ingested.
                    undreamed = search_memory(
                        self._workspace, query, scope="undreamed", level=level
                    )
                    results = vector_results + undreamed
                    strategy = "hybrid"
            except Exception as exc:
                logger.warning("vector search failed, falling back to grep: %s", exc)
                results = []

        if not results and strategy == "grep":
            results = search_memory(self._workspace, query, scope=scope, level=level)  # type: ignore[arg-type]

        emit_tool_event(
            "memory.recall",
            {
                "query": query,
                "scope": scope,
                "level": level,
                "result_count": len(results),
            },
        )
        return {
            "results": [r.to_dict() for r in results],
            "total": len(results),
            "strategy": strategy,
        }


def _vector_row_to_result(row: dict) -> Result:
    """Shape a LanceDB row to match the grep Result schema."""
    class_name = row.get("class_name", "")
    entry_id = row.get("id", "")
    summary = row.get("summary", "") or ""
    headline = row.get("headline", "") or ""
    return Result(
        source="memory",
        uri=f"memory/{class_name}/{entry_id}",
        headline=headline,
        snippet=(summary[:160] if summary else headline)[:160],
        summary=summary,
        body="",
    )
