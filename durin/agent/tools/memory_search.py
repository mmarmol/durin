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
from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.memory.entity_ranker import (
    extract_query_entities,
    rank_with_entities,
)
from durin.memory.search import Result, search_memory
from durin.memory.vector_index import VectorIndex, vector_index_available

logger = logging.getLogger(__name__)


def _load_cursors_from_entities_dir(
    memory_root: Path,
    entity_refs: list[str],
) -> dict[str, Any]:
    """Read ``dream_processed_through`` from each entity's page (S3, doc 24).

    Returns ``{entity_ref: cursor_value}`` for refs whose page exists and
    has a cursor field. Used by entity_ranker to apply the pre/post-cursor
    boost/demote. Best-effort — missing or unparseable pages skip silently.
    """
    cursors: dict[str, Any] = {}
    for ref in entity_refs:
        if ":" not in ref:
            continue
        type_, slug = ref.split(":", 1)
        page_path = memory_root / "entities" / type_ / f"{slug}.md"
        if not page_path.exists():
            continue
        try:
            page = EntityPage.from_file(page_path)
        except Exception:  # noqa: BLE001
            continue
        if page is not None and page.dream_processed_through is not None:
            cursors[ref] = page.dream_processed_through
    return cursors

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
        # Per doc 25 §2.C: alias index is shared process-wide via
        # durin.memory.aliases_cache, so DreamConsolidator and
        # EntityAbsorption see updates as soon as we (or they) call
        # refresh_for / remove on it. No per-instance state needed.

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

    def _get_alias_index(self) -> Optional[AliasIndex]:
        """Resolve the workspace-shared AliasIndex (doc 25 §2.C).

        Built lazily on first call across the whole process via
        :func:`durin.memory.aliases_cache.get_shared_alias_index`;
        ``DreamConsolidator`` and ``EntityAbsorption`` consult the same
        instance so a single workspace builds the index once even when
        multiple consumers hit it in the same ``durin agent`` run.

        Returns ``None`` when the index is empty — entity-aware
        reranking is a no-op against an empty alias map, so surfacing
        ``None`` lets the upstream code skip the rerank step entirely.
        """
        try:
            from durin.memory.aliases_cache import get_shared_alias_index

            idx = get_shared_alias_index(self._workspace / "memory")
        except Exception as exc:  # noqa: BLE001
            logger.warning("alias_index resolve failed: %s", exc)
            return None
        return idx if idx.size() > 0 else None

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
        ranking = "default"
        # S2 (doc 24): metrics for telemetry to inform future tuning.
        top_1_before: str = ""
        top_1_after: str = ""
        query_entities: list[str] = []

        vi = self._get_vector_index()
        if level == "warm" and scope in ("dreamed", "all") and vi is not None:
            try:
                t0 = time.monotonic()
                vector_rows = vi.search(query, top_k=10)
                duration_ms = (time.monotonic() - t0) * 1000.0

                # W1 (doc 24): entity-aware reranking via RRF when alias
                # index has data + query mentions a known entity. Operates
                # on raw LanceDB rows BEFORE Result conversion so the
                # ranker has access to entities + valid_from + _distance.
                top_1_before = vector_rows[0]["id"] if vector_rows else ""
                ai = self._get_alias_index()
                if vector_rows and ai is not None:
                    query_entities = extract_query_entities(query, ai)
                    if query_entities:
                        cursors = _load_cursors_from_entities_dir(
                            self._workspace / "memory", query_entities,
                        )
                        ranked = rank_with_entities(
                            vector_rows,
                            query_entities=query_entities,
                            cursors=cursors,
                            score_field="_distance",
                            higher_is_better=False,
                        )
                        vector_rows = [rc.record for rc in ranked]
                        ranking = "entity_aware"
                top_1_after = vector_rows[0]["id"] if vector_rows else ""

                vector_results = [_vector_row_to_result(row) for row in vector_rows]
                emit_tool_event(
                    "memory.recall.vector",
                    {
                        "query": query,
                        "scope": scope,
                        "embedding_model": self._embedding_model or "",
                        "hit_count": len(vector_results),
                        "duration_ms": duration_ms,
                        # S2 (doc 24): entity-aware ranking telemetry
                        # piggy-backs on the existing vector event instead
                        # of duplicating into memory.recall.entity_aware.
                        "ranking": ranking,
                        "query_entities_count": len(query_entities),
                        "reordered": top_1_before != top_1_after,
                        "top_1_id_before": top_1_before,
                        "top_1_id_after": top_1_after,
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
        # §2.H: rendered block carries explicit `=== CANONICAL/FRAGMENT
        # ===` markers so the LLM can distinguish the main answer from
        # recent post-cursor context at parse time. Same convention as
        # the compaction `=== ARCHIVED SUMMARY ===` block. Raw fields
        # remain in to_dict() for callers that prefer structured access.
        return {
            "results": [
                {**r.to_dict(), "rendered": r.render_block()}
                for r in results
            ],
            "total": len(results),
            "strategy": strategy,
            # S1 (doc 24): separate `ranking` field from `strategy` so
            # downstream callers that pattern-match strategy don't break
            # when entity-aware ranking is applied.
            "ranking": ranking,
        }


def _vector_row_to_result(row: dict) -> Result:
    """Shape a LanceDB row to match the grep Result schema.

    Per doc 25 §2.H: preserve ``class_name`` / ``valid_from`` /
    ``entities`` so the LLM-facing :meth:`Result.to_dict` and
    :meth:`Result.render_block` can mark the row as canonical vs
    fragment. Earlier versions dropped these fields, breaking the
    contract that doc 18 §6 ("LLM reconcilia con timestamps y
    contexto") implied.
    """
    class_name = row.get("class_name", "")
    entry_id = row.get("id", "")
    summary = row.get("summary", "") or ""
    headline = row.get("headline", "") or ""
    valid_from = row.get("valid_from", "") or ""
    raw_entities = row.get("entities") or []
    entities = tuple(str(e) for e in raw_entities)
    return Result(
        source="memory",
        uri=f"memory/{class_name}/{entry_id}",
        headline=headline,
        snippet=(summary[:160] if summary else headline)[:160],
        summary=summary,
        body="",
        class_name=class_name,
        valid_from=valid_from,
        entities=entities,
    )
