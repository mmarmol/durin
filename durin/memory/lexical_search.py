"""Lexical retrieval — execute the query against the right FTS5 table.

Take a
:class:`durin.memory.query_router.RoutingDecision` and run it against
the corresponding FTS5 path, returning a ranked list of URIs that
the RRF fusion step consumes.

The three execution paths run the same expression from
:func:`build_fts_expression` — loose tokens OR-joined (bm25 ranks partial
matches), balanced quoted phrases and every ``keywords`` token required
(AND):

  - ``UNICODE61``      → ``SELECT … FROM memory_fts WHERE text MATCH ?``
  - ``TRIGRAM``        → ``SELECT … FROM memory_fts_trigram WHERE text MATCH ?``
  - ``LIKE_SUBSTRING`` → the same required/optional split, expressed as
    ``LIKE`` clauses (no bm25 ranking — returned in table order)

Every term is double-quoted before FTS5 so special characters (``%``,
``*``, ``:``) and — critically — the FTS5 boolean keywords
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
    keywords = decision.keywords or decision.auto_keywords
    expr = build_fts_expression(decision.normalized_query, keywords)
    if not expr.text:
        if emit:
            _emit_lexical(decision=decision, hit_count=0,
                          required=expr.required, optional=expr.optional,
                          duration_ms=(time.perf_counter() - t0) * 1000.0)
        return hits

    if decision.route is LexicalRoute.UNICODE61:
        hits = index.search(
            expr.text, limit=limit, type_=type_,
            include_types=include_types, exclude_types=exclude_types,
        )
    elif decision.route is LexicalRoute.TRIGRAM:
        hits = index.search_trigram(
            expr.text, limit=limit, type_=type_,
            include_types=include_types, exclude_types=exclude_types,
        )
    elif decision.route is LexicalRoute.LIKE_SUBSTRING:
        required, optional = _split_terms(decision.normalized_query, keywords)
        hits = _like_substring_scan(
            index, optional, required=required, limit=limit, type_=type_,
            include_types=include_types, exclude_types=exclude_types,
        )

    if emit:
        _emit_lexical(
            decision=decision, hit_count=len(hits),
            required=expr.required, optional=expr.optional,
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
    required_raw, optional_raw = _split_terms(query, keywords)
    required = [_fts_term(t) for t in required_raw]
    optional = [_fts_term(t) for t in optional_raw]
    parts: list[str] = list(required)
    if optional:
        parts.append("(" + " OR ".join(optional) + ")")
    return FtsExpression(" AND ".join(parts), len(required), len(optional))


def _split_terms(query: str, keywords: str | None) -> tuple[list[str], list[str]]:
    """Raw, un-quoted ``(required, optional)`` terms behind one query.

    Required = balanced quoted phrases in ``query`` plus every token of
    ``keywords``. Optional = the loose (non-phrase) tokens of ``query``.
    Shared by :func:`build_fts_expression` (which quotes these for FTS5)
    and the LIKE fallback (which interpolates them directly into ``LIKE``
    patterns) so both routes agree on what counts as required.
    """
    phrases, loose, balanced = _extract_phrases(query or "")
    if not balanced:
        loose = [tok.replace('"', "") for tok in loose]
    required: list[str] = [p for p in phrases if p.strip()]
    if keywords and keywords.strip():
        kw_phrases, kw_loose, kw_balanced = _extract_phrases(keywords)
        if not kw_balanced:
            kw_loose = [tok.replace('"', "") for tok in kw_loose]
        required += [t for t in kw_loose if t]
        required += [p for p in kw_phrases if p.strip()]
    optional = [t for t in loose if t]
    return required, optional


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
    tokens: Sequence[str],
    *,
    required: Sequence[str] = (),
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

    ``tokens`` (the loose, optional terms) are OR-joined — any one
    substring match qualifies a row, mirroring the bm25-ranked OR
    group the FTS routes use. ``required`` (quoted phrases and
    ``keywords``) are ANDed on top, one ``LIKE`` per term — with no
    loose tokens the required clauses stand alone. LIKE has no
    ranking, so results come back in table order.

    ``type_``/``include_types``/``exclude_types`` build the same type
    clause as the FTS routes — see ``FTSIndex._type_clause`` and
    ``lexical_search``.
    """
    conn = index._conn  # noqa: SLF001 — intentional friend access
    clauses: list[str] = []
    params: list[str] = []
    if tokens:
        clauses.append("(" + " OR ".join("text LIKE ?" for _ in tokens) + ")")
        params.extend(f"%{t}%" for t in tokens)
    for term in required:
        clauses.append("text LIKE ?")
        params.append(f"%{term}%")
    type_clause, type_params = index._type_clause(type_, include_types, exclude_types)  # noqa: SLF001
    where = " AND ".join(clauses)
    cur = conn.execute(
        f"SELECT uri, path, type, entity_type FROM memory_fts "
        f"WHERE {where}{type_clause} LIMIT ?",
        (*params, *type_params, limit),
    )
    return [
        FTSHit(uri=u, path=p, type=t, entity_type=et)
        for (u, p, t, et) in cur.fetchall()
    ]


def _emit_lexical(
    *, decision: RoutingDecision, hit_count: int, required: int, optional: int,
    duration_ms: float,
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
                "required": required,
                "optional": optional,
                "duration_ms": duration_ms,
            },
        )
    except Exception:  # pragma: no cover
        pass
