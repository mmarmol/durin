"""`memory_search` does its post-search work off the event loop and times it.

After the search pipeline returns, the tool converts hits, applies the
per-source cap, drops what is already in context, renders the text and looks
query entities up in the alias index. That work runs in the same worker thread
as the pipeline, so a slow render never freezes the gateway loop, and the
`memory.recall` row reports it: `duration_ms` is the pipeline alone,
`postprocess_duration_ms` the work after it, `total_duration_ms` the whole call.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.indexer import rebuild_fts_index

_SLOW_RENDER_S = 0.3


def _seed(workspace: Path) -> None:
    EntityPage(
        type="person", name="Marcelo", aliases=["m"], body="content",
    ).save(workspace / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(workspace)


def _capture_recall(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setattr(
        "durin.agent.tools.memory_search.emit_tool_event",
        lambda t, d: rows.append(d) if t == "memory.recall" else None,
    )
    return rows


def _slow_render(monkeypatch: pytest.MonkeyPatch, on_call=None) -> None:
    import durin.memory.sectioned_output as sectioned_output

    real = sectioned_output.render_sectioned

    def slow(*args, **kwargs):
        if on_call is not None:
            on_call()
        time.sleep(_SLOW_RENDER_S)
        return real(*args, **kwargs)

    monkeypatch.setattr(sectioned_output, "render_sectioned", slow)


def test_recall_row_times_the_pipeline_the_post_work_and_the_whole_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    rows = _capture_recall(monkeypatch)
    _slow_render(monkeypatch)
    tool = MemorySearchTool(workspace=tmp_path)

    asyncio.run(tool.execute(query="Marcelo"))

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row["postprocess_duration_ms"], float)
    assert isinstance(row["total_duration_ms"], float)
    # The slow render is post-search work, not pipeline time.
    assert row["postprocess_duration_ms"] >= _SLOW_RENDER_S * 1000
    assert row["duration_ms"] < row["postprocess_duration_ms"]
    assert row["total_duration_ms"] >= row["duration_ms"] + row["postprocess_duration_ms"]


def test_archive_recall_row_times_the_walk_the_post_work_and_the_whole_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scope='archive' has no pipeline: its walk over archived files takes
    that place in `duration_ms`, and its rendering is post-processing."""
    import durin.memory.storage as storage
    from durin.agent.tools.memory_search import MemorySearchTool

    archived = tmp_path / "memory" / "archive" / "episodic"
    archived.mkdir(parents=True)
    (archived / "ep-001.md").write_text(
        "---\nheadline: 'Trip to Paris'\nsummary: 'Visited the Louvre.'\n---\n"
        "Body about the Paris trip.\n",
        encoding="utf-8",
    )
    walk_s = 0.1
    real_split = storage.split_frontmatter

    def slow_split(text):
        time.sleep(walk_s)
        return real_split(text)

    monkeypatch.setattr(storage, "split_frontmatter", slow_split)
    rows = _capture_recall(monkeypatch)
    _slow_render(monkeypatch)
    tool = MemorySearchTool(workspace=tmp_path)

    asyncio.run(tool.execute(query="Paris", scope="archive"))

    assert len(rows) == 1
    row = rows[0]
    assert row["duration_ms"] >= walk_s * 1000
    assert row["postprocess_duration_ms"] >= _SLOW_RENDER_S * 1000
    assert row["total_duration_ms"] >= row["duration_ms"] + row["postprocess_duration_ms"]


def test_post_search_work_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    _capture_recall(monkeypatch)
    ticks = [0]
    seen: dict[str, int] = {}

    def at_render_start() -> None:
        seen["before"] = ticks[0]

    _slow_render(monkeypatch, on_call=at_render_start)
    tool = MemorySearchTool(workspace=tmp_path)

    async def run() -> None:
        async def ticker() -> None:
            while True:
                await asyncio.sleep(0.01)
                ticks[0] += 1

        task = asyncio.create_task(ticker())
        try:
            await tool.execute(query="Marcelo")
        finally:
            task.cancel()
        seen["after"] = ticks[0]

    asyncio.run(run())

    # The loop kept ticking while the render slept: it ran on another thread.
    assert seen["after"] - seen["before"] >= 5
