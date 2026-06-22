"""Privacy: free-text fields truncated to 200 chars at emit time.

The indexer / search pipeline / agent must NOT persist full user queries or
content snippets to telemetry. The emit helper truncates the listed fields at
200 chars; nested structures pass through.
"""

from __future__ import annotations

from durin.agent.tools._telemetry import _truncate_freetext


def test_short_query_unchanged() -> None:
    data = {"query": "hello"}
    assert _truncate_freetext(data) == {"query": "hello"}


def test_long_query_truncated_to_200() -> None:
    long_query = "a" * 500
    out = _truncate_freetext({"query": long_query})
    assert out["query"].startswith("a" * 200)
    assert len(out["query"]) == 201  # 200 + ellipsis


def test_ellipsis_appended_when_truncated() -> None:
    out = _truncate_freetext({"query": "a" * 500})
    assert out["query"].endswith("…")


def test_at_boundary_not_truncated() -> None:
    """Exactly 200 chars must not be truncated (off-by-one guard)."""
    out = _truncate_freetext({"query": "a" * 200})
    assert out["query"] == "a" * 200


def test_other_freetext_fields_also_truncated() -> None:
    """The same rule applies to `text`, `snippet`, `content`, `needle`."""
    long_value = "b" * 300
    for field in ("text", "snippet", "content", "needle"):
        out = _truncate_freetext({field: long_value})
        assert len(out[field]) == 201


def test_non_freetext_fields_pass_through() -> None:
    """`uri`, `path`, `count`, `ts`, … should never be truncated."""
    huge_path = "a/" * 200 + "x.md"
    out = _truncate_freetext({
        "uri": "person:" + "a" * 500,
        "path": huge_path,
        "count": 9999,
    })
    assert out["uri"] == "person:" + "a" * 500
    assert out["path"] == huge_path
    assert out["count"] == 9999


def test_input_dict_not_mutated() -> None:
    """Truncation must produce a copy — caller's dict stays intact."""
    long_query = "a" * 500
    original = {"query": long_query}
    _truncate_freetext(original)
    assert original["query"] == long_query


def test_non_string_freetext_passes_through() -> None:
    """A `query` that's somehow non-string (caller bug) must not crash."""
    out = _truncate_freetext({"query": None})
    assert out == {"query": None}


def test_nested_dict_passes_through() -> None:
    """Only top-level keys are inspected — nested structures are
    structured metadata, not free text."""
    nested = {
        "uri": "x",
        "extra": {"query": "a" * 500},
    }
    out = _truncate_freetext(nested)
    assert out["extra"]["query"] == "a" * 500  # untouched
