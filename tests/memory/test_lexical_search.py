"""Integration tests for the lexical search executor."""

from __future__ import annotations

from pathlib import Path

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


# ---------------------------------------------------------------------------
# Audit H10 (2026-05-29): phrase matching via double-quoted substrings
# ---------------------------------------------------------------------------
#
# Pre-H10 every token in the query was quoted independently for FTS5,
# so a query `Marcelo Marmol` resolved to `"Marcelo" "Marmol"` — the
# AND of two phrase-tokens, which matches a document containing both
# words anywhere. Useful for token search but loses ordering: it also
# matches "Marmol Marcelo lives in Spain".
#
# H10 lets the agent express a phrase intent with double quotes:
# `"Marcelo Marmol" lives` is parsed as one FTS5 phrase + one token.
# Documents must contain "Marcelo Marmol" adjacent, and the token
# "lives" anywhere.


def test_quoted_phrase_matches_exact_sequence(tmp_path: Path) -> None:
    """A double-quoted phrase resolves to an FTS5 NEAR-style phrase
    that requires the words in order."""
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="ok:in_order", path="memory/x/a.md",
            type_="topic", entity_type=None,
            text="Marcelo Marmol lives in Spain", mtime=1.0,
        )
        idx.upsert(
            uri="bad:reversed", path="memory/x/b.md",
            type_="topic", entity_type=None,
            text="Marmol Marcelo says hi", mtime=2.0,
        )
        # Quoted phrase: must appear in order.
        decision = decide_lexical_route('"Marcelo Marmol"')
        hits = lexical_search(idx, decision)
    uris = {h.uri for h in hits}
    assert "ok:in_order" in uris
    assert "bad:reversed" not in uris, (
        "phrase match must reject the reversed-token document"
    )


def test_quoted_phrase_plus_loose_token(tmp_path: Path) -> None:
    """Quoted phrase AND loose token: phrase contiguous, loose
    token anywhere in the same doc."""
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="hit:phrase_and_token", path="memory/x/a.md",
            type_="topic", entity_type=None,
            text="Marcelo Marmol architects durin systems",
            mtime=1.0,
        )
        idx.upsert(
            uri="miss:no_token", path="memory/x/b.md",
            type_="topic", entity_type=None,
            text="Marcelo Marmol lives in Spain", mtime=2.0,
        )
        decision = decide_lexical_route('"Marcelo Marmol" durin')
        hits = lexical_search(idx, decision)
    uris = {h.uri for h in hits}
    assert "hit:phrase_and_token" in uris
    assert "miss:no_token" not in uris


def test_unmatched_quote_falls_back_to_token_search(tmp_path: Path) -> None:
    """Robustness: an unbalanced quote must not crash FTS — it
    degrades gracefully to per-token search."""
    with FTSIndex.open(tmp_path) as idx:
        _seed(idx)
        decision = decide_lexical_route('Marcelo "incomplete')
        hits = lexical_search(idx, decision)
    # Should still find Marcelo's entry — degradation doesn't lose hits.
    assert any(h.uri == "person:marcelo" for h in hits)


# ---------------------------------------------------------------------------
# Regression: FTS5 boolean keywords in a natural-language query
# ---------------------------------------------------------------------------
#
# `_quote_for_fts` used to pass AND/OR/NOT/NEAR through as FTS5
# operators (case-insensitively). Since the recall query is natural
# language — never a boolean expression — that hijacked the commonest
# English function words. A query beginning with "not" left a bare
# leading NOT operator with no left operand, raising
# `fts5: syntax error near "NOT"`, which the pipeline swallowed as a
# silent lexical-tier failure. Every token must now be quoted as
# literal content.


def test_leading_boolean_keyword_does_not_crash(tmp_path: Path) -> None:
    """A query starting with the word "not" must match literally, not
    parse as a dangling FTS5 NOT operator."""
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="topic:not_sure", path="memory/x/a.md",
            type_="topic", entity_type=None,
            text="not sure what to deploy next", mtime=1.0,
        )
        decision = decide_lexical_route("not sure what to deploy")
        hits = lexical_search(idx, decision)
    # Pre-fix this raised fts5 syntax error near "NOT"; now "not" is a
    # literal token and the doc surfaces.
    assert any(h.uri == "topic:not_sure" for h in hits)


def test_boolean_keywords_are_quoted_as_literals() -> None:
    """The lowercase/uppercase boolean keywords are quoted, never
    emitted as bare FTS5 operators."""
    from durin.memory.lexical_search import _quote_for_fts

    assert _quote_for_fts("not sure") == '"not" "sure"'
    assert _quote_for_fts("and then") == '"and" "then"'
    assert _quote_for_fts("do NOT delete") == '"do" "NOT" "delete"'
    assert _quote_for_fts("near the edge") == '"near" "the" "edge"'
