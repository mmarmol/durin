"""Tests for L1 light retrieval ranker (durin.memory.entity_ranker)."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.memory.entity_ranker import (
    RRF_K,
    extract_query_entities,
    rank_with_entities,
)


# ---------------------------------------------------------------------------
# extract_query_entities
# ---------------------------------------------------------------------------


class TestExtractQueryEntities:
    @pytest.fixture
    def populated_index(self, tmp_path: Path) -> AliasIndex:
        idx = AliasIndex(tmp_path)
        idx.add(
            EntityPage(
                type="person",
                name="Marcelo Marmol",
                aliases=["Marcelo", "marcelo"],
                extra={"identifiers": ["mmarmol@mxhero.com"]},
            ),
            slug="marcelo",
        )
        idx.add(
            EntityPage(type="project", name="durin", aliases=["Durin"]),
            slug="durin",
        )
        return idx

    def test_single_word_match(self, populated_index: AliasIndex) -> None:
        out = extract_query_entities("what does Marcelo prefer", populated_index)
        assert "person:marcelo" in out

    def test_case_insensitive(self, populated_index: AliasIndex) -> None:
        assert extract_query_entities("MARCELO prefers X", populated_index) == [
            "person:marcelo"
        ]

    def test_multi_word_phrase_match(self, populated_index: AliasIndex) -> None:
        out = extract_query_entities("about Marcelo Marmol's work", populated_index)
        assert "person:marcelo" in out

    def test_email_token_resolves(self, populated_index: AliasIndex) -> None:
        out = extract_query_entities(
            "user mmarmol@mxhero.com sent a message", populated_index
        )
        assert "person:marcelo" in out

    def test_multiple_entities_in_query(self, populated_index: AliasIndex) -> None:
        out = extract_query_entities(
            "did Marcelo decide on durin embeddings?", populated_index
        )
        assert "person:marcelo" in out
        assert "project:durin" in out

    def test_no_match_returns_empty(self, populated_index: AliasIndex) -> None:
        assert extract_query_entities("random question", populated_index) == []

    def test_empty_query_returns_empty(self, populated_index: AliasIndex) -> None:
        assert extract_query_entities("", populated_index) == []
        assert extract_query_entities("   \n", populated_index) == []

    def test_ambiguous_alias_returns_all_candidates(self, tmp_path: Path) -> None:
        """When 'marcelo' could match 2 entities, both returned (R6)."""
        idx = AliasIndex(tmp_path)
        idx.add(
            EntityPage(type="person", name="Marcelo Marmol",
                       aliases=["Marcelo", "marcelo"]),
            slug="marcelo_marmol",
        )
        idx.add(
            EntityPage(type="person", name="Marcelo Diaz",
                       aliases=["Marcelo", "marcelo"]),
            slug="marcelo_diaz",
        )
        out = extract_query_entities("ask Marcelo about it", idx)
        assert set(out) == {"person:marcelo_marmol", "person:marcelo_diaz"}


# ---------------------------------------------------------------------------
# rank_with_entities
# ---------------------------------------------------------------------------


class TestRankWithEntities:
    def test_no_query_entities_uses_only_vector_rank(self) -> None:
        """Without entity context, only vector ranking contributes."""
        candidates = [
            {"id": "a", "_distance": 0.1, "class_name": "episodic"},
            {"id": "b", "_distance": 0.2, "class_name": "episodic"},
        ]
        ranked = rank_with_entities(candidates, query_entities=[])
        assert [r.record["id"] for r in ranked] == ["a", "b"]
        # Each should have one vector_rank signal
        for r in ranked:
            assert any("vector_rank:" in s for s in r.signals)

    def test_entity_page_for_query_entity_boosted(self) -> None:
        """When a query mentions person:marcelo, the marcelo PAGE surfaces.

        With RRF: page gets vector_rank + entity_page_rank → fused score
        higher than memX which only has vector_rank.
        """
        candidates = [
            {"id": "person:marcelo", "_distance": 0.3, "class_name": "entity_page"},
            {"id": "memX", "_distance": 0.1, "class_name": "episodic"},
        ]
        ranked = rank_with_entities(
            candidates, query_entities=["person:marcelo"],
        )
        # Page appears in BOTH vector list and entity list → higher RRF.
        assert ranked[0].record["id"] == "person:marcelo"
        assert any("entity_page_rank:" in s for s in ranked[0].signals)

    def test_memory_entry_with_matching_tag_post_cursor_boosted(self) -> None:
        """An entry post-cursor about person:marcelo gets RRF boost."""
        candidates = [
            {
                "id": "fresh",
                "_distance": 0.3,
                "class_name": "episodic",
                "entities": ["person:marcelo"],
                "valid_from": "2026-05-23",
            },
            {
                "id": "baseline",
                "_distance": 0.25,
                "class_name": "episodic",
                "entities": [],
                "valid_from": "2026-05-23",
            },
        ]
        ranked = rank_with_entities(
            candidates,
            query_entities=["person:marcelo"],
        )
        assert ranked[0].record["id"] == "fresh"
        assert any("tagged_rank:" in s for s in ranked[0].signals)

    def test_combined_realistic_mix(self) -> None:
        """Page + tagged entries + neutral entry.

        Per doc 18 §3.4 (read-time reconciliation), the page and tagged
        entries COEXIST in results — the LLM reconciles. We require the
        canonical page and the freshest tagged entry in the top 2; the
        untagged neutral entry (vector signal only) ranks last.
        """
        candidates = [
            {"id": "neutral", "_distance": 0.5, "class_name": "episodic",
             "entities": [], "valid_from": "2026-05-23"},
            {"id": "person:marcelo", "_distance": 0.6, "class_name": "entity_page"},
            {"id": "fresh", "_distance": 0.55, "class_name": "episodic",
             "entities": ["person:marcelo"], "valid_from": "2026-05-23"},
            {"id": "old", "_distance": 0.55, "class_name": "episodic",
             "entities": ["person:marcelo"], "valid_from": "2026-04-01"},
        ]
        ranked = rank_with_entities(
            candidates,
            query_entities=["person:marcelo"],
        )
        ids = [r.record["id"] for r in ranked]
        # Page + freshest tagged entry lead (order between them not asserted).
        assert set(ids[:2]) == {"person:marcelo", "fresh"}
        # The untagged neutral entry (vector signal only) ranks last.
        assert ids[-1] == "neutral"

    def test_higher_is_better_score_handled(self) -> None:
        """When base score is similarity (higher=better), ranking respects that."""
        candidates = [
            {"id": "low_sim", "similarity": 0.3, "class_name": "episodic"},
            {"id": "high_sim", "similarity": 0.9, "class_name": "episodic"},
        ]
        ranked = rank_with_entities(
            candidates,
            query_entities=[],
            score_field="similarity",
            higher_is_better=True,
        )
        assert ranked[0].record["id"] == "high_sim"

    def test_tagged_entry_boosted(self) -> None:
        """A memory entry tagged with a query entity gets the entity-match boost."""
        candidates = [
            {
                "id": "tagged",
                "_distance": 0.2,
                "class_name": "episodic",
                "entities": ["person:marcelo"],
                "valid_from": "2026-05-23",
            },
        ]
        ranked = rank_with_entities(
            candidates,
            query_entities=["person:marcelo"],
        )
        assert any("tagged_rank:" in s for s in ranked[0].signals)


# ---------------------------------------------------------------------------
# RRF-specific behavior (new tests for the refactor)
# ---------------------------------------------------------------------------


class TestRRFBehavior:
    def test_doc_in_both_lists_fuses_scores(self) -> None:
        """A doc appearing in both vector and entity lists gets summed RRF."""
        candidates = [
            {"id": "person:marcelo", "_distance": 0.3,
             "class_name": "entity_page"},
            {"id": "other", "_distance": 0.5, "class_name": "episodic",
             "entities": []},
        ]
        ranked = rank_with_entities(
            candidates, query_entities=["person:marcelo"],
        )
        # person:marcelo: vector_rank=0 + entity_page_rank=0
        # other: vector_rank=1 only
        # → 2/(0+K) > 1/(1+K) always
        page = next(r for r in ranked if r.record["id"] == "person:marcelo")
        other = next(r for r in ranked if r.record["id"] == "other")
        assert page.adjusted_score > other.adjusted_score
        # Page has TWO signals (vector + entity_page), other has ONE
        assert len(page.signals) == 2
        assert len(other.signals) == 1

    def test_missing_id_raises(self) -> None:
        """G4: candidate without id field fails fast (no silent collision)."""
        import pytest
        candidates = [{"_distance": 0.1, "class_name": "episodic"}]
        with pytest.raises(ValueError, match="missing required.*id"):
            rank_with_entities(candidates, query_entities=[])

    def test_empty_id_raises(self) -> None:
        """G4: empty string id is treated as missing."""
        import pytest
        candidates = [{"id": "", "_distance": 0.1, "class_name": "episodic"}]
        with pytest.raises(ValueError, match="missing required.*id"):
            rank_with_entities(candidates, query_entities=[])

    def test_rrf_k_constant_exposed(self) -> None:
        """RRF_K is a module-level constant matching the standard k=60."""
        assert RRF_K == 60
