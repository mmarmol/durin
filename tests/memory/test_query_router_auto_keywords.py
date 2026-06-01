"""Auto-keyword detection in the query router (P3.3 / doc 03 §3.1).

When the query contains an exact identifier (email, URL, UUID,
file path), the router surfaces it as `auto_keywords` so the
search pipeline can apply the lexical boost without the agent
having to pass `keywords` explicitly.
"""

from __future__ import annotations

import pytest

from durin.memory.query_router import decide_lexical_route


def test_email_detected() -> None:
    decision = decide_lexical_route("find marcelo@mxhero.com")
    assert decision.auto_keywords == "marcelo@mxhero.com"


def test_https_url_detected() -> None:
    decision = decide_lexical_route("see https://example.com/foo")
    assert decision.auto_keywords == "https://example.com/foo"


def test_http_url_detected() -> None:
    decision = decide_lexical_route("link http://internal.io/a/b")
    assert decision.auto_keywords == "http://internal.io/a/b"


def test_uuid_detected() -> None:
    decision = decide_lexical_route(
        "session 7c155274-d96b-43cd-b485-352e5b7ef411 details",
    )
    assert decision.auto_keywords == "7c155274-d96b-43cd-b485-352e5b7ef411"


def test_file_path_detected() -> None:
    decision = decide_lexical_route("read /etc/durin/config.json")
    assert decision.auto_keywords == "/etc/durin/config.json"


def test_relative_path_with_md_detected() -> None:
    decision = decide_lexical_route(
        "where in memory/entities/person/marcelo.md is the alias?",
    )
    assert decision.auto_keywords == "memory/entities/person/marcelo.md"


def test_plain_language_no_match() -> None:
    decision = decide_lexical_route("tell me about Marcelo")
    assert decision.auto_keywords is None


def test_explicit_keywords_takes_precedence() -> None:
    """When the caller passed `keywords`, ignore auto-detection."""
    decision = decide_lexical_route(
        "find marcelo@mxhero.com please",
        keywords="explicit_override",
    )
    # `keywords` field still wins for the boost; auto_keywords surfaces
    # for diagnostics but the search pipeline reads `keywords` first.
    assert decision.keywords == "explicit_override"


def test_version_string_not_treated_as_identifier() -> None:
    """`v1.2.3` is too ambiguous — leave it for semantic search."""
    decision = decide_lexical_route("released in v1.2.3 last week")
    assert decision.auto_keywords is None


def test_multiple_matches_returns_first() -> None:
    """If a query has two identifiers we pick the first; the lexical
    boost only needs one anchor."""
    decision = decide_lexical_route(
        "user marcelo@x.com or alt@y.com",
    )
    assert decision.auto_keywords == "marcelo@x.com"
