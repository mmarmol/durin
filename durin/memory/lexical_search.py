"""Lexical retrieval — execute the query against the right FTS5 table.

Per `docs/memory/03_search_pipeline.md` §5: take a
:class:`durin.memory.query_router.RoutingDecision` and run it against
the corresponding FTS5 path, returning a ranked list of URIs that
the RRF fusion step consumes.

The three execution paths:

  - ``UNICODE61``      → ``SELECT … FROM memory_fts WHERE text MATCH ?``
  - ``TRIGRAM``        → ``SELECT … FROM memory_fts_trigram WHERE text MATCH ?``
  - ``LIKE_SUBSTRING`` → ``SELECT … FROM memory_fts WHERE text LIKE %?%``
    (no scoring — returned in insertion / mtime order)

FTS5 special characters in the query are escaped per Hermes-agent's
pattern (`hermes_state.py:2207-2213`): each non-operator token is
double-quoted; operators (``AND``/``OR``/``NOT``) pass through.

Emits ``memory.recall.lexical`` per call with route + counts + duration.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

from durin.memory.fts_index import FTSHit, FTSIndex
from durin.memory.query_router import LexicalRoute, RoutingDecision

__all__ = ["lexical_search"]

logger = logging.getLogger(__name__)


_OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})


def lexical_search(
    index: FTSIndex,
    decision: RoutingDecision,
    *,
    limit: int = 50,
) -> list[FTSHit]:
    """Execute the lexical part of the search pipeline.

    Returns up to ``limit`` :class:`FTSHit` rows in best-first order
    (BM25 score for FTS paths; insertion order for the LIKE fallback).

    Emits ``memory.recall.lexical`` after the run.
    """
    t0 = time.perf_counter()
    hits: list[FTSHit] = []
    query = decision.normalized_query
    if not query:
        _emit_lexical(decision=decision, hit_count=0,
                      duration_ms=(time.perf_counter() - t0) * 1000.0)
        return hits

    if decision.route is LexicalRoute.UNICODE61:
        hits = index.search(_quote_for_fts(query), limit=limit)
    elif decision.route is LexicalRoute.TRIGRAM:
        hits = index.search_trigram(_quote_for_fts(query), limit=limit)
    elif decision.route is LexicalRoute.LIKE_SUBSTRING:
        hits = _like_substring_scan(index, query, limit=limit)

    _emit_lexical(
        decision=decision, hit_count=len(hits),
        duration_ms=(time.perf_counter() - t0) * 1000.0,
    )
    return hits


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _quote_for_fts(query: str) -> str:
    """Per `03_search_pipeline.md` §5.2: quote non-operator tokens so
    special chars (``%``, ``*``, ``:``) don't confuse the parser.
    """
    parts: list[str] = []
    for token in query.split():
        if token.upper() in _OPERATORS:
            parts.append(token.upper())
            continue
        # Escape embedded double-quotes (FTS5 doubles them inside
        # phrase-quoted tokens).
        safe = token.replace('"', '""')
        parts.append(f'"{safe}"')
    return " ".join(parts)


def _like_substring_scan(
    index: FTSIndex, query: str, *, limit: int,
) -> list[FTSHit]:
    """Direct LIKE scan on the unicode61 table for short CJK queries.

    Reaches into the underlying connection because the trigram table
    can't tokenise tokens shorter than 3 chars (a single CJK char
    typically). LIKE is O(N) but the workspace size is small enough
    that this is fine as a fallback.
    """
    conn = index._conn  # noqa: SLF001 — intentional friend access
    like = f"%{query}%"
    cur = conn.execute(
        "SELECT uri, path, type, entity_type FROM memory_fts "
        "WHERE text LIKE ? LIMIT ?",
        (like, limit),
    )
    return [
        FTSHit(uri=u, path=p, type=t, entity_type=et)
        for (u, p, t, et) in cur.fetchall()
    ]


def _emit_lexical(
    *, decision: RoutingDecision, hit_count: int, duration_ms: float,
) -> None:
    """Best-effort telemetry — never raises."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(
            "memory.recall.lexical",
            {
                "route": decision.route.value,
                "query_chars": len(decision.normalized_query),
                "cjk_chars": decision.cjk_chars,
                "hit_count": hit_count,
                "duration_ms": duration_ms,
            },
        )
    except Exception:  # pragma: no cover
        pass
