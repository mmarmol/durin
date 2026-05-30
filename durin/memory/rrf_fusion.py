"""Reciprocal Rank Fusion (RRF) — cross-source merge for the search
pipeline.

Per `docs/architecture/memory/03_search_pipeline.md` §7:

  RRF_score(uri) = Σ over sources:  w_source / (k + rank_in_source(uri))

with ``k = 60`` (Cormack/Clarke/Buettcher 2009, the de-facto IR
default) and per-source weights:

  - ``w_vector  = 1.0``
  - ``w_lexical = 0.7`` (boosted to ``2.5`` when the agent supplied
    a ``keywords`` parameter — see doc 03 §7.2)
  - ``w_grep    = 0.3``

A uri appearing in multiple sources accumulates contributions and
ranks higher. The algorithm operates in rank space so it's
score-scale invariant — works for cosine, L2, BM25, and grep.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

__all__ = [
    "DEFAULT_K",
    "DEFAULT_W_GREP",
    "DEFAULT_W_LEXICAL",
    "DEFAULT_W_LEXICAL_BOOSTED",
    "DEFAULT_W_VECTOR",
    "FusedHit",
    "fuse_rrf",
]


DEFAULT_K: int = 60
DEFAULT_W_VECTOR: float = 1.0
DEFAULT_W_LEXICAL: float = 0.7
DEFAULT_W_LEXICAL_BOOSTED: float = 2.5
DEFAULT_W_GREP: float = 0.3


@dataclass(frozen=True)
class FusedHit:
    """One uri's fused row.

    ``sources`` and ``ranks`` are diagnostics: dashboards can split
    "found by both" from "found by lexical only", and the
    sectioned-output renderer uses ``sources`` to tell the LLM where
    each block originated.
    """

    uri: str
    score: float
    sources: tuple[str, ...]
    ranks: dict[str, int] = field(default_factory=dict)


def fuse_rrf(
    *,
    vector: Sequence[str],
    lexical: Sequence[str],
    grep: Sequence[str],
    keywords_provided: bool = False,
    k: int = DEFAULT_K,
    w_vector: float = DEFAULT_W_VECTOR,
    w_lexical: float | None = None,
    w_grep: float = DEFAULT_W_GREP,
    emit_telemetry: bool = True,
) -> list[FusedHit]:
    """Fuse three ranked URI lists into one ranked list.

    Each input is a sequence of URIs in best-first order. Duplicates
    within a single source are ignored (first occurrence wins on
    rank).

    When ``keywords_provided`` is True and ``w_lexical`` was left at
    its default, the lexical weight is boosted to
    :data:`DEFAULT_W_LEXICAL_BOOSTED` per doc 03 §7.2. An explicit
    ``w_lexical`` override is honoured regardless.
    """
    if w_lexical is None:
        w_lexical = (
            DEFAULT_W_LEXICAL_BOOSTED if keywords_provided
            else DEFAULT_W_LEXICAL
        )

    t0 = time.perf_counter()
    # Accumulator: uri → (score, sources_set, ranks_dict)
    acc: dict[str, _Accum] = {}
    vec_list = list(vector)
    lex_list = list(lexical)
    grep_list = list(grep)
    _accumulate(acc, vec_list, source="vector", weight=w_vector, k=k)
    _accumulate(acc, lex_list, source="lexical", weight=w_lexical, k=k)
    _accumulate(acc, grep_list, source="grep", weight=w_grep, k=k)

    hits = [
        FusedHit(
            uri=uri,
            score=a.score,
            sources=tuple(sorted(a.sources)),
            ranks=dict(a.ranks),
        )
        for uri, a in acc.items()
    ]
    hits.sort(key=lambda h: h.score, reverse=True)

    if emit_telemetry:
        _emit_rrf(
            vector_count=len(vec_list),
            lexical_count=len(lex_list),
            grep_count=len(grep_list),
            fused_count=len(hits),
            boosted=keywords_provided,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
        )

    return hits


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


@dataclass
class _Accum:
    score: float = 0.0
    sources: set[str] = field(default_factory=set)
    ranks: dict[str, int] = field(default_factory=dict)


def _emit_rrf(
    *,
    vector_count: int,
    lexical_count: int,
    grep_count: int,
    fused_count: int,
    boosted: bool,
    duration_ms: float,
) -> None:
    """Best-effort telemetry — never raises."""
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event(
            "memory.recall.rrf",
            {
                "vector_count": vector_count,
                "lexical_count": lexical_count,
                "grep_count": grep_count,
                "fused_count": fused_count,
                "boosted": boosted,
                "duration_ms": duration_ms,
            },
        )
    except Exception:  # pragma: no cover
        pass


def _accumulate(
    acc: dict[str, _Accum],
    uris: Iterable[str],
    *,
    source: str,
    weight: float,
    k: int,
) -> None:
    seen_in_source: set[str] = set()
    rank = 0
    for uri in uris:
        if not isinstance(uri, str) or not uri:
            continue
        if uri in seen_in_source:
            continue
        seen_in_source.add(uri)
        rank += 1
        a = acc.setdefault(uri, _Accum())
        a.score += weight / (k + rank)
        a.sources.add(source)
        a.ranks[source] = rank
