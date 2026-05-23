"""Tests for L1 light retrieval ranker (durin.memory.entity_ranker)."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.memory.entity_ranker import (
    BOOST_ENTITY_PAGE,
    BOOST_POST_CURSOR,
    DEMOTE_PRE_CURSOR,
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
    def test_no_query_entities_preserves_order(self) -> None:
        """Without entity context, base score order is preserved."""
        candidates = [
            {"id": "a", "_distance": 0.1, "class_name": "episodic"},
            {"id": "b", "_distance": 0.2, "class_name": "episodic"},
        ]
        ranked = rank_with_entities(candidates, query_entities=[])
        assert [r.record["id"] for r in ranked] == ["a", "b"]
        # No signals applied
        assert all(r.signals == [] for r in ranked)

    def test_entity_page_for_query_entity_boosted(self) -> None:
        """When a query mentions person:marcelo, the marcelo PAGE surfaces."""
        candidates = [
            {"id": "person:marcelo", "_distance": 0.3, "class_name": "entity_page"},
            {"id": "memX", "_distance": 0.1, "class_name": "episodic"},
        ]
        ranked = rank_with_entities(
            candidates, query_entities=["person:marcelo"],
        )
        # Without boost, memX (distance 0.1) would win. With boost, page wins.
        assert ranked[0].record["id"] == "person:marcelo"
        assert any("entity_page:" in s for s in ranked[0].signals)

    def test_memory_entry_with_matching_tag_post_cursor_boosted(self) -> None:
        """An entry post-cursor about person:marcelo boosts above base."""
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
            cursors={"person:marcelo": "2026-05-01"},  # entry is post-cursor
        )
        assert ranked[0].record["id"] == "fresh"
        assert any("post_cursor:" in s for s in ranked[0].signals)

    def test_memory_entry_pre_cursor_demoted(self) -> None:
        """Pre-cursor entries (already consolidated) demoted."""
        candidates = [
            {
                "id": "old",
                "_distance": 0.1,  # very close, but pre-cursor
                "class_name": "episodic",
                "entities": ["person:marcelo"],
                "valid_from": "2026-04-01",
            },
            {
                "id": "neutral",
                "_distance": 0.2,
                "class_name": "episodic",
                "entities": [],
                "valid_from": "2026-04-01",
            },
        ]
        ranked = rank_with_entities(
            candidates,
            query_entities=["person:marcelo"],
            cursors={"person:marcelo": "2026-05-01"},  # entry is pre-cursor
        )
        # 'old' has better base score (closer) but demoted: should fall behind
        ids = [r.record["id"] for r in ranked]
        assert ids.index("neutral") < ids.index("old")
        old = next(r for r in ranked if r.record["id"] == "old")
        assert any("pre_cursor:" in s for s in old.signals)

    def test_combined_realistic_mix(self) -> None:
        """Page + post-cursor entry + pre-cursor entry + neutral entry.

        Per doc 18 §3.4 (read-time reconciliation), the page and fresh
        entries COEXIST in results — the LLM reconciles. So we don't
        require the page to always be #1; we require the canonical page
        and the fresh entry to be in the top 2, and pre-cursor entries
        to drop to the bottom.
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
            cursors={"person:marcelo": "2026-05-01"},
        )
        ids = [r.record["id"] for r in ranked]
        # The two entity-relevant fresh candidates (page + post-cursor entry)
        # should occupy positions 0 and 1 (order between them not asserted —
        # they coexist intentionally).
        assert set(ids[:2]) == {"person:marcelo", "fresh"}
        # Pre-cursor entry is demoted to the bottom.
        assert ids[-1] == "old"

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

    def test_no_cursor_defaults_to_boost(self) -> None:
        """When cursor is missing, treat as post-cursor (don't penalize)."""
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
            cursors={},  # no cursor for marcelo
        )
        # Should still boost as post-cursor
        assert any("post_cursor:" in s for s in ranked[0].signals)
