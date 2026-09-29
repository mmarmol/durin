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

import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from watchdog.events import FileSystemEventHandler

from durin.memory import file_watcher as file_watcher_module
from durin.memory.entity_page import EntityPage
from durin.memory.file_watcher import MemoryFileWatcher
from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import Backfill, reindex_one_file


def _wait_until(cond, *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def _drained(watcher: MemoryFileWatcher) -> bool:
    return watcher.pending_events() == 0 and not watcher.is_processing()


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


def test_start_alone_never_runs_the_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backfill builds the embedding provider and reads the whole vector
    table, so `start()` (called while the gateway boots) must not run it:
    neither the provider nor the backfill is touched until it is requested."""
    touched: list[str] = []

    def fake_backfill(workspace, vi, **kwargs):
        touched.append("backfill")
        return Backfill({}, None)

    monkeypatch.setattr(
        "durin.memory.indexer.backfill_missing_vectors", fake_backfill
    )
    watcher = MemoryFileWatcher(tmp_path, embedding_model="fake-model")

    def fake_vector_index():
        touched.append("provider")
        return object()

    monkeypatch.setattr(watcher, "_get_vector_index", fake_vector_index)

    watcher.start()
    try:
        time.sleep(0.5)
        assert touched == []
        watcher.request_backfill()
        assert _wait_until(lambda: touched == ["provider", "backfill"])
    finally:
        watcher.stop()


def test_a_requested_backfill_runs_on_the_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`request_backfill()` returns before the backfill runs; the worker thread runs it once."""
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
        watcher.request_backfill()
        # request_backfill() must return without waiting for the backfill:
        # it's still blocked on `may_proceed` at this point.
        assert not ran.is_set(), "request_backfill() waited for the backfill"

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
        watcher.request_backfill()
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
    live = watcher._memory_root / "episodic" / "fresh.md"

    watcher.start()
    try:
        watcher.request_backfill()
        assert first_chunk_started.wait(timeout=5.0)
        watcher._enqueue_path(str(live))  # arrives while chunk 1 is running
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
    watcher.request_backfill()
    worker = watcher._worker
    assert started.wait(timeout=5.0)
    watcher.stop()

    assert worker is not None and not worker.is_alive()
    assert len(calls) <= 3


# ---------------------------------------------------------------------------
# What the OS reports: only the changes the watcher acts on, never .git
# ---------------------------------------------------------------------------

# Event kinds the OS emits for a plain read (open, then a close with no
# write) and for a close after writing — none of them is a change the
# watcher acts on.
_READ_EVENT_KINDS = {"FileOpenedEvent", "FileClosedNoWriteEvent", "FileClosedEvent"}


def _entry(entry_id: str, body: str = "body") -> str:
    return f"---\nid: {entry_id}\nheadline: {entry_id}\n---\n\n{body}\n"


def _simulate_git(git_dir: Path) -> None:
    """What a commit followed by `git rev-list` / `git show` does inside
    `.git`: loose objects in fan-out folders, the index written through a
    lock file renamed into place, a reflog append, then a read of every
    file."""
    for i in range(20):
        obj = git_dir / "objects" / f"{i:02x}" / ("f" * 38)
        obj.parent.mkdir(parents=True, exist_ok=True)
        obj.write_bytes(b"blob")
    lock = git_dir / "index.lock"
    lock.write_bytes(b"index")
    lock.replace(git_dir / "index")
    (git_dir / "logs").mkdir(exist_ok=True)
    with (git_dir / "logs" / "HEAD").open("a", encoding="utf-8") as fh:
        fh.write("entry\n")
    for path in git_dir.rglob("*"):
        if path.is_file():
            path.read_bytes()


def test_every_watch_filters_out_reads_and_none_covers_git(tmp_path: Path) -> None:
    """Pins how the observer is set up, on any platform: each top-level
    folder is watched on its own except `.git`, `archive` and `pending`, the
    root is watched non-recursively (for folders created later), and every
    watch subscribes only to create / modify / move / delete — never to
    opens or closes, which the OS would otherwise report for every read."""
    memory = tmp_path / "memory"
    for name in ("episodic", "entities", ".git", "archive", "pending"):
        (memory / name).mkdir(parents=True)

    watcher = MemoryFileWatcher(tmp_path)
    watcher.start()
    try:
        watches = {Path(e.watch.path).name: e.watch for e in watcher._observer.emitters}
    finally:
        watcher.stop()

    assert set(watches) == {"memory", "episodic", "entities"}
    assert watches["memory"].is_recursive is False
    assert watches["episodic"].is_recursive is True
    assert watches["entities"].is_recursive is True
    for watch in watches.values():
        kinds = {kind.__name__ for kind in watch.event_filter}
        assert kinds.isdisjoint(_READ_EVENT_KINDS | {"DirModifiedEvent"})
        assert {
            "FileCreatedEvent", "FileModifiedEvent", "FileMovedEvent", "FileDeletedEvent",
        } <= kinds


def test_reads_and_git_activity_reach_no_handler_while_edits_still_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real observer on a real folder: reading every file under memory/
    and git-like reads and writes inside `.git` hand the handler nothing and
    queue nothing, while creating, modifying and moving a memory file
    (within a folder and across folders) still queue a re-index of every
    path involved."""
    memory = tmp_path / "memory"
    episodic = memory / "episodic"
    stable = memory / "stable"
    episodic.mkdir(parents=True)
    stable.mkdir()
    edited = episodic / "edited.md"
    renamed = episodic / "renamed.md"
    crossing = episodic / "crossing.md"
    for path in (edited, renamed, crossing):
        path.write_text(_entry(path.stem), encoding="utf-8")
    git_dir = memory / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    _simulate_git(git_dir)

    delivered: list[tuple[str, str]] = []
    original_dispatch = FileSystemEventHandler.dispatch

    def recording_dispatch(self, event):
        delivered.append((type(event).__name__, str(event.src_path)))
        original_dispatch(self, event)

    monkeypatch.setattr(FileSystemEventHandler, "dispatch", recording_dispatch)
    watcher = MemoryFileWatcher(tmp_path)
    reindexed: list[Path] = []
    monkeypatch.setattr(watcher, "_reindex_path", reindexed.append)

    watcher.start()
    try:
        # Let the OS deliver whatever it still holds from the setup above.
        time.sleep(1.0)
        delivered.clear()
        reindexed.clear()

        for path in memory.rglob("*"):
            if path.is_file():
                path.read_bytes()
        _simulate_git(git_dir)
        time.sleep(1.0)

        assert [d for d in delivered if ".git" in Path(d[1]).parts] == []
        assert [d for d in delivered if d[0] in _READ_EVENT_KINDS] == []
        assert reindexed == []
        assert watcher.pending_events() == 0

        root = watcher._memory_root
        created = root / "episodic" / "created.md"
        created.write_text(_entry("created"), encoding="utf-8")
        edited.write_text(_entry("edited", "changed body"), encoding="utf-8")
        (root / "episodic" / "renamed.md").rename(root / "episodic" / "renamed-to.md")
        (root / "episodic" / "crossing.md").rename(root / "stable" / "crossing.md")

        expected = {
            root / "episodic" / "created.md",
            root / "episodic" / "edited.md",
            root / "episodic" / "renamed.md",
            root / "episodic" / "renamed-to.md",
            root / "episodic" / "crossing.md",
            root / "stable" / "crossing.md",
        }
        assert _wait_until(lambda: expected <= set(reindexed)), (
            f"missing: {sorted(str(p) for p in expected - set(reindexed))}"
        )
    finally:
        watcher.stop()


def test_a_folder_created_after_start_gets_its_own_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class folders are created lazily on their first write, so a fresh
    workspace starts with no folders to watch. A folder that appears later
    is picked up — including the file written right after `mkdir`, before
    its watch could exist — while a `.git` that appears later still gets no
    watch."""
    watcher = MemoryFileWatcher(tmp_path)
    reindexed: list[Path] = []
    monkeypatch.setattr(watcher, "_reindex_path", reindexed.append)
    root = watcher._memory_root

    watcher.start()
    try:
        folder = root / "session_summary"
        folder.mkdir()
        first = folder / "first.md"
        first.write_text(_entry("first"), encoding="utf-8")
        assert _wait_until(lambda: first in reindexed)

        second = folder / "second.md"
        second.write_text(_entry("second"), encoding="utf-8")
        assert _wait_until(lambda: second in reindexed)

        page = root / "entities" / "person" / "ada.md"
        page.parent.mkdir(parents=True)
        page.write_text("---\nname: Ada\n---\n\nbody\n", encoding="utf-8")
        assert _wait_until(lambda: page in reindexed)

        (root / ".git").mkdir()
        _simulate_git(root / ".git")
        time.sleep(0.5)
        watched = {Path(e.watch.path).name for e in watcher._observer.emitters}
    finally:
        watcher.stop()

    assert ".git" not in watched
    assert {"session_summary", "entities"} <= watched


# ---------------------------------------------------------------------------
# The work queue: coalesced by path, bounded
# ---------------------------------------------------------------------------


def _blocked_worker(
    watcher: MemoryFileWatcher, monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, list[str]]:
    """Park the worker on a first path so the test can pile work behind it.
    Returns the gate that releases it and the names re-indexed so far."""
    gate = threading.Event()
    calls: list[str] = []

    def fake_reindex(path: Path) -> None:
        calls.append(path.name)
        if path.name == "blocker.md":
            gate.wait(timeout=10.0)

    monkeypatch.setattr(watcher, "_reindex_path", fake_reindex)
    watcher.start()
    watcher._enqueue_path(str(watcher._memory_root / "episodic" / "blocker.md"))
    assert _wait_until(lambda: calls == ["blocker.md"])
    return gate, calls


def test_a_burst_of_writes_to_one_file_is_one_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = MemoryFileWatcher(tmp_path)
    gate, calls = _blocked_worker(watcher, monkeypatch)
    try:
        hot = str(watcher._memory_root / "episodic" / "hot.md")
        for _ in range(500):
            watcher._enqueue_path(hot)
        assert watcher.pending_events() == 1

        gate.set()
        assert _wait_until(lambda: _drained(watcher))
        time.sleep(0.2)
        assert calls == ["blocker.md", "hot.md"]
    finally:
        gate.set()
        watcher.stop()


def test_a_backlog_past_the_bound_collapses_into_one_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Distinct paths past the bound are not kept: the backlog becomes one
    rescan, logged once, and paths arriving while that rescan waits are
    absorbed by it. Once the rescan has started, new writes queue again."""
    monkeypatch.setattr(file_watcher_module, "_MAX_PENDING_PATHS", 5)
    watcher = MemoryFileWatcher(tmp_path)
    rescans: list[int] = []
    monkeypatch.setattr(watcher, "_run_rescan", lambda: rescans.append(1))
    gate, calls = _blocked_worker(watcher, monkeypatch)
    episodic = watcher._memory_root / "episodic"
    try:
        with caplog.at_level("WARNING", logger=file_watcher_module.__name__):
            for i in range(200):
                watcher._enqueue_path(str(episodic / f"burst-{i}.md"))
                assert watcher.pending_events() <= 5
        assert watcher.pending_events() == 1
        overflow_logs = [r for r in caplog.records if "rescan" in r.getMessage()]
        assert len(overflow_logs) == 1

        gate.set()
        assert _wait_until(lambda: rescans == [1] and _drained(watcher))
        assert calls == ["blocker.md"]

        watcher._enqueue_path(str(episodic / "after.md"))
        assert _wait_until(lambda: calls == ["blocker.md", "after.md"])
    finally:
        gate.set()
        watcher.stop()


def test_the_rescan_reindexes_only_files_the_index_is_behind_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged indexed files are skipped; a file changed after its row was
    written and a file with no row at all are re-indexed; archive and
    pending stay out."""
    watcher = MemoryFileWatcher(tmp_path)
    people = watcher._memory_root / "entities" / "person"
    unchanged = people / "unchanged.md"
    changed = people / "changed.md"
    for path in (unchanged, changed):
        EntityPage(type="person", name=path.stem, body="indexed").save(path)
        reindex_one_file(watcher._workspace, path)
    later = time.time() + 60
    os.utime(changed, (later, later))
    unindexed = people / "unindexed.md"
    EntityPage(type="person", name="unindexed", body="new").save(unindexed)
    archived = watcher._memory_root / "archive" / "entities" / "person" / "old.md"
    EntityPage(type="person", name="old", body="archived").save(archived)

    seen: list[Path] = []
    monkeypatch.setattr(watcher, "_reindex_path", seen.append)
    watcher._run_rescan()

    assert sorted(p.name for p in seen) == ["changed.md", "unindexed.md"]


# ---------------------------------------------------------------------------
# A folder whose watch stopped is watched again
# ---------------------------------------------------------------------------


def _folder_emitter(watcher: MemoryFileWatcher, name: str):
    """The observer's emitter for the top-level folder `name`, or None."""
    for emitter in list(watcher._observer.emitters):
        if Path(emitter.watch.path).name == name:
            return emitter
    return None


def test_a_folder_whose_watch_stopped_is_watched_again_and_caught_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watchdog stops a folder's watch for good when the folder is deleted,
    even if a folder of the same name is created right after, and on macOS
    nothing reports either. The idle reconcile replaces the stopped watch
    and re-indexes what changed in the folder while it was unwatched: the
    files it holds now and the indexed files that left it. Later writes
    are caught again."""
    watcher = MemoryFileWatcher(tmp_path)
    people = watcher._memory_root / "entities" / "person"
    gone = people / "gone.md"
    EntityPage(type="person", name="gone", body="indexed").save(gone)
    reindex_one_file(watcher._workspace, gone)
    reindexed: list[Path] = []
    monkeypatch.setattr(watcher, "_reindex_path", reindexed.append)

    watcher.start()
    try:
        stopped = _folder_emitter(watcher, "entities")
        assert stopped is not None
        # What watchdog does on its own when the watched folder is deleted.
        stopped.stop()
        gone.unlink()
        arrived = people / "arrived.md"
        EntityPage(type="person", name="arrived", body="new").save(arrived)

        assert _wait_until(lambda: {gone, arrived} <= set(reindexed)), reindexed
        replacement = _folder_emitter(watcher, "entities")
        assert replacement is not None and replacement is not stopped
        assert replacement.should_keep_running()

        later = people / "later.md"
        EntityPage(type="person", name="later", body="later").save(later)
        assert _wait_until(lambda: later in reindexed), reindexed
    finally:
        watcher.stop()


def test_a_folder_deleted_and_created_again_keeps_being_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same on the real filesystem, as a `reset --hard` that empties a
    class folder does: the folder is deleted and created again at once, and
    a file written into it afterwards is still re-indexed."""
    watcher = MemoryFileWatcher(tmp_path)
    folder = watcher._memory_root / "episodic"
    folder.mkdir(parents=True)
    (folder / "first.md").write_text(_entry("first"), encoding="utf-8")
    reindexed: list[Path] = []
    monkeypatch.setattr(watcher, "_reindex_path", reindexed.append)

    watcher.start()
    try:
        time.sleep(0.5)
        shutil.rmtree(folder)
        folder.mkdir()
        # Long enough for the OS to report it and for an idle reconcile.
        time.sleep(1.5)
        after = folder / "after.md"
        after.write_text(_entry("after"), encoding="utf-8")
        assert _wait_until(lambda: after in reindexed), reindexed
    finally:
        watcher.stop()
