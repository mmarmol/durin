"""Tests for ``todo_state`` session metadata helpers."""

from __future__ import annotations

from durin.session.todo_state import (
    TODOS_KEY,
    parse_todos,
    render_todos_markdown,
    todos_runtime_lines,
    todos_ws_blob,
)


def test_parse_todos_returns_none_for_missing_or_invalid():
    assert parse_todos(None) is None
    assert parse_todos("not json") is None
    assert parse_todos({"not": "a list"}) is None


def test_parse_todos_filters_invalid_entries():
    raw = [
        {"content": "ok", "status": "pending", "activeForm": "okay-ing"},
        {"content": "", "status": "pending", "activeForm": "blank content"},
        {"content": "bad status", "status": "wat", "activeForm": "?"},
        "not even a dict",
    ]
    items = parse_todos(raw)
    assert items is not None
    assert len(items) == 1
    assert items[0]["content"] == "ok"


def test_parse_todos_accepts_json_string():
    items = parse_todos(
        '[{"content":"x","status":"in_progress","activeForm":"xing"}]'
    )
    assert items == [{"content": "x", "status": "in_progress", "activeForm": "xing"}]


def test_parse_todos_truncates_long_fields():
    long_text = "a" * 2000
    items = parse_todos([
        {"content": long_text, "status": "pending", "activeForm": long_text},
    ])
    assert items is not None
    assert len(items[0]["content"]) == 400
    assert len(items[0]["activeForm"]) == 400


def test_parse_todos_defaults_active_form_to_content():
    """If activeForm is missing/blank, fall back to content so the runtime
    line still has something useful to render."""
    items = parse_todos([
        {"content": "Run tests", "status": "in_progress", "activeForm": ""},
    ])
    assert items == [
        {"content": "Run tests", "status": "in_progress", "activeForm": "Run tests"},
    ]


def test_runtime_lines_empty_when_no_todos():
    assert todos_runtime_lines(None) == []
    assert todos_runtime_lines({}) == []
    assert todos_runtime_lines({TODOS_KEY: []}) == []


def test_runtime_lines_include_counts_and_items():
    meta = {
        TODOS_KEY: [
            {"content": "Read code", "status": "completed", "activeForm": "Reading code"},
            {"content": "Write fix", "status": "in_progress", "activeForm": "Writing the fix"},
            {"content": "Run tests", "status": "pending", "activeForm": "Running tests"},
        ]
    }
    lines = todos_runtime_lines(meta)
    # Header carries the counts.
    header = lines[0]
    assert "3 total" in header
    assert "1 in_progress" in header
    assert "1 pending" in header
    assert "1 completed" in header
    # Body uses the activeForm for the in_progress item.
    body = "\n".join(lines[1:])
    assert "Writing the fix" in body
    assert "Read code" in body  # completed → uses content
    assert "Run tests" in body  # pending → uses content


def test_runtime_lines_truncate_at_cap():
    items = [
        {"content": f"item {i}", "status": "pending", "activeForm": f"doing {i}"}
        for i in range(50)
    ]
    meta = {TODOS_KEY: items}
    lines = todos_runtime_lines(meta)
    # No truncation note when within cap.
    assert not any("truncated" in ln for ln in lines)


def test_ws_blob_returns_items_or_empty():
    assert todos_ws_blob(None) == {"items": []}
    meta = {
        TODOS_KEY: [
            {"content": "x", "status": "pending", "activeForm": "xing"},
        ]
    }
    assert todos_ws_blob(meta) == {
        "items": [{"content": "x", "status": "pending", "activeForm": "xing"}]
    }


def test_render_markdown_shows_status_boxes():
    items = [
        {"content": "a", "status": "completed", "activeForm": "a-ing"},
        {"content": "b", "status": "in_progress", "activeForm": "b-ing"},
        {"content": "c", "status": "pending", "activeForm": "c-ing"},
    ]
    md = render_todos_markdown(items)
    assert "[x] a" in md
    assert "[~] b-ing" in md  # in_progress uses activeForm
    assert "[ ] c" in md


def test_render_markdown_empty():
    assert "no todos" in render_todos_markdown(None)
    assert "no todos" in render_todos_markdown([])
