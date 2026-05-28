"""End-to-end orchestrator for the v2 search pipeline.

Per `docs/memory/03_search_pipeline.md`: take a raw query string and
optional `keywords` hint, return a list of :class:`SectionedHit` rows
ready for rendering.

Pipeline (steps numbered per doc 03 §1):

    1. Query analysis        → query_router.decide_lexical_route
    2a. Vector search        → VectorIndex.search (optional, when
                                LanceDB + provider available)
    2b. Lexical search       → lexical_search.lexical_search
    3.  Cross-source RRF     → rrf_fusion.fuse_rrf
    4.  Entity-aware rerank  → entity_ranker.rerank_by_entity (when
                                an alias index resolves entities in
                                the query)
    7.  Sectioning + cap     → sectioned_output

Each step is wrapped in try/except so a failure of one source
degrades that source to empty instead of failing the whole call —
matches the graceful-degradation contract in doc 03 §14.

Per doc 03 §9 (Phase 4) the cross-encoder rerank step is omitted
here; it's an opt-in module that wraps this pipeline's output.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from durin.memory.fts_index import FTSIndex
from durin.memory.lexical_search import lexical_search
from durin.memory.query_router import decide_lexical_route
from durin.memory.rrf_fusion import fuse_rrf
from durin.memory.sectioned_output import (
    SectionedHit,
    apply_per_source_cap,
)

__all__ = ["SearchPipelineResult", "run_search_pipeline"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchPipelineResult:
    """Pipeline output."""

    hits: list[SectionedHit]
    # Diagnostics — let callers / dashboards see what each source
    # produced before fusion. Useful for the bench harness too.
    vector_count: int
    lexical_count: int


def run_search_pipeline(
    workspace: Path,
    query: str,
    *,
    keywords: Optional[str] = None,
    vector_index: Optional[Any] = None,
    limit: int = 10,
) -> SearchPipelineResult:
    """Execute the v2 search pipeline.

    ``vector_index`` is an optional :class:`durin.memory.vector_index.VectorIndex`
    or any object exposing ``search(query, top_k) -> [{uri, type, …}]``.
    When ``None``, the pipeline skips step 2a and runs lexical-only.

    The result is already capped per source (corpus chunks) and ready
    to render via :func:`durin.memory.sectioned_output.render_sectioned`.
    """
    decision = decide_lexical_route(query, keywords=keywords)

    # Step 2a — vector retrieval (optional)
    vector_hits = _safe_vector_search(vector_index, decision.normalized_query)
    vector_uris = [h["uri"] for h in vector_hits if "uri" in h]
    vector_meta = {h["uri"]: h for h in vector_hits if "uri" in h}

    # Step 2b — lexical retrieval
    lexical_hits = _safe_lexical_search(workspace, decision)
    lexical_uris = [h.uri for h in lexical_hits]
    lexical_meta = {h.uri: h for h in lexical_hits}

    # Step 3 — cross-source RRF.
    fused = fuse_rrf(
        vector=vector_uris,
        lexical=lexical_uris,
        grep=[],  # grep fallback (doc 03 §6) not wired here yet
        keywords_provided=bool(keywords),
    )

    # Build SectionedHit rows from the fused results, looking up
    # metadata from whichever source surfaced the uri.
    section_hits = []
    for f in fused[:limit * 4]:  # carry extra so the cap step can drop
        meta = _resolve_meta(f.uri, vector_meta, lexical_meta)
        section_hits.append(SectionedHit(
            uri=f.uri,
            type=meta.get("type", "episodic"),
            path=meta.get("path", ""),
            score=f.score,
            ts=meta.get("ts", "") or meta.get("valid_from", ""),
            snippet=meta.get("snippet", "") or meta.get("headline", ""),
            ingest_id=meta.get("ingest_id"),
        ))
    capped = apply_per_source_cap(section_hits)
    return SearchPipelineResult(
        hits=capped[:limit],
        vector_count=len(vector_uris),
        lexical_count=len(lexical_uris),
    )


# ---------------------------------------------------------------------------
# Per-step wrappers — never raise
# ---------------------------------------------------------------------------


def _safe_vector_search(
    vector_index: Optional[Any], query: str,
) -> list[dict]:
    if vector_index is None or not query:
        return []
    try:
        # We accept either a real `VectorIndex.search` (returns a list
        # of dicts) or any duck-typed object with the same shape.
        return list(vector_index.search(query, top_k=50))
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_pipeline: vector failed: %s", exc)
        return []


def _safe_lexical_search(workspace: Path, decision) -> list:
    try:
        with FTSIndex.open(workspace) as idx:
            return lexical_search(idx, decision, limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_pipeline: lexical failed: %s", exc)
        return []


def _resolve_meta(
    uri: str,
    vector_meta: dict[str, dict],
    lexical_meta: dict,
) -> dict:
    """Pick the metadata fields needed by SectionedHit from whichever
    source carried the uri. Vector wins on type because it can
    distinguish entity vs episodic vs corpus from the indexed schema."""
    meta: dict = {}
    if uri in lexical_meta:
        lh = lexical_meta[uri]
        meta["type"] = lh.type
        meta["path"] = lh.path
    if uri in vector_meta:
        vh = vector_meta[uri]
        # Vector index uses `type` field too — entity_page rows come
        # back as type="entity_page" historically; normalise to "entity".
        vtype = vh.get("type", meta.get("type", "episodic"))
        if vtype == "entity_page":
            vtype = "entity"
        meta["type"] = vtype
        meta["path"] = vh.get("path", meta.get("path", ""))
        if vh.get("valid_from"):
            meta["valid_from"] = vh["valid_from"]
        if vh.get("headline"):
            meta["headline"] = vh["headline"]
    return meta
