"""Tests for memory_drill (durin.memory.drill + MemoryDrillTool)."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.drill import DrillError, drill, extract_section


# ---------------------------------------------------------------------------
# extract_section (pure)
# ---------------------------------------------------------------------------


def test_extract_basic_section() -> None:
    text = (
        "# Title\n"
        "\n"
        "## turn-1\n"
        "first turn content\n"
        "\n"
        "## turn-2\n"
        "second turn content\n"
    )
    out = extract_section(text, "turn-1")
    assert "## turn-1" in out
    assert "first turn content" in out
    assert "## turn-2" not in out


def test_extract_last_section_runs_to_eof() -> None:
    text = "# Title\n\n## turn-1\nA\n\n## turn-2\nB\nC\n"
    out = extract_section(text, "turn-2")
    assert "## turn-2" in out
    assert "B" in out
    assert "C" in out


def test_extract_stops_at_same_level_header() -> None:
    text = "## a\nA\n## b\nB\n## c\nC\n"
    out = extract_section(text, "a")
    assert out.strip() == "## a\nA"


def test_extract_stops_at_higher_level_header() -> None:
    text = "## a\nA\n### a-sub\nsub\n# top\nT\n## b\nB\n"
    out = extract_section(text, "a")
    assert "## a" in out
    assert "### a-sub" in out
    assert "# top" not in out
    assert "## b" not in out


def test_extract_unknown_anchor_raises() -> None:
    text = "## a\nA\n"
    with pytest.raises(DrillError, match="anchor not found"):
        extract_section(text, "missing")


# ---------------------------------------------------------------------------
# drill (uri resolver)
# ---------------------------------------------------------------------------


def test_drill_resolves_turn_anchor_in_session(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "abc.md").write_text(
        "# Session abc\n\n## turn-1\nhello\n\n## turn-2\nworld\n",
        encoding="utf-8",
    )
    out = drill(tmp_path, "sessions/abc.md#turn-1")
    assert "hello" in out
    assert "world" not in out


def test_drill_resolves_section_in_ingested(tmp_path: Path) -> None:
    src_dir = tmp_path / "ingested" / "doc-1"
    src_dir.mkdir(parents=True)
    (src_dir / "source.md").write_text(
        "# Doc\n\n## intro\nintro body\n\n## api\napi body\n",
        encoding="utf-8",
    )
    out = drill(tmp_path, "ingested/doc-1/source.md#api")
    assert "api body" in out
    assert "intro" not in out


def test_drill_memory_entry_without_extension_or_anchor(tmp_path: Path) -> None:
    """memory/<class>/<id> with no extension resolves to <id>.md."""
    mem_dir = tmp_path / "memory" / "stable"
    mem_dir.mkdir(parents=True)
    (mem_dir / "mem-001.md").write_text(
        "---\nid: mem-001\nheadline: h\n---\n\nbody text\n",
        encoding="utf-8",
    )
    out = drill(tmp_path, "memory/stable/mem-001")
    assert "body text" in out
    assert "---" in out  # frontmatter included


def test_drill_no_anchor_returns_full_file(tmp_path: Path) -> None:
    p = tmp_path / "doc.md"
    p.write_text("just some content\n", encoding="utf-8")
    out = drill(tmp_path, "doc.md")
    assert "just some content" in out


def test_drill_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(DrillError, match="file not found"):
        drill(tmp_path, "nope.md")


def test_drill_missing_anchor_raises(tmp_path: Path) -> None:
    p = tmp_path / "doc.md"
    p.write_text("## a\nA\n", encoding="utf-8")
    with pytest.raises(DrillError, match="anchor not found"):
        drill(tmp_path, "doc.md#missing")


def test_drill_empty_uri_raises(tmp_path: Path) -> None:
    with pytest.raises(DrillError, match="uri is required"):
        drill(tmp_path, "")


def test_drill_consolidated_anchor(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "abc.md").write_text(
        "# s\n\n## consolidated-1\nrollup\n\n## turn-3\nstill live\n",
        encoding="utf-8",
    )
    out = drill(tmp_path, "sessions/abc.md#consolidated-1")
    assert "rollup" in out
    assert "still live" not in out


# ---------------------------------------------------------------------------
# MemoryDrillTool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_returns_content(tmp_path: Path) -> None:
    from durin.agent.tools.memory_drill import MemoryDrillTool

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "abc.md").write_text("## turn-1\nhi\n", encoding="utf-8")

    tool = MemoryDrillTool(workspace=tmp_path)
    out = await tool.execute(uri="sessions/abc.md#turn-1")
    assert out["uri"] == "sessions/abc.md#turn-1"
    assert "hi" in out["content"]


@pytest.mark.asyncio
async def test_tool_error_on_missing(tmp_path: Path) -> None:
    from durin.agent.tools.memory_drill import MemoryDrillTool

    tool = MemoryDrillTool(workspace=tmp_path)
    out = await tool.execute(uri="nope.md#turn-1")
    assert "error" in out
