"""Tests for read_file multi-path fan-out (paths[])."""

from __future__ import annotations

import pytest

from durin.agent.tools.filesystem import MAX_READ_PATHS, ReadFileTool


@pytest.mark.asyncio
async def test_paths_fan_out_returns_one_record_per_path_in_order(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)

    out = await tool.execute(paths=["a.txt", "b.txt"])
    recs = out["results"]
    assert [r["path"] for r in recs] == ["a.txt", "b.txt"]
    assert "alpha" in recs[0]["content"]
    assert "beta" in recs[1]["content"]


@pytest.mark.asyncio
async def test_one_missing_path_does_not_abort_batch(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)

    out = await tool.execute(paths=["a.txt", "nope.txt"])
    recs = out["results"]
    assert "alpha" in recs[0]["content"]
    # missing file surfaces as a per-item content string, batch still returns
    assert recs[1]["path"] == "nope.txt"
    assert "content" in recs[1] or "error" in recs[1]


@pytest.mark.asyncio
async def test_path_and_paths_mutually_exclusive(tmp_path):
    tool = ReadFileTool(workspace=tmp_path)
    out = await tool.execute(path="a.txt", paths=["b.txt"])
    assert out == "Error: pass either `path` (single) or `paths` (list), not both"


@pytest.mark.asyncio
async def test_paths_cap_enforced(tmp_path):
    tool = ReadFileTool(workspace=tmp_path)
    out = await tool.execute(paths=["x.txt"] * (MAX_READ_PATHS + 1))
    assert "too many paths" in out


@pytest.mark.asyncio
async def test_single_path_still_works(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)
    out = await tool.execute(path="a.txt")
    assert "alpha" in out and "beta" in out
