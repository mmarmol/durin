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

    # Step 6 (doc 03) — grep fallback over raw sessions + ingested
    # artifacts. These aren't in LanceDB/FTS5 by design; the only
    # way to surface them is a direct file scan. Best-effort: a
    # failure here logs and degrades that source to empty.
    grep_hits = _safe_grep_fallback(workspace, decision.normalized_query)
    grep_uris = [h["uri"] for h in grep_hits if "uri" in h]
    grep_meta = {h["uri"]: h for h in grep_hits if "uri" in h}

    # Step 3 — cross-source RRF.
    fused = fuse_rrf(
        vector=vector_uris,
        lexical=lexical_uris,
        grep=grep_uris,
        keywords_provided=bool(keywords),
    )

    # Step 4 — entity-aware rerank (doc 03 §8). When the query
    # mentions a known alias, hits whose entities (or ref) include
    # that alias receive an extra RRF contribution. Reuses the
    # existing `entity_ranker.rank_with_entities` API via a thin
    # adapter that maps :class:`FusedHit` to the dict shape it
    # expects.
    fused = _entity_aware_rerank(
        workspace, decision.normalized_query, fused,
        vector_meta=vector_meta, lexical_meta=lexical_meta,
        grep_meta=grep_meta,
    )

    # Build SectionedHit rows from the fused results, looking up
    # metadata from whichever source surfaced the uri.
    section_hits = []
    for f in fused[:limit * 4]:  # carry extra so the cap step can drop
        meta = _resolve_meta(
            f.uri, vector_meta, lexical_meta, grep_meta=grep_meta,
        )
        section_hits.append(SectionedHit(
            uri=f.uri,
            type=meta.get("type", "episodic"),
            path=meta.get("path", ""),
            score=f.score,
            ts=meta.get("ts", "") or meta.get("valid_from", ""),
            snippet=meta.get("snippet", "") or meta.get("headline", ""),
            ingest_id=meta.get("ingest_id"),
            body=meta.get("body", ""),
        ))
    capped = apply_per_source_cap(section_hits)
    return SearchPipelineResult(
        hits=capped[:limit],
        vector_count=len(vector_uris),
        lexical_count=len(lexical_uris),
    )


def _entity_aware_rerank(
    workspace: Path,
    query: str,
    fused: list,
    *,
    vector_meta: dict[str, dict],
    lexical_meta: dict,
    grep_meta: dict[str, dict] | None = None,
) -> list:
    """Apply entity-aware rerank (doc 03 §8) over the RRF-fused list.

    Resolves query → entity URIs via the shared alias index. When the
    set is empty (query mentions no known entity), this is a no-op
    and the input order is preserved.

    Otherwise we delegate to ``entity_ranker.rank_with_entities``
    which produces a second RRF over an entity-match list. The
    function operates on dicts (legacy v1 interface); we adapt by
    building one dict per FusedHit + re-attaching the score after.
    """
    if not fused:
        return fused

    try:
        from durin.memory.aliases_cache import get_shared_alias_index
        alias_index = get_shared_alias_index(workspace / "memory")
    except Exception as exc:  # noqa: BLE001
        logger.warning("entity rerank: alias index unavailable: %s", exc)
        return fused
    if alias_index is None or alias_index.size() == 0:
        return fused

    from durin.memory.entity_ranker import (
        extract_query_entities,
        rank_with_entities,
    )

    query_entities = extract_query_entities(query, alias_index)
    if not query_entities:
        return fused

    # Adapt FusedHit → dict shape rank_with_entities expects.
    candidates: list[dict] = []
    for h in fused:
        meta = _resolve_meta(
            h.uri, vector_meta, lexical_meta, grep_meta=grep_meta,
        )
        class_name = (
            "entity_page" if meta.get("type") == "entity" else
            meta.get("type", "episodic")
        )
        candidates.append({
            "id": h.uri,
            "class_name": class_name,
            "entities": [h.uri] if class_name == "entity_page" else (
                meta.get("entities") or []
            ),
            "valid_from": meta.get("valid_from", ""),
            # Use the RRF score as the base; higher = better, so the
            # ranker must flip its sort.
            "_score": h.score,
            "_fused": h,
        })

    ranked = rank_with_entities(
        candidates,
        query_entities=query_entities,
        score_field="_score",
        higher_is_better=True,
    )
    # Rebuild the FusedHit list in the new order. Adjusted score from
    # the ranker is the entity-aware combined score.
    out: list = []
    from durin.memory.rrf_fusion import FusedHit
    for r in ranked:
        original = r.record.get("_fused")
        if isinstance(original, FusedHit):
            out.append(FusedHit(
                uri=original.uri,
                score=r.adjusted_score,
                sources=original.sources,
                ranks=original.ranks,
            ))
    return out


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


def _safe_grep_fallback(workspace: Path, query: str) -> list[dict]:
    """Run the v1 grep fallback over memory/, sessions/, ingested/.

    Covers two complementary cases:
    - Sessions and ingested artifacts not indexed by LanceDB/FTS5
      (per `01_data_and_entities.md` §3.1-§3.2) — only reachable
      via grep.
    - Memory entries written by callers that bypass the tool layer
      (tests, scripts) and therefore have no FTS row yet — grep
      over `memory/` recovers them so the search doesn't return
      empty just because the indexer never ran.
    """
    if not query:
        return []
    try:
        from durin.memory.search import search_memory
        # `search_memory(scope='all', level='warm')` walks both
        # dreamed (memory/<class>/*) and undreamed (sessions, ingested).
        results = search_memory(
            workspace, query, scope="all", level="warm",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_pipeline: grep fallback failed: %s", exc)
        return []
    out: list[dict] = []
    for r in results:
        out.append({
            "uri": r.uri,
            "path": getattr(r, "uri", ""),
            "type": getattr(r, "class_name", "") or "episodic",
            "snippet": getattr(r, "snippet", "")
                or getattr(r, "headline", ""),
        })
    return out


def _resolve_meta(
    uri: str,
    vector_meta: dict[str, dict],
    lexical_meta: dict,
    *,
    grep_meta: dict[str, dict] | None = None,
) -> dict:
    """Pick the metadata fields needed by SectionedHit from whichever
    source carried the uri. Vector wins on type because it can
    distinguish entity vs episodic vs corpus from the indexed schema."""
    meta: dict = {}
    if grep_meta and uri in grep_meta:
        gh = grep_meta[uri]
        meta["type"] = gh.get("type", "session_summary")
        meta["path"] = gh.get("path", "")
        meta["snippet"] = gh.get("snippet", "")
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
        # P2.5: vector index now persists `body`. Pass it through so
        # cold-tier callers don't need a disk read.
        if vh.get("body"):
            meta["body"] = vh["body"]
    return meta
