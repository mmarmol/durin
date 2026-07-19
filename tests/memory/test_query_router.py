"""Query analysis + FTS5 routing.

The search pipeline routes each query to one of three lexical paths based on
CJK content and token length. The router is pure (no I/O); it returns a typed
decision the lexical-search layer consumes.
"""

from __future__ import annotations

from durin.memory.query_router import (
    MAX_QUERY_CHARS,
    MAX_QUERY_TOKENS,
    LexicalRoute,
    RoutingDecision,
    count_cjk_chars,
    decide_lexical_route,
    normalize_query,
)

# ---------------------------------------------------------------------------
# count_cjk_chars
# ---------------------------------------------------------------------------


def test_count_cjk_pure_chinese() -> None:
    assert count_cjk_chars("马塞洛") == 3


def test_count_cjk_pure_latin() -> None:
    assert count_cjk_chars("hello world") == 0


def test_count_cjk_mixed() -> None:
    """Mixed Chinese + Latin returns just the CJK count."""
    assert count_cjk_chars("马塞洛 marcelo") == 3


def test_count_cjk_hiragana_katakana() -> None:
    assert count_cjk_chars("こんにちは") > 0  # Hiragana
    assert count_cjk_chars("カタカナ") > 0     # Katakana


def test_count_cjk_hangul() -> None:
    assert count_cjk_chars("한국어") > 0


def test_count_cjk_punctuation_ignored() -> None:
    assert count_cjk_chars("。？！,.;:") == 0


def test_count_cjk_empty_string() -> None:
    assert count_cjk_chars("") == 0


def test_count_cjk_non_string_returns_zero() -> None:
    assert count_cjk_chars(None) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# normalize_query (NFC + whitespace collapse)
# ---------------------------------------------------------------------------


def test_normalize_strips_surrounding_whitespace() -> None:
    assert normalize_query("   marcelo   ") == "marcelo"


def test_normalize_collapses_internal_whitespace() -> None:
    assert normalize_query("a    b   c") == "a b c"


def test_normalize_nfc_unifies_decomposed_form() -> None:
    """`é` can be one code point (NFC) or two (NFD). The router
    normalises to NFC so the FTS query matches whichever form was
    indexed."""
    nfd = "Marmól"  # M-a-r-m-o-combining acute-l
    nfc = "Marmól"   # M-a-r-m-ó-l
    assert normalize_query(nfd) == nfc


def test_normalize_handles_empty() -> None:
    assert normalize_query("") == ""


# ---------------------------------------------------------------------------
# decide_lexical_route — the three-branch spec
# ---------------------------------------------------------------------------


def test_route_pure_latin_uses_unicode61() -> None:
    decision = decide_lexical_route("marcelo lives in spain")
    assert isinstance(decision, RoutingDecision)
    assert decision.route == LexicalRoute.UNICODE61
    assert decision.normalized_query == "marcelo lives in spain"


def test_route_cjk_three_or_more_chars_uses_trigram() -> None:
    """CJK count ≥ 3 + every non-operator token has ≥ 3 chars → trigram."""
    decision = decide_lexical_route("马塞洛 工程师")
    assert decision.route == LexicalRoute.TRIGRAM


def test_route_short_cjk_falls_back_to_like_substring() -> None:
    """One or two CJK chars (or CJK tokens < 3 chars) → LIKE
    substring — trigram can't match tokens shorter than 3 chars."""
    decision = decide_lexical_route("马 塞")  # two single-char tokens
    assert decision.route == LexicalRoute.LIKE_SUBSTRING


def test_route_mixed_latin_cjk_with_long_cjk_tokens_uses_trigram() -> None:
    """Mixed but with ≥ 3 CJK chars AND every token ≥ 3 chars."""
    decision = decide_lexical_route("marcelo 马塞洛 engineer")
    assert decision.route == LexicalRoute.TRIGRAM


def test_route_operator_tokens_dont_disqualify() -> None:
    """`AND`/`OR`/`NOT` are FTS5 operators; their length doesn't
    matter for the all-tokens-≥-3 check."""
    decision = decide_lexical_route("马塞洛 OR 工程师")
    assert decision.route == LexicalRoute.TRIGRAM


def test_route_empty_query_returns_unicode61() -> None:
    """Empty query is degenerate but must not crash — return the
    default route and let the caller decide what to do."""
    decision = decide_lexical_route("")
    assert decision.route == LexicalRoute.UNICODE61


def test_route_keywords_param_recorded() -> None:
    """The router records `keywords` for the dynamic-boost step,
    but does NOT alter the routing decision based on it."""
    decision = decide_lexical_route(
        "marcelo", keywords="mmarmol@mxhero.com",
    )
    assert decision.route == LexicalRoute.UNICODE61
    assert decision.keywords == "mmarmol@mxhero.com"


def test_route_keywords_default_none() -> None:
    decision = decide_lexical_route("marcelo")
    assert decision.keywords is None


# ---------------------------------------------------------------------------
# oversized-query bounding
#
# The 2026-07-18 incident: a dream pass fed a whole session transcript
# (380KB, ~50k tokens) as the search query; quoting every token into one
# FTS5 MATCH allocated ~800MB in sqlite per call. The router is the single
# gate to the lexical/grep/vector steps, so it bounds the query itself.
# ---------------------------------------------------------------------------


def test_route_bounds_huge_latin_query() -> None:
    huge = " ".join(f"token{i}" for i in range(50_000))
    decision = decide_lexical_route(huge)
    assert decision.truncated is True
    assert len(decision.normalized_query) <= MAX_QUERY_CHARS
    assert len(decision.normalized_query.split()) <= MAX_QUERY_TOKENS


def test_route_bounds_huge_cjk_query() -> None:
    """CJK has no whitespace tokens — the char cap must bound it."""
    huge = "马塞洛工程师" * 100_000
    decision = decide_lexical_route(huge)
    assert decision.truncated is True
    assert len(decision.normalized_query) <= MAX_QUERY_CHARS


def test_route_normal_query_not_truncated() -> None:
    decision = decide_lexical_route("mxhero onedrive sharing failed 400")
    assert decision.truncated is False
    assert decision.normalized_query == "mxhero onedrive sharing failed 400"


def test_route_truncation_keeps_head_identifiers() -> None:
    """An identifier inside the kept head still gets the auto-keyword boost."""
    huge = "see https://mxhero.zendesk.com/tickets/23098 " + (
        " ".join(f"filler{i}" for i in range(50_000))
    )
    decision = decide_lexical_route(huge)
    assert decision.auto_keywords == "https://mxhero.zendesk.com/tickets/23098"
