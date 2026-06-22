"""Reciprocal Rank Fusion (RRF) cross-source merge.

Merges vector + lexical + grep result lists into one ranked list using RRF
with k=60 and per-source weights. When the agent supplied ``keywords``, the
lexical weight is boosted from 0.7 to 2.5.
"""

from __future__ import annotations

from durin.memory.rrf_fusion import (
    DEFAULT_K,
    DEFAULT_W_GREP,
    DEFAULT_W_LEXICAL,
    DEFAULT_W_LEXICAL_BOOSTED,
    DEFAULT_W_VECTOR,
    fuse_rrf,
)

# ---------------------------------------------------------------------------
# Constants — locked to spec values
# ---------------------------------------------------------------------------


def test_constants_match_spec() -> None:
    assert DEFAULT_K == 60
    assert DEFAULT_W_VECTOR == 1.0
    assert DEFAULT_W_LEXICAL == 0.7
    assert DEFAULT_W_LEXICAL_BOOSTED == 2.5
    assert DEFAULT_W_GREP == 0.3


# ---------------------------------------------------------------------------
# Single-source — degenerate cases
# ---------------------------------------------------------------------------


def test_empty_sources_returns_empty() -> None:
    assert fuse_rrf(vector=[], lexical=[], grep=[]) == []


def test_single_vector_source_preserves_order() -> None:
    hits = fuse_rrf(
        vector=["a", "b", "c"], lexical=[], grep=[],
    )
    uris = [h.uri for h in hits]
    assert uris == ["a", "b", "c"]


def test_single_lexical_source_preserves_order() -> None:
    hits = fuse_rrf(vector=[], lexical=["x", "y"], grep=[])
    assert [h.uri for h in hits] == ["x", "y"]


# ---------------------------------------------------------------------------
# Cross-source — items in multiple sources rank higher
# ---------------------------------------------------------------------------


def test_uri_in_both_sources_beats_uri_in_one() -> None:
    hits = fuse_rrf(
        vector=["a", "b"],
        lexical=["b", "c"],
        grep=[],
    )
    # `b` appears in both sources, accumulating two RRF contributions;
    # `a` and `c` only have one. `b` should rank first.
    uris = [h.uri for h in hits]
    assert uris[0] == "b"


def test_three_source_intersection_ranks_first() -> None:
    """A uri found by vector + lexical + grep dominates one found by
    only two."""
    hits = fuse_rrf(
        vector=["x", "a"],
        lexical=["x", "b"],
        grep=["x", "c"],
    )
    assert hits[0].uri == "x"


def test_fused_score_includes_per_source_contribution() -> None:
    """The score should be the sum of `w_source / (k + rank)` for each
    source the uri appeared in."""
    hits = fuse_rrf(
        vector=["a"],          # rank 1
        lexical=["a"],         # rank 1
        grep=[],
    )
    expected = (
        DEFAULT_W_VECTOR / (DEFAULT_K + 1)
        + DEFAULT_W_LEXICAL / (DEFAULT_K + 1)
    )
    assert hits[0].uri == "a"
    assert abs(hits[0].score - expected) < 1e-9


def test_dedup_keeps_single_row_per_uri() -> None:
    hits = fuse_rrf(
        vector=["a", "a"],     # duplicate in source (defensive)
        lexical=["a"],
        grep=[],
    )
    # Duplicate uris within a single source should not be double-counted.
    assert sum(1 for h in hits if h.uri == "a") == 1


# ---------------------------------------------------------------------------
# Score ordering
# ---------------------------------------------------------------------------


def test_results_sorted_score_desc() -> None:
    hits = fuse_rrf(
        vector=["a", "b", "c", "d", "e"],
        lexical=["b", "f"],
        grep=[],
    )
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Dynamic boost when `keywords` is set
# ---------------------------------------------------------------------------


def test_keywords_boost_elevates_lexical_only_hit() -> None:
    """Without keyword boost, vector-only hit `a` (rank 1) beats
    lexical-only hit `b` (rank 1). With the boost, `b` overtakes."""
    no_boost = fuse_rrf(
        vector=["a"], lexical=["b"], grep=[],
    )
    boosted = fuse_rrf(
        vector=["a"], lexical=["b"], grep=[],
        keywords_provided=True,
    )
    assert no_boost[0].uri == "a"
    assert boosted[0].uri == "b"


def test_boost_does_not_change_grep_weight() -> None:
    """Grep weight stays 0.3 even when keywords is set — the boost is
    intentionally lexical-only."""
    hits = fuse_rrf(
        vector=[], lexical=[], grep=["g"],
        keywords_provided=True,
    )
    expected = DEFAULT_W_GREP / (DEFAULT_K + 1)
    assert abs(hits[0].score - expected) < 1e-9


# ---------------------------------------------------------------------------
# Result-row shape
# ---------------------------------------------------------------------------


def test_fused_hit_records_source_membership() -> None:
    """Downstream (sectioned output, entity rerank) needs to know
    which source(s) contributed to each fused hit."""
    hits = fuse_rrf(
        vector=["a"], lexical=["a"], grep=[],
    )
    hit = hits[0]
    assert hit.uri == "a"
    assert "vector" in hit.sources
    assert "lexical" in hit.sources
    assert "grep" not in hit.sources


def test_fused_hit_records_ranks_per_source() -> None:
    """The per-source rank is useful for diagnostics + dashboards."""
    hits = fuse_rrf(
        vector=["a", "b"], lexical=["b", "a"], grep=[],
    )
    by_uri = {h.uri: h for h in hits}
    assert by_uri["a"].ranks == {"vector": 1, "lexical": 2}
    assert by_uri["b"].ranks == {"vector": 2, "lexical": 1}


# ---------------------------------------------------------------------------
# Type priors — distilled content outranks raw transcript at equal evidence
# ---------------------------------------------------------------------------


def test_type_priors_constant_is_soft_and_session_only() -> None:
    """The prior encodes ONE structural judgment: a raw session turn
    is less distilled than curated memory, so at comparable relevance
    the distillate should lead. Soft (>= 0.8) so a session that is
    clearly the better match still wins; nothing else is demoted."""
    from durin.memory.rrf_fusion import DEFAULT_TYPE_PRIORS

    assert DEFAULT_TYPE_PRIORS["session"] < 1.0
    assert DEFAULT_TYPE_PRIORS["session"] >= 0.8
    assert set(DEFAULT_TYPE_PRIORS) == {"session"}


def test_apply_type_priors_scales_and_resorts() -> None:
    from durin.memory.rrf_fusion import apply_type_priors

    hits = fuse_rrf(
        vector=[], lexical=["session-uri", "entry-uri"],
        grep=["session-uri", "entry-uri"],
    )
    assert hits[0].uri == "session-uri"  # lexical+grep rank 1
    out = apply_type_priors(
        hits, types={"session-uri": "session", "entry-uri": "episodic"},
    )
    assert [h.uri for h in out] == ["entry-uri", "session-uri"]
    by_uri = {h.uri: h for h in out}
    orig = {h.uri: h for h in hits}
    assert by_uri["entry-uri"].score == orig["entry-uri"].score
    assert by_uri["session-uri"].score < orig["session-uri"].score


def test_apply_type_priors_preserves_sources_and_ranks() -> None:
    from durin.memory.rrf_fusion import apply_type_priors

    hits = fuse_rrf(vector=["s"], lexical=["s"], grep=[])
    out = apply_type_priors(hits, types={"s": "session"})
    assert out[0].sources == hits[0].sources
    assert out[0].ranks == hits[0].ranks


def test_apply_type_priors_unknown_type_is_neutral() -> None:
    from durin.memory.rrf_fusion import apply_type_priors

    hits = fuse_rrf(vector=["x"], lexical=[], grep=[])
    out = apply_type_priors(hits, types={})
    assert out[0].score == hits[0].score
