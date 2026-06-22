"""End-to-end orchestrator for the v2 search pipeline.

Per `docs/internals/memory/03_search_pipeline.md`: take a raw query string and
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
from durin.memory.query_router import LexicalRoute, decide_lexical_route
from durin.memory.rrf_fusion import (
    DEFAULT_K,
    DEFAULT_W_LEXICAL,
    FusedHit,
    apply_type_priors,
    fuse_rrf,
)
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
    # P5.2: degraded-run surface. When a safe wrapper caught an
    # exception, the source name lands in `recovered_from` and the
    # total wall-clock spent recovering accumulates in
    # `recovery_duration_ms`. Empty / 0 on clean runs.
    recovered_from: tuple[str, ...] = ()
    recovery_duration_ms: float = 0.0


def run_search_pipeline(
    workspace: Path,
    query: str,
    *,
    keywords: Optional[str] = None,
    vector_index: Optional[Any] = None,
    limit: int = 10,
    cross_encoder: Optional[Any] = None,
    cross_encoder_top_n: int = 10,
    max_per_source: int | None = None,
) -> SearchPipelineResult:
    """Execute the v2 search pipeline.

    ``vector_index`` is an optional :class:`durin.memory.vector_index.VectorIndex`
    or any object exposing ``search(query, top_k) -> [{uri, type, …}]``.
    When ``None``, the pipeline skips step 2a and runs lexical-only.

    The result is already capped per source (corpus chunks) and ready
    to render via :func:`durin.memory.sectioned_output.render_sectioned`.
    """
    decision = decide_lexical_route(query, keywords=keywords)
    # P5.2: shared accumulator passed to safe wrappers so any
    # failure surfaces in the result.
    recovery: dict = {"sources": set(), "ms": 0.0}

    # Step 2a — vector retrieval (optional)
    vector_hits = _safe_vector_search(
        vector_index, decision.normalized_query, recovery=recovery,
    )
    vector_uris = [h["uri"] for h in vector_hits if "uri" in h]
    vector_meta = {h["uri"]: h for h in vector_hits if "uri" in h}

    # Step 2b — lexical retrieval
    lexical_hits = _safe_lexical_search(
        workspace, decision, recovery=recovery,
    )
    lexical_uris = [h.uri for h in lexical_hits]
    lexical_meta = {h.uri: h for h in lexical_hits}

    # Step 6 (doc 03) — grep fallback over memory/, raw sessions and
    # ingested artifacts. Sessions are FTS-indexed too (v6); grep
    # remains their literal-scan source and the only path for raw
    # ingested artifacts and not-yet-indexed files. Best-effort: a
    # failure here logs and degrades that source to empty.
    grep_hits = _safe_grep_fallback(
        workspace, decision.normalized_query, recovery=recovery,
    )
    grep_uris = [h["uri"] for h in grep_hits if "uri" in h]
    grep_meta = {h["uri"]: h for h in grep_hits if "uri" in h}

    # Step 3 — cross-source RRF.
    fused = fuse_rrf(
        vector=vector_uris,
        lexical=lexical_uris,
        grep=grep_uris,
        # P3.3: auto-detected identifier (email/URL/UUID/path) gets
        # the same lexical boost as an explicit `keywords` param.
        keywords_provided=bool(keywords or decision.auto_keywords),
    )

    # Step 3a' — grep-verify boost. A vector-sourced hit the lexical
    # top-50 missed, whose indexed text literally matches the query,
    # gains the lexical-grade contribution the cutoff dropped. Runs
    # before the type prior: it adds evidence; the prior then weighs
    # the total.
    fused = _grep_verify_boost(workspace, decision, fused,
                               keywords=keywords)

    # Step 3b — type prior. Raw session turns (FTS-indexed sessions)
    # carry a soft demotion so the distilled entry/entity leads at
    # comparable evidence; a clearly better session match still wins.
    # Before the entity rerank: an alias match is extra evidence and
    # may legitimately re-elevate a session hit.
    fused = apply_type_priors(
        fused,
        types={
            f.uri: _resolve_meta(
                f.uri, vector_meta, lexical_meta, grep_meta=grep_meta,
            ).get("type", "")
            for f in fused
        },
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

    # Step 5 — cross-encoder rerank (doc 03 §9). Opt-in. When a
    # reranker instance is supplied, take the top 50 hits, build
    # (query, doc_text) pairs, score them, and drop everything
    # ranked below `cross_encoder_top_n`. Graceful degradation: a
    # reranker failure returns the original RRF order.
    if cross_encoder is not None and fused:
        fused = _cross_encoder_rerank(
            cross_encoder, decision.normalized_query, fused,
            vector_meta=vector_meta, lexical_meta=lexical_meta,
            grep_meta=grep_meta,
            top_n=cross_encoder_top_n,
        )

    # Temporal decay removed (2026-05-30): search is faithful
    # retrieval; the LLM does temporal reasoning with `valid_from`
    # already present on every hit. Pre-judging recency without the
    # question's context perjudicated factual atemporal queries (the
    # LoCoMo conv-5-q20 chicken-vs-sushi case) and gave no win we
    # couldn't get from the LLM reading dates itself.

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
            # H4 (audit 2026-05-29): propagate the index's materialised
            # summary (authoritative or body-prefix fallback) so the
            # warm-tier renderer never falls back to a 60-char headline.
            summary=meta.get("summary", ""),
            # H5 (audit 2026-05-29): propagate the source body length
            # so the renderer can emit the completeness qualifier
            # (``complete`` vs ``preview N/M``) in the marker line.
            body_length=int(meta.get("body_length", 0) or 0),
        ))
    # G1 (audit fourth pass, 2026-05-28): honour the configured cap
    # when supplied; fall back to `DEFAULT_MAX_PER_SOURCE` otherwise.
    if max_per_source is None:
        capped = apply_per_source_cap(section_hits)
    else:
        capped = apply_per_source_cap(
            section_hits, max_per_source=max_per_source,
        )
    result = SearchPipelineResult(
        hits=capped[:limit],
        vector_count=len(vector_uris),
        lexical_count=len(lexical_uris),
        recovered_from=tuple(sorted(recovery["sources"])),
        recovery_duration_ms=recovery["ms"],
    )
    # Audit B9 (2026-05-28) — emit `memory.search.failure` when at
    # least one safe wrapper caught an exception. The pipeline always
    # recovers (the surviving sources cover the loss most of the
    # time); the event lets dashboards see degradation rate per
    # component. Wrapped in try/except: telemetry never breaks the
    # search result.
    if recovery["sources"]:
        try:
            _emit_search_failure(
                affected=recovery["sources"],
                duration_ms=recovery["ms"],
                vector_count=len(vector_uris),
                lexical_count=len(lexical_uris),
                hit_count=len(capped),
            )
        except Exception:  # pragma: no cover
            pass
    return result


def _emit_search_failure(
    *,
    affected: set[str],
    duration_ms: float,
    vector_count: int,
    lexical_count: int,
    hit_count: int,
) -> None:
    """Emit ``memory.search.failure`` with derived degradation info.

    ``degraded_to`` is derived from the per-source counts AFTER
    recovery so the dashboard sees what the pipeline actually
    surfaced — not just what failed.
    """
    from durin.agent.tools._telemetry import emit_tool_event

    # Derive degraded_to from which sources still produced hits.
    surviving_with_vector = vector_count > 0 and "vector" not in affected
    surviving_with_lexical = lexical_count > 0 and "lexical" not in affected
    if hit_count == 0:
        degraded_to = "none"
    elif surviving_with_vector and surviving_with_lexical:
        # Both primary sources alive — must be grep that failed.
        degraded_to = "full"
    elif surviving_with_vector:
        degraded_to = "vector_only"
    elif surviving_with_lexical:
        degraded_to = "lexical_only"
    else:
        # Only hits left came from grep (or a survivor with 0 count).
        degraded_to = "grep_only"

    emit_tool_event(
        "memory.search.failure",
        {
            "component": ",".join(sorted(affected)),
            "recovery_attempted": True,
            "recovery_succeeded": hit_count > 0,
            "recovery_duration_ms": duration_ms,
            "degraded_to": degraded_to,
        },
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


# Weight of the (normalised) cross-encoder score in the final blend
# (doc 03 §9.2). The CE is fused with the RRF score, not allowed to
# replace the order outright. Calibrated 2026-06-11 by an offline
# recall@10 sweep over the LoCoMo run: α=0.4 / z-score maximised gold
# recall@10 (75.0%) over both α=0 (RRF-only, 69.7%) and α=1 (full CE
# replace). 0 ≤ α ≤ 1;
# α=0 disables the CE contribution, α=1 reverts to a pure CE reorder.
DEFAULT_BLEND_ALPHA = 0.4


def _zscore(values: list[float]) -> list[float]:
    """Standardise to mean 0 / sd 1. Degenerate input (≤1 value or
    zero variance) maps to all-zeros so the blend leans on the other
    signal instead of dividing by ~0."""
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    sd = var ** 0.5
    if sd < 1e-9:
        return [0.0] * n
    return [(v - mean) / sd for v in values]


def _rerank_doc_text(meta: dict, uri: str) -> str:
    """Enriched (query, doc) text for the cross-encoder.

    CE-demotion forensics (2026-06-11): scoring the 160-char snippet
    alone made the reranker blind to the date (fatal for temporal
    queries) and to the entry's own summary, so it demoted gold the
    RRF stage had ranked correctly. The pipeline already carries
    headline / valid_from / summary in `meta` (no disk read), so the
    CE now scores ``<headline>. <date>. <summary|snippet>`` — the
    single change that moved gold recall@10 from 53.9% (snippet) to
    73.7% (enriched) in the offline sweep.
    """
    parts = [
        meta.get("headline") or "",
        str(meta.get("valid_from") or ""),
        meta.get("summary") or meta.get("snippet") or "",
    ]
    text = ". ".join(p for p in parts if p)
    return text or uri


def _cross_encoder_rerank(
    reranker: Any,
    query: str,
    fused: list,
    *,
    vector_meta: dict[str, dict],
    lexical_meta: dict,
    grep_meta: dict[str, dict] | None,
    top_n: int,
) -> list:
    """Blend a cross-encoder score into the fused order (doc 03 §9).

    Two design choices, both set by the 2026-06-11 offline sweep:

    1. **Enriched input** — the CE scores ``<headline>. <date>.
       <summary>`` (see :func:`_rerank_doc_text`), not the bare
       snippet, so it stops demoting gold over a missing date/summary.
    2. **Blend, not replace** — the final order is
       ``α·zscore(ce) + (1-α)·zscore(rrf)`` over the top-50 candidates.
       A hit the RRF stage scored high keeps that standing; the CE
       nudges, it does not veto (which is what wrecked temporal
       queries when it replaced the order outright).

    Graceful degradation: a CE that fails to load / score (``score``
    returns ``None``) leaves the RRF order untouched.

    ``top_n`` is retained for signature compatibility; the blend
    reorders all top-50 candidates and the downstream sectioning +
    per-source cap + ``limit`` do the final trim.
    """
    import time as _time

    candidates = fused[:50]  # cap input to 50 per doc 03 §9.3
    if not candidates:
        return fused
    docs = [
        _rerank_doc_text(
            _resolve_meta(h.uri, vector_meta, lexical_meta, grep_meta=grep_meta),
            h.uri,
        )
        for h in candidates
    ]

    t0 = _time.perf_counter()
    ce_scores = reranker.score(query, docs)
    duration_ms = (_time.perf_counter() - t0) * 1000.0

    blended = ce_scores is not None and len(ce_scores) == len(candidates)
    if blended:
        rrf_norm = _zscore([float(h.score) for h in candidates])
        ce_norm = _zscore([float(s) for s in ce_scores])
        alpha = DEFAULT_BLEND_ALPHA
        scored = [
            (h, alpha * ce_norm[i] + (1 - alpha) * rrf_norm[i])
            for i, h in enumerate(candidates)
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        reordered = [h for h, _ in scored]
    else:
        # CE unavailable: keep the RRF order verbatim.
        reordered = list(candidates)

    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(
            "memory.recall.rerank",
            {
                "input_count": len(candidates),
                "output_count": len(reordered),
                "duration_ms": duration_ms,
                "blend_alpha": DEFAULT_BLEND_ALPHA if blended else 0.0,
                "fallback": not blended,
            },
        )
    except Exception:  # pragma: no cover
        pass

    # Reordered top-50 + the tail beyond 50 (so the per-source cap
    # still has material if the result set was larger than 50).
    return reordered + fused[50:]


def _safe_vector_search(
    vector_index: Optional[Any], query: str,
    *,
    recovery: dict,
) -> list[dict]:
    if vector_index is None or not query:
        return []
    import time as _time
    t0 = _time.perf_counter()
    try:
        # We accept either a real `VectorIndex.search` (returns a list
        # of dicts) or any duck-typed object with the same shape.
        rows = list(vector_index.search(query, top_k=50))
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_pipeline: vector failed: %s", exc)
        recovery["sources"].add("vector")
        recovery["ms"] += (_time.perf_counter() - t0) * 1000.0
        return []
    # Audit H1 (2026-05-29): the production ``VectorIndex.search()``
    # returns rows keyed on ``id`` / ``class_name`` / ``path`` — but
    # the rest of this pipeline (RRF fusion, _resolve_meta, etc.)
    # keys off ``uri`` / ``type``. Pre-H1 ``vector_uris`` was always
    # empty (the comprehension at the call site filtered every row
    # via ``if "uri" in h``), making the entire warm-tier vector
    # path silently inert. The Phase 3 orchestrator test
    # (``test_fake_vector_index_integrated``) passed because the
    # fixture emits ``uri`` directly; production never matched that
    # shape. This boundary normaliser fixes it.
    #
    # Audit H28 (2026-05-30): H1 set ``uri = id`` (bare filename
    # stem) but the FTS indexer (``indexer._payload_for``) writes
    # entries as ``memory/<class>/<id>`` — so RRF was fusing
    # ``9b6f1c81724a`` (vector) and ``memory/episodic/9b6f1c81724a``
    # (FTS) as DIFFERENT URIs. The same entry surfaced twice in the
    # ranked list with split scores; neither contribution alone
    # passed the threshold to top-K, so well-matched entries
    # disappeared from fused even when both subsystems found them
    # (verified: conv-7-q113 Deborah/Karlie case). Fix: vector
    # normaliser builds the SAME ``memory/<class>/<id>`` URI shape
    # as FTS. Entity-page rows (``class_name == "entity_page"``)
    # keep the entity-ref URI (``person:deborah``, etc.) since
    # FTS upserts those with that shape too (see
    # ``indexer._payload_for``).
    normalized: list[dict] = []
    for r in rows:
        if "uri" not in r:
            raw_id = r.get("id")
            if not raw_id:
                continue
            class_name = r.get("class_name", "")
            if class_name == "entity_page":
                # Entity-page rows: id is already the entity_ref
                # (e.g. "person:deborah") per upsert_entity_page.
                uri = raw_id
            elif class_name == "skill":
                # B1 (skills, 2026-06-03): the FTS indexer writes skills
                # under the bare `skill/<slug>` uri (via
                # `_payload_for_skill`), NOT `skills/<slug>/SKILL.md`. To
                # let RRF fuse the vector + FTS hits for the SAME skill,
                # the vector normaliser must use that bare `skill/<slug>`
                # id (== `raw_id`) as the FUSION uri — the H28 principle
                # "build the SAME uri shape as FTS". The drillable
                # `skills/<slug>/SKILL.md` display path rides along in the
                # row's `path` field and is resolved at the result layer
                # (memory_search._sectioned_to_result via
                # `_skill_uri_to_path`).
                uri = raw_id
            elif class_name:
                # Episodic / stable / corpus / session_summary etc.
                # Match the FTS convention: memory/<class>/<id>.
                uri = f"memory/{class_name}/{raw_id}"
            else:
                # No class — fall back to bare id (defensive; should
                # never happen in production).
                uri = raw_id
            r = {**r, "uri": uri}
        if "type" not in r and "class_name" in r:
            r = {**r, "type": r["class_name"]}
        normalized.append(r)
    return normalized


def _safe_lexical_search(
    workspace: Path, decision, *, recovery: dict,
) -> list:
    import time as _time
    t0 = _time.perf_counter()
    try:
        with FTSIndex.open(workspace) as idx:
            return lexical_search(idx, decision, limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_pipeline: lexical failed: %s", exc)
        recovery["sources"].add("lexical")
        recovery["ms"] += (_time.perf_counter() - t0) * 1000.0
        return []


def _safe_grep_fallback(
    workspace: Path, query: str, *, recovery: dict,
) -> list[dict]:
    """Run the v1 grep fallback over memory/, sessions/, ingested/.

    Covers two complementary cases:
    - Raw ingested artifacts, which are not indexed by LanceDB/FTS5
      (per `01_data_and_entities.md` §3.2) — only reachable via
      grep. (Sessions ARE FTS-indexed since schema v6; grep stays
      their literal-substring source and the recovery path when the
      index hasn't caught up.)
    - Memory entries written by callers that bypass the tool layer
      (tests, scripts) and therefore have no FTS row yet — grep
      over `memory/` recovers them so the search doesn't return
      empty just because the indexer never ran.
    """
    if not query:
        return []
    import time as _time
    t0 = _time.perf_counter()
    try:
        from durin.memory.search import search_memory
        # `search_memory(scope='all', level='warm')` walks both
        # dreamed (memory/<class>/*) and undreamed (sessions, ingested).
        results = search_memory(
            workspace, query, scope="all", level="warm",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_pipeline: grep fallback failed: %s", exc)
        recovery["sources"].add("grep")
        recovery["ms"] += (_time.perf_counter() - t0) * 1000.0
        return []
    out: list[dict] = []
    for r in results:
        if getattr(r, "source", "") == "sessions":
            # Raw session turns: type "session" — the type prior and
            # the section renderer key off it (the class_name fallback
            # used to mislabel these "episodic").
            hit_type = "session"
        else:
            hit_type = getattr(r, "class_name", "") or "episodic"
        out.append({
            "uri": r.uri,
            "path": getattr(r, "uri", ""),
            "type": hit_type,
            "snippet": getattr(r, "snippet", "")
                or getattr(r, "headline", ""),
        })
    return out


def _grep_verify_boost(
    workspace: Path,
    decision,
    fused: list,
    *,
    keywords: Optional[str] = None,
) -> list:
    """Literal re-verification of vector-sourced hits (doc 03 §7.4).

    RRF only credits lexical evidence inside the lexical top-50. A doc
    vector ranks high that literally contains the query terms — but
    sat just past that cutoff — got no lexical contribution, so a
    semantically-near distractor could outrank a literally-confirmed
    hit. For each fused hit with a vector source and no lexical
    source, run a per-uri FTS MATCH (same tokenizers as the lexical
    tier — language-neutral, route-aware, no disk reads). Confirmed
    hits gain ``W_LEXICAL / (k + rank_in_vector)``: literal presence
    is the lexical evidence the cutoff dropped, credited at the rank
    vector vouched for. ``keywords``, when supplied, is the literal
    string verified (the agent's explicit literal intent).

    Best-effort: any failure returns the input unchanged.
    """
    candidates = [
        h for h in fused
        if "vector" in h.sources and "lexical" not in h.sources
        and h.ranks.get("vector")
    ]
    target = (keywords or "").strip() or decision.normalized_query
    if not candidates or not target:
        return fused
    import time as _time
    t0 = _time.perf_counter()
    verified: set[str] = set()
    try:
        with FTSIndex.open(workspace) as idx:
            for h in candidates:
                if _literal_match_for_uri(idx, decision.route, target, h.uri):
                    verified.add(h.uri)
    except Exception as exc:  # noqa: BLE001
        logger.warning("search_pipeline: grep-verify failed: %s", exc)
        return fused
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event("memory.recall.grep_verify", {
            "candidates": len(candidates),
            "verified": len(verified),
            "duration_ms": (_time.perf_counter() - t0) * 1000.0,
        })
    except Exception:  # pragma: no cover — telemetry never breaks search
        pass
    if not verified:
        return fused
    out = [
        FusedHit(
            uri=h.uri,
            score=h.score + DEFAULT_W_LEXICAL / (
                DEFAULT_K + h.ranks["vector"]),
            sources=h.sources,
            ranks=h.ranks,
        ) if h.uri in verified else h
        for h in fused
    ]
    out.sort(key=lambda h: h.score, reverse=True)
    return out


def _literal_match_for_uri(
    idx: FTSIndex, route: LexicalRoute, target: str, uri: str,
) -> bool:
    """Does this uri's indexed text literally match ``target``?

    Follows the query's lexical route so CJK verifies through the
    trigram table and short-CJK through LIKE — the same multilingual
    contract as the lexical tier itself.
    """
    from durin.memory.lexical_search import _quote_for_fts

    conn = idx._conn  # noqa: SLF001 — same friend access as LIKE scan
    if route is LexicalRoute.LIKE_SUBSTRING:
        cur = conn.execute(
            "SELECT 1 FROM memory_fts WHERE uri = ? AND text LIKE ? "
            "LIMIT 1",
            (uri, f"%{target}%"),
        )
        return cur.fetchone() is not None
    table = (
        "memory_fts_trigram" if route is LexicalRoute.TRIGRAM
        else "memory_fts"
    )
    cur = conn.execute(
        f"SELECT 1 FROM {table} WHERE {table} MATCH ? AND uri = ? "
        f"LIMIT 1",
        (_quote_for_fts(target), uri),
    )
    return cur.fetchone() is not None


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
        # E11 (2026-05-28): propagate `entities` from the vector row
        # so the entity-aware reranker can find the tag overlap. Pre-
        # E11 this field never reached `rank_with_entities`, which
        # meant every memory entry had `entities=[]` and no entry was
        # ever boosted into the entity-match list — only the canonical
        # page got the boost. Compounded with the missing cursor
        # wiring, this hid the regression: with no entries in the
        # entity-match list at all, there was no observable pre/post
        # difference to detect.
        if vh.get("entities"):
            meta["entities"] = vh["entities"]
        # H4 (audit 2026-05-29): the vector row carries summary —
        # authoritative when Dream / memory_store set it, otherwise
        # the body-prefix fallback materialised at upsert time. The
        # renderer keys off this for the warm-tier triage block.
        if vh.get("summary"):
            meta["summary"] = vh["summary"]
        # H5 (audit 2026-05-29): propagate the source body length so
        # the renderer can compute the per-hit completeness qualifier.
        # ``body_length=0`` (the default) marks "unknown" — the
        # renderer omits the signal entirely for backward compat.
        if vh.get("body_length") is not None:
            meta["body_length"] = int(vh["body_length"] or 0)
        # NOTE: A4 reverted P2.5 — body is no longer stored in
        # LanceDB. `meta["body"]` stays unset; the cold-tier caller
        # (memory_search._enrich_body) reads it from disk.
    return meta


