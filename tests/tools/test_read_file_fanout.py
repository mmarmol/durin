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
async def test_path_and_paths_together_are_read_as_one_batch(tmp_path):
    """A model that names one file in `path` and more in `paths` gets all of
    them in one call, `path` first and each file once, instead of an error
    that costs it a round trip."""
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.txt").write_text(f"{name}-content\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)

    out = await tool.execute(path="a.txt", paths=["b.txt", "a.txt", "c.txt", "b.txt"])

    recs = out["results"]
    assert [r["path"] for r in recs] == ["a.txt", "b.txt", "c.txt"]
    assert [n in r["content"] for n, r in zip(("a-content", "b-content", "c-content"), recs)] == [
        True, True, True,
    ]


@pytest.mark.asyncio
async def test_an_empty_path_beside_paths_counts_as_not_given(tmp_path):
    """`path=""` next to `paths` reads the `paths` batch, as if `path` were
    absent, not a single read of an empty path."""
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")

    out = await ReadFileTool(workspace=tmp_path).execute(path="", paths=["a.txt", "b.txt"])

    assert out == await ReadFileTool(workspace=tmp_path).execute(paths=["a.txt", "b.txt"])


@pytest.mark.asyncio
async def test_merged_batch_shares_the_page_budget_like_a_paths_batch(tmp_path):
    body = "\n".join(f"line {i} " + "x" * 90 for i in range(2_000))
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.txt").write_text(body, encoding="utf-8")

    merged = await ReadFileTool(workspace=tmp_path).execute(path="a.txt", paths=["b.txt", "c.txt"])
    batch = await ReadFileTool(workspace=tmp_path).execute(paths=["a.txt", "b.txt", "c.txt"])

    assert merged == batch
    assert all(len(r["content"]) < len(body) for r in merged["results"])  # cut to a share


@pytest.mark.asyncio
async def test_merged_read_pages_path_at_its_own_offset_and_limit(tmp_path):
    """`offset`/`limit` sent with `path` and `paths` page `path` as they would
    alone. A page the model has not seen must come back as that page, never as
    the "unchanged since last read" stub of the page it read before. The
    files in `paths` are read from their start."""
    (tmp_path / "big.txt").write_text(
        "\n".join(f"line {i}" for i in range(1, 3001)), encoding="utf-8",
    )
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)
    assert "Use offset=2001 to continue" in await tool.execute(path="big.txt")

    out = await tool.execute(path="big.txt", offset=2500, limit=10, paths=["b.txt"])

    big, b = out["results"]
    assert big["content"].startswith("2500| line 2500\n")
    assert "2509| line 2509" in big["content"] and "2510|" not in big["content"]
    assert b["content"].startswith("1| beta")


@pytest.mark.asyncio
async def test_merged_read_reads_the_pages_asked_for_path(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    _write_text_pdf(tmp_path / "doc.pdf", [f"Page {i + 1} content" for i in range(5)])
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)

    out = await tool.execute(path="doc.pdf", pages="2-3", paths=["b.txt"])

    pdf = out["results"][0]["content"]
    assert "Page 2 content" in pdf and "Page 3 content" in pdf
    assert "Page 1 content" not in pdf


@pytest.mark.asyncio
async def test_merged_path_and_paths_obey_the_paths_cap(tmp_path):
    tool = ReadFileTool(workspace=tmp_path)
    out = await tool.execute(path="extra.txt", paths=[f"f{i}.txt" for i in range(MAX_READ_PATHS)])
    assert out == f"Error: too many paths ({MAX_READ_PATHS + 1}); cap is {MAX_READ_PATHS} per call"


def test_fanout_size_counts_path_and_paths_once_each(tmp_path):
    tool = ReadFileTool(workspace=tmp_path)
    assert tool.fanout_size({"path": "a.txt", "paths": ["b.txt", "c.txt"]}) == 3
    assert tool.fanout_size({"path": "a.txt", "paths": ["a.txt", "b.txt"]}) == 2


@pytest.mark.asyncio
async def test_result_leaving_context_releases_the_path_of_a_merged_read(tmp_path):
    """Once a merged read's result is gone from the model's view, a repeat
    read of its `path` file must return the file again, not the stub that
    points at the vanished result."""
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path)
    arguments = {"path": "a.txt", "paths": ["b.txt"]}

    await tool.execute(**arguments)
    assert "unchanged since last read" in await tool.execute(path="a.txt")

    tool.result_left_context(arguments)

    again = await tool.execute(path="a.txt")
    assert "unchanged since last read" not in again
    assert "alpha" in again


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
