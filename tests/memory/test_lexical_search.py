"""Integration tests for the lexical search executor."""

from __future__ import annotations

from pathlib import Path

from durin.memory.fts_index import FTSIndex
from durin.memory.lexical_search import build_fts_expression, lexical_search
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


# ---------------------------------------------------------------------------
# Type set predicates: include_types / exclude_types
# ---------------------------------------------------------------------------


def test_exclude_types_keeps_the_limit_for_the_wanted_rows(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        for i in range(60):
            idx.upsert(
                uri=f"reference:doc#{i}", path=f"r{i}.md",
                type_="reference", entity_type="",
                text="bruenor axe", mtime=float(i),
            )
        idx.upsert(
            uri="memory/episodic/note1", path="n.md",
            type_="episodic", entity_type="",
            text="bruenor axe", mtime=100.0,
        )
        hits = idx.search('"bruenor"', limit=50, exclude_types=("reference",))
        assert [h.uri for h in hits] == ["memory/episodic/note1"]


def test_include_types_is_a_set(tmp_path: Path) -> None:
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:bruenor", path="p.md",
            type_="entity", entity_type="person",
            text="bruenor", mtime=1.0,
        )
        idx.upsert(
            uri="skill/axe", path="s.md",
            type_="skill", entity_type="",
            text="bruenor", mtime=2.0,
        )
        idx.upsert(
            uri="reference:doc#1", path="r.md",
            type_="reference", entity_type="",
            text="bruenor", mtime=3.0,
        )
        hits = idx.search('"bruenor"', include_types=("entity", "skill"))
        assert {h.uri for h in hits} == {"person:bruenor", "skill/axe"}


def test_like_fallback_honours_the_type_set(tmp_path: Path) -> None:
    # "东京" is 2 CJK chars — decide_lexical_route routes it to
    # LIKE_SUBSTRING (trigram needs >= 3 chars), which is the route
    # this test exercises.
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="reference:doc#1", path="r.md",
            type_="reference", entity_type="",
            text="东京 旅行", mtime=1.0,
        )
        idx.upsert(
            uri="memory/episodic/n", path="n.md",
            type_="episodic", entity_type="",
            text="东京 旅行", mtime=2.0,
        )
        hits = lexical_search(
            idx, decide_lexical_route("东京"), emit=False,
            exclude_types=("reference",),
        )
        assert [h.uri for h in hits] == ["memory/episodic/n"]


def test_a_bare_string_type_set_means_that_one_type(tmp_path: Path) -> None:
    """A bare string for include_types or exclude_types is treated as a
    one-element set, not iterated per character."""
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(
            uri="person:bruenor", path="p.md",
            type_="entity", entity_type="person",
            text="bruenor", mtime=1.0,
        )
        idx.upsert(
            uri="reference:doc#1", path="r.md",
            type_="reference", entity_type="",
            text="bruenor", mtime=2.0,
        )
        # Test include_types with bare string
        hits = idx.search('"bruenor"', include_types="entity")
        assert [h.uri for h in hits] == ["person:bruenor"]
        # Test exclude_types with bare string
        hits = idx.search('"bruenor"', exclude_types="reference")
        assert [h.uri for h in hits] == ["person:bruenor"]


# ---------------------------------------------------------------------------
# build_fts_expression: the one FTS5 expression builder
# ---------------------------------------------------------------------------


def test_loose_tokens_are_or_joined():
    e = build_fts_expression("arma preferida Bruenor")
    assert e.text == '("arma" OR "preferida" OR "Bruenor")'
    assert (e.required, e.optional) == (0, 3)


def test_quoted_phrases_are_required():
    e = build_fts_expression('"Mithral Hall" hacha regalo')
    assert e.text == '"Mithral Hall" AND ("hacha" OR "regalo")'
    assert (e.required, e.optional) == (1, 2)


def test_keywords_tokens_are_required_and_quoted_groups_are_phrases():
    e = build_fts_expression("arma regalo", keywords='Bruenor "doble filo"')
    assert e.text == '"Bruenor" AND "doble filo" AND ("arma" OR "regalo")'
    assert (e.required, e.optional) == (2, 2)


def test_only_required_terms_is_a_plain_and():
    e = build_fts_expression("", keywords="Bruenor hacha")
    assert e.text == '"Bruenor" AND "hacha"'


def test_unbalanced_quotes_degrade_to_tokens():
    e = build_fts_expression('Bruenor "incomplete')
    assert e.text == '("Bruenor")'


def test_operators_and_punctuation_stay_literal():
    e = build_fts_expression("NOT AND alpha*beta")
    assert e.text == '("NOT" OR "AND" OR "alpha*beta")'


def test_empty_query_and_keywords_yield_no_expression():
    assert build_fts_expression("   ").text == ""


# ---------------------------------------------------------------------------
# Task 9: lexical_search uses the expression on every route
# ---------------------------------------------------------------------------


def test_a_sentence_finds_the_note_that_shares_its_rare_words(tmp_path):
    # The distractor shares connector words with the query ("la"/"y"/"se") —
    # that's deliberate: under the OR-ranked-by-bm25 contract a document
    # matching only on connector words is still a real match, not excluded,
    # and it must rank below the note that shares the query's rare words.
    # A two-document index makes bm25's idf misjudge those connectors as
    # rare (each appears in only one of two rows), which can invert the
    # ranking; the filler documents below give idf a realistic background
    # so the connector words score as the common tokens they are.
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(uri="memory/episodic/axe", path="a.md", type_="episodic", entity_type="",
                   text="Bruenor prefiere el hacha de doble filo forjada en Mithral Hall, regalo de Thalgrim.",
                   mtime=1.0)
        idx.upsert(uri="memory/episodic/other", path="b.md", type_="episodic", entity_type="",
                   text="La panadería abre a las ocho y el pan de masa madre se agota pronto.",
                   mtime=2.0)
        idx.upsert(uri="memory/episodic/filler1", path="f1.md", type_="episodic", entity_type="",
                   text="El autobús llega a las nueve y la parada está a dos cuadras de aquí.",
                   mtime=3.0)
        idx.upsert(uri="memory/episodic/filler2", path="f2.md", type_="episodic", entity_type="",
                   text="La lluvia empezó temprano y el partido se suspendió por la tarde.",
                   mtime=4.0)
        idx.upsert(uri="memory/episodic/filler3", path="f3.md", type_="episodic", entity_type="",
                   text="El jardín necesita agua y las plantas se marchitan si hace calor.",
                   mtime=5.0)
        idx.upsert(uri="memory/episodic/filler4", path="f4.md", type_="episodic", entity_type="",
                   text="La reunión se movió al lunes y el informe se entrega la próxima semana.",
                   mtime=6.0)
        idx.upsert(uri="memory/episodic/filler5", path="f5.md", type_="episodic", entity_type="",
                   text="El tren sale a las siete y la estación queda cerca del centro.",
                   mtime=7.0)
        idx.upsert(uri="memory/episodic/filler6", path="f6.md", type_="episodic", entity_type="",
                   text="La tienda cierra a las diez y el dueño vive arriba del local.",
                   mtime=8.0)
        decision = decide_lexical_route("¿Qué arma prefiere Bruenor y quién se la regaló?")
        hits = lexical_search(idx, decision, emit=False)
        uris = [h.uri for h in hits]
        assert uris[0] == "memory/episodic/axe"
        assert "memory/episodic/other" in uris[1:]


def test_a_required_phrase_excludes_the_near_miss(tmp_path):
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(uri="a", path="a.md", type_="episodic", entity_type="", text="doble filo forjada", mtime=1.0)
        idx.upsert(uri="b", path="b.md", type_="episodic", entity_type="", text="filo doble forjada", mtime=2.0)
        hits = lexical_search(idx, decide_lexical_route('"doble filo" forjada'), emit=False)
        assert [h.uri for h in hits] == ["a"]


def test_keywords_are_required_terms(tmp_path):
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(uri="a", path="a.md", type_="episodic", entity_type="", text="Bruenor hacha", mtime=1.0)
        idx.upsert(uri="b", path="b.md", type_="episodic", entity_type="", text="Thalgrim hacha", mtime=2.0)
        hits = lexical_search(idx, decide_lexical_route("hacha", keywords="Bruenor"), emit=False)
        assert [h.uri for h in hits] == ["a"]


def test_the_like_fallback_ors_its_tokens(tmp_path):
    with FTSIndex.open(tmp_path) as idx:
        idx.upsert(uri="a", path="a.md", type_="episodic", entity_type="", text="东京", mtime=1.0)
        idx.upsert(uri="b", path="b.md", type_="episodic", entity_type="", text="大阪", mtime=2.0)
        hits = lexical_search(idx, decide_lexical_route("东京 大阪"), emit=False)
        assert {h.uri for h in hits} == {"a", "b"}
