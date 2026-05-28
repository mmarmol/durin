"""L1 light retrieval ranker — Reciprocal Rank Fusion (RRF) over signals.

Per doc 18 §7 + doc 23 T1.3 (post glm peer review, doc 22 A2 validated).

Two pure functions:

1. :func:`extract_query_entities` — case-insensitive lookup of the query
   against the :class:`AliasIndex`. Returns the list of candidate entity
   refs the query mentions (could be empty; could be N>1 for ambiguous
   aliases like ``marcelo``).
2. :func:`rank_with_entities` — applies **Reciprocal Rank Fusion (RRF)**
   over two rankings derived from the candidates:
   - **Vector ranking**: the input candidates ordered by their base
     similarity score (typically LanceDB ``_distance``, lower=better).
   - **Entity-match ranking**: entity-pages whose id matches a query
     entity (any order, see note below), then memory entries tagged
     with any query entity AND post-cursor (newer than the entity's
     ``dream_processed_through``), ordered by recency. Pre-cursor
     tagged entries are EXCLUDED from this list (their info lives in
     the page; surfacing the raw entry duplicates context).

   For each candidate, final score = ``Σ 1 / (rank_in_list + RRF_K)``
   across lists in which it appears. Sorted descending.

**Why RRF instead of multiplicative boost** (doc 22 A2 + doc 23 §9 G):
LanceDB L2 distances are non-linear and corpus-dependent (can be 10-50
for poorly-normalized embeddings); applying ``score × 1.5`` over a
``1/(1+d)`` normalization distorts ordering rather than improving it.
RRF works in **rank space**, invariant to score-scale differences.
Standard approach (Graphiti uses it; Cormack et al. 2009).

**List-length asymmetry note** (G9): the entity-match list is typically
much shorter than the vector list (3-5 items vs 50-100). Contributions
``1/(rank+K)`` thus give the entity signal less aggregate weight than
the vector signal. **This is deliberate** — entity matching is a nudge
to surface the canonical page + fresh entries, not an override of
semantic similarity.

**Pages order note** (G13): when query mentions N>1 entities, the
``pages_for_query`` sub-list preserves the order in which they appear
in the input ``candidates`` (which is vector-sort order). No internal
ranking among multiple pages for query entities is performed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pathlib import Path

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage

__all__ = [
    "RRF_K",
    "extract_query_entities",
    "rank_with_entities",
    "load_cursors_from_entities_dir",
    "RankedCandidate",
]


def load_cursors_from_entities_dir(
    memory_root: Path,
    entity_refs: list[str],
) -> dict[str, Any]:
    """Read ``dream_processed_through`` from each entity's page (E11).

    Returns ``{entity_ref: cursor_value}`` for refs whose page exists
    and has a cursor field. The cursor map feeds ``rank_with_entities``
    so the pre/post-cursor partitioning in §8.4 of doc 03 actually
    applies — pre-E11 the v2 pipeline never loaded cursors and treated
    every tagged entry as post-cursor.

    Best-effort — missing pages or parse errors skip silently.

    History: pre-E11 this lived as ``_load_cursors_from_entities_dir``
    in ``durin/agent/tools/memory_search.py`` (v1 path consumer). When
    the v1 path was removed in commit c820447 (Phase 5 d1 migration),
    the helper was orphaned. Audit E11 (2026-05-28) moved it here next
    to ``rank_with_entities``, the only consumer that needs it.
    """
    cursors: dict[str, Any] = {}
    memory_root = Path(memory_root)
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


# Constant from Cormack, Clarke, Buettcher 2009 ("Reciprocal Rank Fusion
# outperforms Condorcet and individual Rank Learning Methods"). k=60 is
# the standard. Smaller k weights top ranks more; larger k flattens.
RRF_K = 60


@dataclass
class RankedCandidate:
    """One result with its fused score + signals (which lists contributed)."""

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

    Strategy: tokenize the query into N-gram windows, look each up in
    the alias index. The index is case-insensitive (already lower-folded
    internally).

    - Single-word lookups always run (e.g. ``"marcelo"``).
    - Multi-word phrase lookups try contiguous N-grams up to a small
      cap (default 4 words) so ``"Marcelo Marmol"`` resolves.

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

    # Try N-grams from longest to shortest so phrase matches take precedence.
    for window in range(min(len(tokens), 4), 0, -1):
        for start in range(0, len(tokens) - window + 1):
            phrase = " ".join(tokens[start:start + window])
            for ref in alias_index.lookup(phrase):
                if ref not in seen_refs:
                    seen_refs.add(ref)
                    out.append(ref)

    return out


def _tokenize(query: str) -> list[str]:
    """Split query into lowercase tokens, preserving common identifier chars."""
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
# Cursor comparison (G3): parse ISO datetimes, never compare raw strings.
# ---------------------------------------------------------------------------


def _is_pre_cursor(entry_ts: str | None, cursor: Any) -> bool:
    """True iff entry timestamp is at or before the entity's cursor.

    Parses both as ISO datetimes (handles ``2026-04-01``, ``2026-04-01T...``,
    trailing ``Z``). String comparison is intentionally avoided per glm
    G3: ``"2024-01-15T10:30:00" <= "2024-01-15"`` is False
    lexicographically but the entry IS post the date-only cursor in
    real time. Numeric cursors (msg_idx) are not comparable to ISO ts
    and return False (treat as "not pre-cursor", default to boost).
    """
    if not entry_ts or cursor is None:
        return False
    # Integer cursor (msg_idx) cannot be compared to ISO timestamp.
    if isinstance(cursor, (int, float)):
        return False
    try:
        et = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
        ct = datetime.fromisoformat(str(cursor).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False  # malformed → fail open (don't filter out)
    return et <= ct


# ---------------------------------------------------------------------------
# rank_with_entities (RRF)
# ---------------------------------------------------------------------------


def _require_id(c: dict[str, Any]) -> str:
    """Extract the ``id`` field, failing fast if missing (G4).

    The score-fusion in RRF uses ``id`` as the dictionary key; missing/
    empty ids collapse distinct candidates into the same slot and yield
    incorrect cumulative scores. Better to fail loudly at the boundary.
    """
    did = c.get("id")
    if not isinstance(did, str) or not did:
        raise ValueError(
            f"candidate missing required string 'id' field; "
            f"keys={list(c.keys())}"
        )
    return did


def rank_with_entities(
    candidates: list[dict[str, Any]],
    *,
    query_entities: list[str],
    cursors: dict[str, Any] | None = None,
    score_field: str = "_distance",
    higher_is_better: bool = False,
) -> list[RankedCandidate]:
    """Multi-signal ranking via Reciprocal Rank Fusion (RRF).

    Composes two rankings of the same candidates:

    - **Vector rank**: candidates sorted by ``score_field`` (default
      LanceDB ``_distance``, lower=better; flip with ``higher_is_better``).
    - **Entity-match rank**: entity-pages whose ``id`` ∈ query_entities,
      then memory entries tagged with any query entity AND post-cursor,
      ordered by recency. Pre-cursor entries are excluded.

    Final score for each candidate = sum of ``1/(rank + RRF_K)`` across
    lists in which the candidate's id appears. Sorted descending.

    Args:
        candidates: List of dicts. Each MUST have an ``id`` field
            (string, non-empty). G4: missing id raises ValueError.
        query_entities: Entity refs identified in the query (output of
            :func:`extract_query_entities`). Empty list → ranking falls
            back to vector ranking only.
        cursors: Optional ``entity_ref → cursor_value`` map.
            ``cursor_value`` may be ISO datetime string or int msg_idx;
            G3 handles both safely.
        score_field: Name of the base similarity field. Default
            ``"_distance"`` (LanceDB).
        higher_is_better: Set True if base score is similarity (higher=better).

    Returns:
        List of :class:`RankedCandidate` sorted best-first (highest
        ``adjusted_score`` first).

    Raises:
        ValueError: when any candidate is missing the ``id`` field.
    """
    cursors = cursors or {}
    query_entity_set = set(query_entities)

    # --- Vector ranking ---------------------------------------------------
    def base_sort_key(c: dict[str, Any]) -> float:
        v = float(c.get(score_field, 0.0))
        return v if not higher_is_better else -v

    by_vector = sorted(candidates, key=base_sort_key)

    # --- Entity-match ranking ---------------------------------------------
    # Pages for query entities, then post-cursor tagged entries by recency.
    # G13: order among pages for different query entities is the order
    # they appear in ``candidates`` (typically vector-sort order). No
    # additional sorting is performed across pages.
    pages_for_query: list[dict[str, Any]] = []
    tagged_post_cursor: list[tuple[str, dict[str, Any]]] = []

    if query_entity_set:
        for c in candidates:
            class_name = c.get("class_name", "")
            if class_name == "entity_page":
                if c.get("id") in query_entity_set:
                    pages_for_query.append(c)
                continue
            # Memory entry — check overlap of its entities with query.
            recs = c.get("entities", []) or []
            if isinstance(recs, str):
                recs = [e.strip() for e in recs.split(",") if e.strip()]
            overlap = [e for e in recs if e in query_entity_set]
            if not overlap:
                continue
            entry_ts = c.get("valid_from") or c.get("created_at") or ""
            # G2/G3: cursor compare uses datetime, not string.
            is_pre = any(_is_pre_cursor(entry_ts, cursors.get(e)) for e in overlap)
            if not is_pre:
                tagged_post_cursor.append((str(entry_ts), c))

        # Newest first within tagged-post group.
        tagged_post_cursor.sort(key=lambda t: t[0], reverse=True)

    entity_rank_list: list[dict[str, Any]] = (
        pages_for_query + [t[1] for t in tagged_post_cursor]
    )

    # --- RRF fusion -------------------------------------------------------
    scores: dict[str, float] = {}
    signals_by_id: dict[str, list[str]] = {}

    for rank, c in enumerate(by_vector):
        did = _require_id(c)
        scores[did] = scores.get(did, 0.0) + 1.0 / (rank + RRF_K)
        signals_by_id.setdefault(did, []).append(f"vector_rank:{rank}")

    for rank, c in enumerate(entity_rank_list):
        did = _require_id(c)
        scores[did] = scores.get(did, 0.0) + 1.0 / (rank + RRF_K)
        # Annotate which sub-signal contributed.
        if c.get("class_name") == "entity_page":
            signals_by_id.setdefault(did, []).append(f"entity_page_rank:{rank}")
        else:
            signals_by_id.setdefault(did, []).append(f"post_cursor_rank:{rank}")

    # --- Build output -----------------------------------------------------
    ranked: list[RankedCandidate] = []
    for c in candidates:
        did = _require_id(c)
        ranked.append(RankedCandidate(
            record=c,
            base_score=float(c.get(score_field, 0.0)),
            adjusted_score=scores.get(did, 0.0),
            signals=signals_by_id.get(did, []),
        ))

    # Sort best first by RRF score.
    ranked.sort(key=lambda r: r.adjusted_score, reverse=True)
    return ranked
