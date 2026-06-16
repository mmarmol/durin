"""Tests for session jsonl → markdown formatter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.memory.session_md import (
    SessionMdError,
    regenerate_session_md,
    render_session_md,
)


def _write_session(
    path: Path,
    metadata: dict,
    messages: list[dict],
) -> None:
    """Write a jsonl session file: metadata line 0, then one message per line."""
    lines = [json.dumps({"_type": "metadata", **metadata})]
    for msg in messages:
        lines.append(json.dumps(msg))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_renders_empty_session(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write_session(p, {"key": "abc"}, [])
    md = render_session_md(p)
    assert "# Session abc" in md
    assert "## turn-1" not in md
    assert "## consolidated" not in md


def test_renders_single_user_turn(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write_session(
        p,
        {"key": "abc"},
        [{"role": "user", "content": "hola", "timestamp": "2026-05-20T10:00:00"}],
    )
    md = render_session_md(p)
    assert "## turn-1" in md
    assert "**user**" in md
    assert "`2026-05-20T10:00:00`" in md
    assert "hola" in md


def test_renders_multiple_turns_in_order(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write_session(
        p,
        {"key": "abc"},
        [
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "que tal"},
            {"role": "user", "content": "bien"},
        ],
    )
    md = render_session_md(p)
    pos1 = md.index("## turn-1")
    pos2 = md.index("## turn-2")
    pos3 = md.index("## turn-3")
    assert pos1 < pos2 < pos3


def test_anchor_stability_under_consolidation(tmp_path: Path) -> None:
    """Each turn keeps its turn-N anchor regardless of consolidation."""
    p = tmp_path / "s.jsonl"
    _write_session(
        p,
        {"key": "abc", "last_consolidated": 2},
        [
            {"role": "user", "content": "turn1"},
            {"role": "assistant", "content": "turn2"},
            {"role": "user", "content": "turn3"},
        ],
    )
    md = render_session_md(p)
    # All per-turn anchors still present
    assert "## turn-1" in md
    assert "## turn-2" in md
    assert "## turn-3" in md
    # Consolidated super-anchor present
    assert "## consolidated-1" in md
    # turn-1 and turn-2 carry the (consolidated) marker; turn-3 does not
    t1 = md.index("## turn-1")
    t2 = md.index("## turn-2")
    t3 = md.index("## turn-3")
    assert "(consolidated)" in md[t1:t2]
    assert "(consolidated)" in md[t2:t3]
    assert "(consolidated)" not in md[t3:]


def test_deterministic_output(tmp_path: Path) -> None:
    """Same input → byte-identical markdown across calls."""
    p = tmp_path / "s.jsonl"
    _write_session(
        p,
        {"key": "abc"},
        [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "y",
                "tool_calls": [{"name": "foo", "args": {"a": 1}}],
            },
        ],
    )
    assert render_session_md(p) == render_session_md(p)


def test_renders_tool_calls_as_json_block(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    _write_session(
        p,
        {"key": "abc"},
        [
            {
                "role": "assistant",
                "content": "calling tool",
                "tool_calls": [{"name": "search", "args": {"q": "test"}}],
            }
        ],
    )
    md = render_session_md(p)
    assert "```json" in md
    assert '"name": "search"' in md


def test_renders_list_content(tmp_path: Path) -> None:
    """Some providers return content as a list of text blocks."""
    p = tmp_path / "s.jsonl"
    _write_session(
        p,
        {"key": "abc"},
        [{"role": "assistant", "content": [{"type": "text", "text": "hello world"}]}],
    )
    md = render_session_md(p)
    assert "hello world" in md


def test_malformed_metadata_raises(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    with pytest.raises(SessionMdError, match="not valid JSON"):
        render_session_md(p)


def test_missing_metadata_type_raises(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"foo": "bar"}) + "\n", encoding="utf-8")
    with pytest.raises(SessionMdError, match="metadata"):
        render_session_md(p)


def test_skips_malformed_message_line(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"_type": "metadata", "key": "abc"}) + "\n"
        + "{ not json\n"
        + json.dumps({"role": "user", "content": "ok"}) + "\n",
        encoding="utf-8",
    )
    md = render_session_md(p)
    assert "## turn-1" in md  # malformed line still gets its anchor
    assert "## turn-2" in md
    assert "(malformed)" in md
    assert "ok" in md


def test_regenerate_writes_sibling_md(tmp_path: Path) -> None:
    p = tmp_path / "abc.jsonl"
    _write_session(p, {"key": "abc"}, [{"role": "user", "content": "hi"}])
    md_path = regenerate_session_md(p)
    assert md_path == tmp_path / "abc.md"
    assert md_path.is_file()
    assert "## turn-1" in md_path.read_text(encoding="utf-8")


def test_regenerate_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "abc.jsonl"
    _write_session(p, {"key": "abc"}, [{"role": "user", "content": "hi"}])
    regenerate_session_md(p)
    first = (tmp_path / "abc.md").read_text(encoding="utf-8")
    regenerate_session_md(p)
    second = (tmp_path / "abc.md").read_text(encoding="utf-8")
    assert first == second


def test_regenerate_explicit_output_path(tmp_path: Path) -> None:
    p = tmp_path / "abc.jsonl"
    _write_session(p, {"key": "abc"}, [{"role": "user", "content": "hi"}])
    custom = tmp_path / "custom.md"
    md_path = regenerate_session_md(p, custom)
    assert md_path == custom
    assert custom.is_file()


def test_session_save_writes_md_sibling(tmp_path: Path) -> None:
    """End-to-end: SessionManager.save() must regenerate the .md view."""
    from durin.session.manager import SessionManager

    mgr = SessionManager(workspace=tmp_path)
    session = mgr.get_or_create("integration-test")
    session.messages.append({"role": "user", "content": "hola", "timestamp": "now"})
    mgr.save(session)

    md_path = mgr.sessions_dir / "integration-test.md"
    assert md_path.is_file(), f"expected {md_path} to exist"
    content = md_path.read_text(encoding="utf-8")
    assert "## turn-1" in content
    assert "hola" in content
