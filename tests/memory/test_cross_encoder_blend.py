"""Unit tests for the enriched-input + blended cross-encoder
(P-CE-blend, 2026-06-11; doc 03 §9.2).

Validates the two design choices the offline recall@10 sweep
established, deterministically:

1. The CE scores enriched text (headline + date + summary), not the
   bare snippet — `_rerank_doc_text`.
2. The CE is BLENDED with RRF (α=0.4·zscore(ce) + 0.6·zscore(rrf)),
   not a full reorder: a hit the RRF stage scored high survives a low
   CE score (Marcelo's "why lower something that already scored high").
"""

from __future__ import annotations

import datetime

from durin.memory.rrf_fusion import FusedHit
from durin.memory.search_pipeline import (
    DEFAULT_BLEND_ALPHA,
    _cross_encoder_rerank,
    _rerank_doc_text,
    _zscore,
)

# --- _zscore ---------------------------------------------------------------


def test_zscore_standardises():
    out = _zscore([1.0, 2.0, 3.0])
    assert abs(sum(out)) < 1e-9          # mean 0
    assert out[0] < out[1] < out[2]      # order preserved


def test_zscore_degenerate_is_zeros():
    assert _zscore([]) == []
    assert _zscore([5.0]) == [0.0]
    assert _zscore([7.0, 7.0, 7.0]) == [0.0, 0.0, 0.0]  # zero variance


# --- _rerank_doc_text ------------------------------------------------------


def test_doc_text_enriches_with_headline_date_summary():
    meta = {
        "headline": "Joanna writes",
        "valid_from": datetime.date(2023, 5, 7),
        "summary": "has a stuffed dog Tilly while writing",
    }
    doc = _rerank_doc_text(meta, "memory/episodic/x")
    assert "Joanna writes" in doc
    assert "2023-05-07" in doc            # date is now visible to the CE
    assert "Tilly" in doc


def test_doc_text_falls_back_to_snippet_then_uri():
    assert _rerank_doc_text(
        {"snippet": "only a snippet"}, "u",
    ) == "only a snippet"
    assert _rerank_doc_text({}, "memory/episodic/x") == "memory/episodic/x"


# --- blend behaviour -------------------------------------------------------


class _Scorer:
    """Returns CE scores keyed by the gold token embedded in each doc."""

    def __init__(self, by_token: dict[str, float]):
        self._by_token = by_token

    def score(self, query, docs):
        return [
            next((v for tok, v in self._by_token.items() if tok in d), 0.0)
            for d in docs
        ]


def _hit(uri: str, score: float) -> FusedHit:
    return FusedHit(uri=uri, score=score, sources=("vector",), ranks={})


def _meta(token: str) -> dict:
    return {"headline": token, "summary": token}


def test_high_rrf_hit_survives_low_ce(monkeypatch):
    """The core blend guarantee: a hit the RRF stage ranked clearly
    first is NOT expelled by a low CE score (α=0.4 < 0.6)."""
    fused = [_hit("u1", 10.0), _hit("u2", 5.0), _hit("u3", 4.0)]
    vector_meta = {"u1": _meta("AAA"), "u2": _meta("BBB"), "u3": _meta("CCC")}
    # CE hates u1, mildly likes the others.
    reranker = _Scorer({"AAA": 0.0, "BBB": 0.5, "CCC": 0.5})

    out = _cross_encoder_rerank(
        reranker, "q", fused,
        vector_meta=vector_meta, lexical_meta={}, grep_meta={},
        top_n=10,
    )
    assert out[0].uri == "u1"            # RRF-strong hit kept on top


def test_ce_breaks_rrf_ties(monkeypatch):
    """When RRF is tied, the CE decides — it nudges, it just can't veto
    a strong RRF lead."""
    fused = [_hit("u1", 5.0), _hit("u2", 5.0), _hit("u3", 5.0)]
    vector_meta = {"u1": _meta("AAA"), "u2": _meta("BBB"), "u3": _meta("CCC")}
    reranker = _Scorer({"AAA": 0.0, "BBB": 9.0, "CCC": 0.0})

    out = _cross_encoder_rerank(
        reranker, "q", fused,
        vector_meta=vector_meta, lexical_meta={}, grep_meta={},
        top_n=10,
    )
    assert out[0].uri == "u2"            # CE outlier wins the tie


def test_ce_failure_keeps_rrf_order(monkeypatch):
    class _Broken:
        def score(self, query, docs):
            return None

    fused = [_hit("u1", 10.0), _hit("u2", 5.0)]
    out = _cross_encoder_rerank(
        _Broken(), "q", fused,
        vector_meta={"u1": _meta("A"), "u2": _meta("B")},
        lexical_meta={}, grep_meta={}, top_n=10,
    )
    assert [h.uri for h in out] == ["u1", "u2"]   # untouched


def test_blend_alpha_is_calibrated_value():
    assert DEFAULT_BLEND_ALPHA == 0.4
