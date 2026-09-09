"""Lexical retrieval — execute the query against the right FTS5 table.

Take a
:class:`durin.memory.query_router.RoutingDecision` and run it against
the corresponding FTS5 path, returning a ranked list of URIs that
the RRF fusion step consumes.

The three execution paths:

  - ``UNICODE61``      → ``SELECT … FROM memory_fts WHERE text MATCH ?``
  - ``TRIGRAM``        → ``SELECT … FROM memory_fts_trigram WHERE text MATCH ?``
  - ``LIKE_SUBSTRING`` → ``SELECT … FROM memory_fts WHERE text LIKE %?%``
    (no scoring — returned in insertion / mtime order)

Every query token is double-quoted before FTS5 so special characters
(``%``, ``*``, ``:``) and — critically — the FTS5 boolean keywords
(``AND``/``OR``/``NOT``/``NEAR``) are treated as literal content, not
operators. The recall tool contract is natural language plus balanced
double-quoted phrases (see ``memory_search``); it never exposes boolean
syntax. Passing those keywords through as operators would hijack the
commonest English function words — a query like "not sure what to do"
starts with a bare ``NOT``, which is a leading binary operator with no
left operand and raises ``fts5: syntax error near "NOT"``.

Emits ``memory.recall.lexical`` per call with route + counts + duration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from durin.memory.fts_index import FTSHit, FTSIndex
from durin.memory.query_router import LexicalRoute, RoutingDecision

__all__ = ["FtsExpression", "build_fts_expression", "lexical_search"]

logger = logging.getLogger(__name__)


def lexical_search(
    index: FTSIndex,
    decision: RoutingDecision,
    *,
    limit: int = 50,
    emit: bool = True,
    type_: Optional[str] = None,
    include_types: Optional[Sequence[str]] = None,
    exclude_types: Optional[Sequence[str]] = None,
) -> list[FTSHit]:
    """Execute the lexical part of the search pipeline.

    Returns up to ``limit`` :class:`FTSHit` rows in best-first order
    (BM25 score for FTS paths; insertion order for the LIKE fallback).

    Emits ``memory.recall.lexical`` after the run. Pass ``emit=False`` from a
    caller that is not a memory search: the event's row count is read as the
    number of searches, and its duration series is the search latency, so a
    lookup that is a side effect of some other tool would dilute both.

    ``type_`` is the one-type shorthand used by ``artifact_recall`` — when
    given, it restricts every route to rows of that stored ``type``, so a
    caller that wants only one row shape (e.g. entity pages) gets ``limit``
    spent on that shape, not truncated by unrelated rows that also match the
    query text. ``include_types``/``exclude_types`` are the set forms used
    by the search pipeline's scope; see ``FTSIndex._type_clause`` for
    precedence.
    """
    t0 = time.perf_counter()
    hits: list[FTSHit] = []
    query = decision.normalized_query
    if not query:
        if emit:
            _emit_lexical(decision=decision, hit_count=0,
                          duration_ms=(time.perf_counter() - t0) * 1000.0)
        return hits

    if decision.route is LexicalRoute.UNICODE61:
        hits = index.search(
            _quote_for_fts(query), limit=limit, type_=type_,
            include_types=include_types, exclude_types=exclude_types,
        )
    elif decision.route is LexicalRoute.TRIGRAM:
        hits = index.search_trigram(
            _quote_for_fts(query), limit=limit, type_=type_,
            include_types=include_types, exclude_types=exclude_types,
        )
    elif decision.route is LexicalRoute.LIKE_SUBSTRING:
        hits = _like_substring_scan(
            index, query, limit=limit, type_=type_,
            include_types=include_types, exclude_types=exclude_types,
        )

    if emit:
        _emit_lexical(
            decision=decision, hit_count=len(hits),
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )
    return hits


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FtsExpression:
    text: str
    required: int
    optional: int


def _fts_term(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def build_fts_expression(query: str, keywords: str | None = None) -> FtsExpression:
    """The one FTS5 expression behind every lexical search.

    Loose tokens of ``query`` are OR-joined: bm25 ranks partial matches, so
    a sentence finds the note that shares its rare words. Balanced quoted
    phrases in ``query`` and every token of ``keywords`` are required
    (AND) — that is where exactness lives. Operators and punctuation are
    quoted so they stay literal. An unbalanced quote degrades to tokens.
    """
    phrases, loose, balanced = _extract_phrases(query or "")
    if not balanced:
        loose = [tok.replace('"', "") for tok in loose]
    required: list[str] = [_fts_term(p) for p in phrases if p.strip()]
    if keywords and keywords.strip():
        kw_phrases, kw_loose, kw_balanced = _extract_phrases(keywords)
        if not kw_balanced:
            kw_loose = [tok.replace('"', "") for tok in kw_loose]
        required += [_fts_term(t) for t in kw_loose if t]
        required += [_fts_term(p) for p in kw_phrases if p.strip()]
    optional = [_fts_term(t) for t in loose if t]
    parts: list[str] = list(required)
    if optional:
        parts.append("(" + " OR ".join(optional) + ")")
    return FtsExpression(" AND ".join(parts), len(required), len(optional))


def _quote_for_fts(query: str) -> str:
    """Quote every token so special chars (``%``, ``*``, ``:``) and the
    FTS5 boolean keywords (``AND``/``OR``/``NOT``/``NEAR``) are treated
    as literal content — the recall query is natural language, not a
    boolean expression, so a bare leading ``NOT``/``AND`` must not reach
    the parser as a dangling operator.

    Respects agent-supplied double-quoted phrases. A balanced
    ``"like this"`` substring in the query is
    preserved as a single FTS5 phrase token (words must appear
    adjacent and in order); the remaining tokens are quoted
    individually as before. An unbalanced quote falls back to
    token-only parsing — the lone quote is stripped and the rest of
    the query is treated as tokens, so a malformed query degrades
    rather than crashes.
    """
    phrases, loose, balanced = _extract_phrases(query)
    if not balanced:
        # Unbalanced — strip stray quotes from the loose tokens and
        # fall through to the per-token path with no phrases.
        loose = [tok.replace('"', '') for tok in loose]
    parts: list[str] = []
    for phrase in phrases:
        if not phrase.strip():
            continue
        safe_phrase = phrase.replace('"', '""')
        parts.append(f'"{safe_phrase}"')
    for token in loose:
        if not token:
            continue
        safe = token.replace('"', '""')
        parts.append(f'"{safe}"')
    return " ".join(parts)


def _extract_phrases(query: str) -> tuple[list[str], list[str], bool]:
    """Split ``query`` into ``(phrases, loose_tokens, balanced)``.

    A double-quoted substring becomes one entry in ``phrases``; the
    remainder is whitespace-split into ``loose_tokens``. ``balanced``
    is False when the query contains an odd number of unescaped
    double quotes; callers degrade to token-only parsing in that
    case.

    Examples
    --------
    >>> _extract_phrases('"Marcelo Marmol" lives in Spain')
    (['Marcelo Marmol'], ['lives', 'in', 'Spain'], True)
    >>> _extract_phrases('hello world')
    ([], ['hello', 'world'], True)
    >>> _extract_phrases('Marcelo "incomplete')
    ([], ['Marcelo', '"incomplete'], False)
    """
    phrases: list[str] = []
    loose: list[str] = []
    chunks: list[str] = []  # text between quoted segments
    cursor = 0
    open_idx: Optional[int] = None
    for i, ch in enumerate(query):
        if ch != '"':
            continue
        if open_idx is None:
            chunks.append(query[cursor:i])
            open_idx = i
        else:
            phrases.append(query[open_idx + 1:i])
            cursor = i + 1
            open_idx = None
    if open_idx is not None:
        # Unbalanced — drop everything from the dangling quote onward
        # so the tokenless tail can't bias the AND-join. Tokens before
        # the lone quote stay; the rest is discarded.
        loose_tokens = query[:open_idx].split()
        return [], loose_tokens, False
    chunks.append(query[cursor:])
    for chunk in chunks:
        loose.extend(chunk.split())
    return phrases, loose, True


def _like_substring_scan(
    index: FTSIndex,
    query: str,
    *,
    limit: int,
    type_: Optional[str] = None,
    include_types: Optional[Sequence[str]] = None,
    exclude_types: Optional[Sequence[str]] = None,
) -> list[FTSHit]:
    """Direct LIKE scan on the unicode61 table for short CJK queries.

    Reaches into the underlying connection because the trigram table
    can't tokenise tokens shorter than 3 chars (a single CJK char
    typically). LIKE is O(N) but the workspace size is small enough
    that this is fine as a fallback.

    ``type_``/``include_types``/``exclude_types`` build the same type
    clause as the FTS routes — see ``FTSIndex._type_clause`` and
    ``lexical_search``.
    """
    conn = index._conn  # noqa: SLF001 — intentional friend access
    like = f"%{query}%"
    clause, params = index._type_clause(type_, include_types, exclude_types)  # noqa: SLF001
    cur = conn.execute(
        f"SELECT uri, path, type, entity_type FROM memory_fts "
        f"WHERE text LIKE ?{clause} LIMIT ?",
        (like, *params, limit),
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
