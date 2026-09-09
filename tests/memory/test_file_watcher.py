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

import threading
import time
from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.file_watcher import MemoryFileWatcher
from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import Backfill


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


def test_the_watcher_runs_the_backfill_off_the_startup_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start()` returns before the backfill runs; the worker thread runs it once."""
    may_proceed = threading.Event()
    ran = threading.Event()
    seen: dict[str, threading.Thread] = {}

    def fake_backfill(workspace, vi, **kwargs):
        may_proceed.wait(timeout=5.0)
        seen["thread"] = threading.current_thread()
        ran.set()
        return Backfill({}, None)

    monkeypatch.setattr(
        "durin.memory.indexer.backfill_missing_vectors", fake_backfill
    )

    watcher = MemoryFileWatcher(tmp_path, embedding_model="fake-model")
    monkeypatch.setattr(watcher, "_get_vector_index", lambda: object())

    watcher.start()
    try:
        # start() must return without waiting for the backfill: it's
        # still blocked on `may_proceed` at this point.
        assert watcher._running is True
        assert not ran.is_set(), "start() waited for the backfill to finish"

        may_proceed.set()
        assert ran.wait(timeout=5.0), "worker thread never ran the backfill"
        assert seen["thread"] is watcher._worker
        assert seen["thread"] is not threading.current_thread()
    finally:
        watcher.stop()


def test_worker_thread_binds_gateway_telemetry_for_the_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh thread has no bound telemetry logger, so `emit_tool_event`
    silently drops `memory.index.backfill` / `memory.index.write` rows.
    `_worker_loop` must bind the gateway's own session logger for the
    thread's lifetime so those rows land in the gateway's telemetry file."""
    from durin.telemetry.logger import current_telemetry

    ran = threading.Event()
    seen: dict[str, object] = {}

    def fake_backfill(workspace, vi, **kwargs):
        seen["logger"] = current_telemetry()
        ran.set()
        return Backfill({}, None)

    monkeypatch.setattr(
        "durin.memory.indexer.backfill_missing_vectors", fake_backfill
    )

    watcher = MemoryFileWatcher(tmp_path, embedding_model="fake-model")
    monkeypatch.setattr(watcher, "_get_vector_index", lambda: object())

    watcher.start()
    try:
        assert ran.wait(timeout=5.0), "worker thread never ran the backfill"
    finally:
        watcher.stop()

    tlog = seen.get("logger")
    assert tlog is not None, "worker thread must have a bound telemetry logger"
    assert tlog.session_key == "gateway"


def test_a_live_event_is_indexed_between_backfill_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunk that leaves entries behind re-queues itself *behind* the
    live events that arrived while it ran, so a fresh write waits for
    one chunk, never for the whole backlog."""
    order: list[str] = []
    first_chunk_started = threading.Event()
    may_finish_first_chunk = threading.Event()
    finished = threading.Event()

    def fake_backfill(workspace, vi, *, cursor=None, limit=None):
        order.append(f"backfill:{cursor}")
        if cursor is None:
            first_chunk_started.set()
            may_finish_first_chunk.wait(timeout=5.0)
            return Backfill({"episodic": limit}, ("episodic", "e1"))
        finished.set()
        return Backfill({"episodic": 1}, None)

    monkeypatch.setattr(
        "durin.memory.indexer.backfill_missing_vectors", fake_backfill
    )
    watcher = MemoryFileWatcher(tmp_path, embedding_model="fake-model")
    monkeypatch.setattr(watcher, "_get_vector_index", lambda: object())
    monkeypatch.setattr(
        watcher, "_reindex_path", lambda path: order.append(f"live:{path.name}"),
    )
    live = tmp_path / "memory" / "episodic" / "fresh.md"

    watcher.start()
    try:
        assert first_chunk_started.wait(timeout=5.0)
        watcher._queue.put(str(live))  # arrives while chunk 1 is running
        may_finish_first_chunk.set()
        assert finished.wait(timeout=5.0), "the second chunk never ran"
    finally:
        watcher.stop()

    assert order == [
        "backfill:None", "live:fresh.md", "backfill:('episodic', 'e1')",
    ]


def test_stop_is_honoured_after_the_running_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backfill that never runs out of entries must not keep the worker
    alive past `stop()`: the stop sentinel is drained after at most the
    chunk that was already queued."""
    calls: list[int] = []
    started = threading.Event()

    def endless_backfill(workspace, vi, *, cursor=None, limit=None):
        calls.append(1)
        started.set()
        time.sleep(0.05)
        return Backfill({"episodic": limit}, ("episodic", f"e{len(calls)}"))

    monkeypatch.setattr(
        "durin.memory.indexer.backfill_missing_vectors", endless_backfill
    )
    watcher = MemoryFileWatcher(tmp_path, embedding_model="fake-model")
    monkeypatch.setattr(watcher, "_get_vector_index", lambda: object())

    watcher.start()
    worker = watcher._worker
    assert started.wait(timeout=5.0)
    watcher.stop()

    assert worker is not None and not worker.is_alive()
    assert len(calls) <= 3
