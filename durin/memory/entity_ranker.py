"""L1 light retrieval ranker — pure functions for entity-aware re-ranking.

Per ``docs/18_entity_centric_plan.md`` §7 + Phase 3 of doc 19. Three
pieces that compose into a multi-signal score:

1. :func:`extract_query_entities` — case-insensitive lookup of the query
   against the :class:`AliasIndex`. Returns the list of candidate entity
   refs the query mentions (could be empty; could be N>1 for ambiguous
   aliases like ``marcelo``).
2. :func:`rank_with_entities` — given a vector-search result list and
   the set of cursors-per-entity, apply boost/demote:
   - boost candidates whose ``entities`` tag overlaps with query entities
     **and** are post-cursor (the info is fresh, not yet consolidated)
   - demote candidates whose ``entities`` tag overlaps and are
     pre-cursor (the info is already in the consolidated page; surfacing
     the raw entries duplicates context)
   - entity pages (``class_name == "entity_page"``) get their own boost
     when the page corresponds to one of the query entities — the
     canonical page should surface near the top for "tell me about X"
     queries.
3. Optional recency tiebreaker — leaves it to the caller (we don't make
   recency dominate; per discussion, weight is intrinsic, recency
   resolves ties).

All inputs are **pure data** (dicts and lists) — no side effects, no
LanceDB / disk access. Easy to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from durin.memory.aliases_index import AliasIndex

__all__ = [
    "BOOST_POST_CURSOR",
    "BOOST_ENTITY_PAGE",
    "DEMOTE_PRE_CURSOR",
    "extract_query_entities",
    "rank_with_entities",
    "RankedCandidate",
]


# Multipliers on the base vector-similarity score. Conservative starting
# values; refinable via Phase 0.2 telemetry once we have real usage data.
BOOST_POST_CURSOR = 1.5     # entry mentions query entity, written after cursor
BOOST_ENTITY_PAGE = 1.4     # the page itself for one of the query entities
DEMOTE_PRE_CURSOR = 0.7     # entry mentions query entity, already consolidated


@dataclass
class RankedCandidate:
    """One result with its adjusted score + applied signals (for debugging)."""

    record: dict[str, Any]
    base_score: float
    adjusted_score: float
    signals: list[str]


# ---------------------------------------------------------------------------
# extract_query_entities
# ---------------------------------------------------------------------------


def extract_query_entities(
    query: str,
    alias_index: AliasIndex,
) -> list[str]:
    """Find all entity refs mentioned by name/alias/identifier in *query*.

    Strategy: tokenize the query into windowed substrings and look each
    up in the alias index. The index is case-insensitive (already lower-
    folded internally), so the lookup is straightforward.

    - Single-word lookups always run (e.g. ``"marcelo"``).
    - Multi-word phrase lookups try contiguous N-grams up to a small
      cap (default 4 words) so ``"Marcelo Marmol"`` resolves even
      when only the full form is in the index.

    Returns a deduplicated, order-preserving list of entity refs
    (``type:slug``). Empty list when no matches.
    """
    if not isinstance(query, str) or not query.strip():
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []

    seen_refs: set[str] = set()
    out: list[str] = []

    # Try N-grams from longest to shortest so phrase matches take precedence
    # over single-word ones when both could resolve.
    for window in range(min(len(tokens), 4), 0, -1):
        for start in range(0, len(tokens) - window + 1):
            phrase = " ".join(tokens[start:start + window])
            for ref in alias_index.lookup(phrase):
                if ref not in seen_refs:
                    seen_refs.add(ref)
                    out.append(ref)

    return out


def _tokenize(query: str) -> list[str]:
    """Split a query into lowercase tokens, preserving common identifier chars.

    Tokens are alphanumeric + ``@ . - _ + : / `` — so emails, phones,
    paths, and entity refs survive as single tokens for direct lookup.
    """
    chunks: list[str] = []
    current: list[str] = []
    for ch in query:
        if ch.isalnum() or ch in "@.-_+:/":
            current.append(ch.lower())
        else:
            if current:
                chunks.append("".join(current))
                current = []
    if current:
        chunks.append("".join(current))
    return chunks


# ---------------------------------------------------------------------------
# rank_with_entities
# ---------------------------------------------------------------------------


def rank_with_entities(
    candidates: list[dict[str, Any]],
    *,
    query_entities: list[str],
    cursors: dict[str, int | str | None] | None = None,
    score_field: str = "_distance",
    higher_is_better: bool = False,
) -> list[RankedCandidate]:
    """Apply L1 light multi-factor ranking to vector-search results.

    Args:
        candidates: List of vector-index records. Each record should
            carry ``id``, ``class_name``, and ideally ``entities``
            (for memory entries — list of entity refs) and ``valid_from``
            (for cursor comparisons).
        query_entities: List of entity refs identified in the query.
            Empty list = no entity context; ranker falls back to base
            scores (returned with signals=[]).
        cursors: Optional dict of ``entity_ref → cursor_value``. Used
            to decide pre/post-cursor for memory entry candidates.
            Missing entities → no boost/demote applied.
        score_field: Name of the base similarity field in each record
            (``_distance`` for LanceDB; lower=closer).
        higher_is_better: Set True if base score is a similarity
            (higher=better). For LanceDB ``_distance``, False.

    Returns:
        List of :class:`RankedCandidate` sorted best-first.
    """
    cursors = cursors or {}
    query_entity_set = set(query_entities)

    ranked: list[RankedCandidate] = []
    for record in candidates:
        base_score = float(record.get(score_field, 0.0))
        # Normalize to a positive similarity-like score where higher=better.
        # For LanceDB distance (smaller=closer), use 1/(1+d) which maps
        # [0,inf) → (0,1] monotonically. For similarity scores already
        # in that shape, keep as-is. Multiplicative boost/demote then
        # behave intuitively (boost moves up, demote moves down).
        if higher_is_better:
            normalized = base_score
        else:
            normalized = 1.0 / (1.0 + max(base_score, 0.0))
        adjusted = normalized
        signals: list[str] = []

        class_name = record.get("class_name", "")

        # Signal 1: entity page for one of the query entities.
        if class_name == "entity_page":
            page_ref = record.get("id", "")
            if page_ref in query_entity_set:
                adjusted *= BOOST_ENTITY_PAGE
                signals.append(f"entity_page:{page_ref}")

        # Signal 2/3: memory entry with overlapping entity tags.
        record_entities = record.get("entities", []) or []
        if isinstance(record_entities, str):
            # Sometimes serialized as comma-list. Be lenient.
            record_entities = [e.strip() for e in record_entities.split(",") if e.strip()]
        overlap = [e for e in record_entities if e in query_entity_set]
        if overlap:
            # Decide post vs pre cursor. We compare the record's
            # ``valid_from`` (or any ts field) to each overlapping
            # entity's cursor. If post-cursor, boost; if pre-cursor,
            # demote. If no cursor known, default to boost (fresh
            # information not yet consolidated).
            entry_ts = record.get("valid_from") or record.get("created_at") or ""
            for ent in overlap:
                cursor = cursors.get(ent)
                is_pre_cursor = (
                    cursor is not None
                    and isinstance(entry_ts, str)
                    and entry_ts != ""
                    and isinstance(cursor, str)
                    and entry_ts <= cursor
                )
                if is_pre_cursor:
                    adjusted *= DEMOTE_PRE_CURSOR
                    signals.append(f"pre_cursor:{ent}")
                else:
                    adjusted *= BOOST_POST_CURSOR
                    signals.append(f"post_cursor:{ent}")

        ranked.append(RankedCandidate(
            record=record,
            base_score=base_score,
            adjusted_score=adjusted,
            signals=signals,
        ))

    # Sort best first by adjusted score (higher = better).
    ranked.sort(key=lambda r: r.adjusted_score, reverse=True)
    return ranked
