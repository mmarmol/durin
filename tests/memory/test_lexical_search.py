"""Integration tests for the lexical search executor."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.fts_index import FTSIndex
from durin.memory.lexical_search import lexical_search
from durin.memory.query_router import decide_lexical_route


def _seed(idx: FTSIndex) -> None:
    idx.upsert(
        uri="person:marcelo", path="memory/entities/person/marcelo.md",
        type_="entity", entity_type="person",
        text="Marcelo Marmol lives in Spain", mtime=1.0,
    )
    idx.upsert(
        uri="person:masailuo", path="memory/entities/person/masailuo.md",
        type_="entity", entity_type="person",
        text="马塞洛 是 工程师", mtime=2.0,
    )
    idx.upsert(
        uri="topic:autocompaction", path="memory/topic/auto.md",
        type_="topic", entity_type=None,
        text="autocompaction loop guard", mtime=3.0,
    )


def test_unicode61_path_returns_matches(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        _seed(idx)
        decision = decide_lexical_route("Marcelo")
        hits = lexical_search(idx, decision)
    assert any(h.uri == "person:marcelo" for h in hits)


def test_trigram_path_handles_cjk(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        _seed(idx)
        decision = decide_lexical_route("马塞洛 工程师")
        hits = lexical_search(idx, decision)
    assert any(h.uri == "person:masailuo" for h in hits)


def test_like_substring_path_handles_short_cjk(tmp_path: Path) -> None:
    """A single-char CJK query that trigram can't index — falls back
    to LIKE."""
    with FTSIndex.open(tmp_path) as idx:
        _seed(idx)
        decision = decide_lexical_route("马")
        hits = lexical_search(idx, decision)
    # The 1-char CJK query falls into the LIKE substring path; the
    # 马 character is in the indexed text so the hit surfaces.
    assert any(h.uri == "person:masailuo" for h in hits)


def test_empty_query_returns_empty(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        _seed(idx)
        decision = decide_lexical_route("")
        hits = lexical_search(idx, decision)
    assert hits == []


def test_quoting_handles_special_chars(tmp_path: Path) -> None:
    """A query with a `:` (FTS5 separator) must not crash the parser."""
    with FTSIndex.open(tmp_path) as idx:
        _seed(idx)
        decision = decide_lexical_route("person:marcelo")
        hits = lexical_search(idx, decision)
    # The token is `"person:marcelo"` after quoting; FTS5 finds it as
    # a phrase. The seed text doesn't contain that literal, so no hit;
    # the key check is "doesn't raise".
    assert isinstance(hits, list)
