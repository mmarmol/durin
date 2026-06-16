"""Tests for memory ingestion (durin.memory.ingestion + MemoryIngestTool)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.memory.ingestion import IngestError, ingest_artifact

# ---------------------------------------------------------------------------
# ingest_artifact (pure function)
# ---------------------------------------------------------------------------


def _make_source(tmp_path: Path, name: str, content: str) -> Path:
    src = tmp_path / "incoming" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(content, encoding="utf-8")
    return src


def test_ingest_copies_markdown_and_returns_content(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    src = _make_source(tmp_path, "doc.md", "# Hello\nbody")

    result = ingest_artifact(workspace, src)

    assert result["content"] == "# Hello\nbody"
    assert result["size_bytes"] == src.stat().st_size

    saved = Path(result["source"])
    assert saved.is_file()
    assert saved.name == "source.md"
    assert saved.parent == workspace / "ingested" / result["id"]


def test_ingest_writes_meta_json_with_placeholder_fields(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    src = _make_source(tmp_path, "doc.md", "body text")

    result = ingest_artifact(workspace, src)

    meta = json.loads(Path(result["meta_path"]).read_text(encoding="utf-8"))
    assert meta["id"] == result["id"]
    derived = meta["derived"]
    assert derived["source_path"] == str(src)
    assert derived["size_bytes"] == src.stat().st_size
    assert "ingested_at" in derived
    # LLM-derived fields start empty
    assert derived["summary"] == ""
    assert derived["entities"] == []
    assert derived["relations"] == []


def test_ingest_idempotent_by_content_hash(tmp_path: Path) -> None:
    """Re-ingesting the same (filename, content) gives the same id and doesn't double-copy."""
    workspace = tmp_path / "ws"
    src = _make_source(tmp_path, "doc.md", "body text")

    first = ingest_artifact(workspace, src)
    second = ingest_artifact(workspace, src)
    assert first["id"] == second["id"]
    assert first["source"] == second["source"]


def test_ingest_distinct_filenames_yield_distinct_ids(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    s1 = _make_source(tmp_path, "a.md", "same body")
    s2 = _make_source(tmp_path / "other", "b.md", "same body")
    r1 = ingest_artifact(workspace, s1)
    r2 = ingest_artifact(workspace, s2)
    assert r1["id"] != r2["id"]


def test_ingest_preserves_extension(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    src = _make_source(tmp_path, "notes.txt", "plain text")
    result = ingest_artifact(workspace, src)
    assert Path(result["source"]).name == "source.txt"


def test_ingest_rejects_missing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    with pytest.raises(IngestError, match="does not exist"):
        ingest_artifact(workspace, tmp_path / "nope.md")


def test_ingest_rejects_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    a_dir = tmp_path / "subdir"
    a_dir.mkdir()
    with pytest.raises(IngestError, match="not a regular file"):
        ingest_artifact(workspace, a_dir)


def test_ingest_rejects_binary(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    src = tmp_path / "blob.bin"
    src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\xff" * 100)
    with pytest.raises(IngestError, match="not utf-8 text"):
        ingest_artifact(workspace, src)


def test_ingest_unicode_content(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    src = _make_source(tmp_path, "doc.md", "日本語 + emoji 🎯 + ñ")
    result = ingest_artifact(workspace, src)
    assert result["content"] == "日本語 + emoji 🎯 + ñ"


# ---------------------------------------------------------------------------
# MemoryIngestTool (agent-facing wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_happy_path(tmp_path: Path) -> None:
    from durin.agent.tools.memory_ingest import MemoryIngestTool

    workspace = tmp_path / "ws"
    src = _make_source(tmp_path, "doc.md", "hello body")

    tool = MemoryIngestTool(workspace=workspace)
    out = await tool.execute(path=str(src))

    assert "error" not in out
    assert out["content"] == "hello body"
    assert out["size_bytes"] == src.stat().st_size
    assert Path(out["saved_to"]).is_file()


@pytest.mark.asyncio
async def test_tool_returns_error_dict_on_missing_path(tmp_path: Path) -> None:
    from durin.agent.tools.memory_ingest import MemoryIngestTool

    tool = MemoryIngestTool(workspace=tmp_path / "ws")
    out = await tool.execute(path="")
    assert out == {"error": "path is required"}


@pytest.mark.asyncio
async def test_tool_returns_error_dict_on_unknown_file(tmp_path: Path) -> None:
    from durin.agent.tools.memory_ingest import MemoryIngestTool

    tool = MemoryIngestTool(workspace=tmp_path / "ws")
    out = await tool.execute(path=str(tmp_path / "missing.md"))
    assert "error" in out
    assert "does not exist" in out["error"]


@pytest.mark.asyncio
async def test_tool_resolves_workspace_relative_path(tmp_path: Path) -> None:
    from durin.agent.tools.memory_ingest import MemoryIngestTool

    workspace = tmp_path / "ws"
    workspace.mkdir()
    rel_src = workspace / "notes.md"
    rel_src.write_text("relative", encoding="utf-8")

    tool = MemoryIngestTool(workspace=workspace)
    out = await tool.execute(path="notes.md")
    assert "error" not in out
    assert out["content"] == "relative"
