"""Memory file watcher.

Detects manual edits under `memory/` and triggers
`reindex_one_file` synchronously, plus auto-commits to
`memory/.git/` with `author: user`.

The watcher's lifecycle is decoupled from agent loop in these tests:
we instantiate, start, mutate the filesystem, give it a short window
to flush events, and stop. Production wiring lives in
`AgentLoop.start` and is exercised separately.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.file_watcher import MemoryFileWatcher
from durin.memory.fts_index import FTSIndex


def _flush(watcher: MemoryFileWatcher, *, timeout_s: float = 5.0) -> None:
    """Wait until the watcher's event queue drains, or timeout.

    Adds a small grace at the start so FSEvents / inotify has a beat
    to enqueue the event before we look at `pending_events`.
    """
    time.sleep(0.2)
    deadline = time.time() + timeout_s
    saw_activity = False
    while time.time() < deadline:
        pending = watcher.pending_events()
        processing = watcher.is_processing()
        if pending > 0 or processing:
            saw_activity = True
        if saw_activity and pending == 0 and not processing:
            return
        time.sleep(0.05)


@pytest.fixture
def workspace_with_entity(tmp_path: Path) -> Path:
    page = EntityPage(
        type="person", name="Marcelo", aliases=["m"], body="initial body",
    )
    page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    return tmp_path


def test_edit_triggers_reindex(workspace_with_entity: Path) -> None:
    """Modifying an entity page under memory/ flushes a re-index
    through the watcher so the next FTS search sees the new content."""
    page_path = (
        workspace_with_entity / "memory" / "entities" / "person"
        / "marcelo.md"
    )

    watcher = MemoryFileWatcher(workspace_with_entity)
    watcher.start()
    try:
        # Edit the page — simulating vim save.
        page = EntityPage.from_file(page_path)
        page.body = "manual edit by user about kubernetes deploys"
        page.save(page_path)
        _flush(watcher)
    finally:
        watcher.stop()

    with FTSIndex.open(workspace_with_entity) as idx:
        hits = idx.search("kubernetes")
    assert any("marcelo" in (h.path or "") for h in hits), (
        "watcher didn't pick up the manual edit"
    )


def test_excludes_archive_paths(tmp_path: Path) -> None:
    """Edits under memory/archive/** must NOT trigger re-index."""
    archive_dir = tmp_path / "memory" / "archive" / "episodic"
    archive_dir.mkdir(parents=True)
    archived = archive_dir / "old.md"
    archived.write_text(
        "---\nid: old\nheadline: archived\n---\n\nbody\n",
        encoding="utf-8",
    )

    watcher = MemoryFileWatcher(tmp_path)
    watcher.start()
    try:
        archived.write_text(
            "---\nid: old\nheadline: archived\n---\n\nbody update\n",
            encoding="utf-8",
        )
        _flush(watcher)
    finally:
        watcher.stop()

    with FTSIndex.open(tmp_path) as idx:
        assert idx.count() == 0, (
            "archive edit should not surface in the live FTS index"
        )


def test_excludes_pending_paths(tmp_path: Path) -> None:
    pending_dir = tmp_path / "memory" / "pending"
    pending_dir.mkdir(parents=True)
    p = pending_dir / "raw.md"
    p.write_text(
        "---\nid: raw\nheadline: pending\n---\n\nbody\n",
        encoding="utf-8",
    )

    watcher = MemoryFileWatcher(tmp_path)
    watcher.start()
    try:
        p.write_text(
            "---\nid: raw\nheadline: pending\n---\n\nupdated\n",
            encoding="utf-8",
        )
        _flush(watcher)
    finally:
        watcher.stop()

    with FTSIndex.open(tmp_path) as idx:
        assert idx.count() == 0


def test_start_stop_idempotent(workspace_with_entity: Path) -> None:
    watcher = MemoryFileWatcher(workspace_with_entity)
    watcher.start()
    watcher.start()  # double start — no-op
    watcher.stop()
    watcher.stop()  # double stop — no-op


def test_pending_events_counter(workspace_with_entity: Path) -> None:
    """Internal counter for `_flush`-style synchronisation in tests
    (and for future dashboards). Starts at 0; bumps on enqueue;
    decrements when the event is processed."""
    watcher = MemoryFileWatcher(workspace_with_entity)
    assert watcher.pending_events() == 0
    watcher.start()
    try:
        page_path = (
            workspace_with_entity / "memory" / "entities" / "person"
            / "marcelo.md"
        )
        page = EntityPage.from_file(page_path)
        page.body = "body v2"
        page.save(page_path)
        # Give watcher a moment to enqueue, then flush.
        _flush(watcher)
        assert watcher.pending_events() == 0
    finally:
        watcher.stop()
