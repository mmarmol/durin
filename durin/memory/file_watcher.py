"""Filesystem watcher for the memory subsystem.

Watches `<workspace>/memory/` for `.md` mutations and triggers a
synchronous `reindex_one_file` for each change. Edits under
`memory/archive/**` and `memory/pending/**` are ignored (matches
the `walk_memory` exclusion contract).

Lifecycle is explicit (`start()`/`stop()`) so the agent loop can
wire it in and tests can drive it deterministically.

What the OS is asked to report is kept to what the watcher acts on:

- Each top-level folder of `memory/` gets its own recursive watch,
  except `.git` (git's own reads and commits touch thousands of object
  files that are never memory), `archive` and `pending` (never indexed).
  The root itself is watched non-recursively, so a folder created later
  gets its watch when it appears. watchdog's FSEvents backend (macOS)
  never reports a subfolder's creation to a non-recursive watch, so the
  worker also reconciles the folder watches whenever it is idle.
- Every watch subscribes only to create / modify / move / delete. Without
  that filter the OS reports every file open and every read-only close,
  so any read of `memory/` — a `git rev-list`, a dream pass, a health
  scan — would turn into one Python event object per file touched.

The watcher serializes event processing through a single worker
thread: bursts (e.g. `git checkout` touching many files) are
processed FIFO without contention against LanceDB / FTS5 writes.
Queued paths are coalesced — a burst of writes to one file is one
re-index — and bounded: past `_MAX_PENDING_PATHS` distinct paths the
backlog collapses into a single rescan of `memory/`.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["MemoryFileWatcher"]


# Sentinel queued to signal the worker thread to exit.
_STOP_SENTINEL = object()

# Queued in place of every waiting path once the backlog passes
# `_MAX_PENDING_PATHS`; the worker answers it with `_run_rescan`.
_RESCAN = object()

# Queue key of the backfill chunk; at most one chunk waits at a time.
_BACKFILL_KEY = object()

# Most distinct paths allowed to wait for the worker, so a burst cannot
# grow memory without limit; past it the burst is answered with one walk
# of memory/ instead of being held path by path.
_MAX_PENDING_PATHS = 10_000

# Top-level folders of memory/ that get no watch (see the module docstring).
_UNWATCHED_FOLDERS = frozenset({".git", "archive", "pending"})


def _is_watchable(child: Path) -> bool:
    """A top-level entry of memory/ that gets its own recursive watch.
    Symlinked folders are skipped, as a recursive watch skips them too."""
    return child.name not in _UNWATCHED_FOLDERS and child.is_dir() and not child.is_symlink()


# Queued by `request_backfill()` so the worker backfills any vector rows
# missed while the process was down (crash, upgrade, or a run with no
# embedding model configured). Runs on the worker thread like every other
# queued item. The backfill is chunked: each item embeds at most
# `_BACKFILL_CHUNK` entries and re-queues itself with the cursor where it
# stopped, so live filesystem events that arrived meanwhile are drained
# between chunks (a fresh `/remember` waits for one chunk, never for the
# whole backlog) and `stop()` is honoured after the running chunk.
class _Backfill:
    __slots__ = ("cursor",)

    def __init__(self, cursor: tuple[str, str] | None = None) -> None:
        self.cursor = cursor


_BACKFILL_CHUNK = 50


class MemoryFileWatcher:
    """Watches ``<workspace>/memory/`` for `.md` mutations.

    Internally uses ``watchdog`` (FSEvents on macOS, inotify on Linux,
    ReadDirectoryChangesW on Windows; polling fallback otherwise).
    Each detected modification is queued for a worker thread that
    invokes :func:`durin.memory.indexer.reindex_one_file`.

    The watcher is intentionally **single-process state** — multiple
    instances within the same process for the same workspace would
    duplicate work. Tests should always pair ``start()`` with
    ``stop()`` to avoid leaking threads.
    """

    def __init__(self, workspace: Path, embedding_model: str | None = None) -> None:
        self._workspace = Path(workspace).resolve()
        self._memory_root = self._workspace / "memory"
        # Work waiting for the worker, oldest first, one entry per key: a
        # path string for a changed file, or one of the control keys above.
        # Guarded by `_cond`, which also guards `_processing`.
        self._pending: dict[object, object] = {}
        self._cond = threading.Condition()
        self._processing = False
        self._worker: Optional[threading.Thread] = None
        self._observer: Any = None
        self._handler: Any = None
        self._event_kinds: list[Any] = []
        # Watch of each top-level folder, by folder path; None when the OS
        # refused it.
        self._folder_watches: dict[str, Any] = {}
        self._running = False
        # N2: re-embed entity pages reactively (FTS via reindex_one_file is not
        # enough — nothing else embeds them at author/edit time). None disables
        # the vector half (FTS still runs).
        self._embedding_model = embedding_model
        self._vector_index = None
        self._vector_attempted = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._memory_root.mkdir(parents=True, exist_ok=True)
        # Lazy import keeps watchdog out of import-time when the
        # watcher isn't wired (CLI / tests that don't need it).
        from watchdog.events import (
            DirCreatedEvent,
            DirMovedEvent,
            FileCreatedEvent,
            FileDeletedEvent,
            FileModifiedEvent,
            FileMovedEvent,
            FileSystemEventHandler,
        )
        from watchdog.observers import Observer

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event):  # type: ignore[override]
                if not event.is_directory:
                    watcher._enqueue_path(str(event.src_path))

            def on_created(self, event):  # type: ignore[override]
                if event.is_directory:
                    watcher._on_folder_appeared(str(event.src_path))
                else:
                    watcher._enqueue_path(str(event.src_path))

            def on_deleted(self, event):  # type: ignore[override]
                # A file moved between two separately watched folders
                # reaches the source folder's watch as a delete; re-indexing
                # the vanished path drops its row, as the move would.
                if not event.is_directory:
                    watcher._enqueue_path(str(event.src_path))

            def on_moved(self, event):  # type: ignore[override]
                if event.is_directory:
                    watcher._on_folder_appeared(
                        str(event.dest_path), moved_from=str(event.src_path),
                    )
                    return
                # Re-index both endpoints: the source row goes, the
                # destination is indexed.
                watcher._enqueue_path(str(event.src_path))
                watcher._enqueue_path(str(event.dest_path))

        self._handler = _Handler()
        self._event_kinds = [
            FileCreatedEvent, FileModifiedEvent, FileMovedEvent, FileDeletedEvent,
            DirCreatedEvent, DirMovedEvent,
        ]
        self._folder_watches = {}
        self._observer = Observer()
        self._observer.schedule(
            self._handler, str(self._memory_root),
            recursive=False, event_filter=self._event_kinds,
        )
        self._observer.start()
        # The root watch is live, so a folder created from here on reaches
        # `_on_folder_appeared`; these are the ones already there.
        for child in sorted(self._memory_root.iterdir()):
            if _is_watchable(child):
                self._watch_folder(str(child))

        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"durin-memory-watcher-{self._workspace.name}",
            daemon=True,
        )
        self._worker.start()
        self._running = True

    def stop(self) -> None:
        if not self._running:
            return
        # Signal worker to exit + flush observer.
        self._put_control(_STOP_SENTINEL, _STOP_SENTINEL)
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
        self._folder_watches = {}
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        self._running = False

    def request_backfill(self) -> None:
        """Queue the vector backfill (see :class:`_Backfill`).

        Not part of :meth:`start`: the backfill builds the embedding
        provider and reads the whole vector table, so the agent loop
        requests it once it is serving rather than while the gateway boots.
        """
        self._put_control(_BACKFILL_KEY, _Backfill())

    # ------------------------------------------------------------------
    # introspection (for tests + dashboards)
    # ------------------------------------------------------------------

    def pending_events(self) -> int:
        with self._cond:
            return len(self._pending)

    def is_processing(self) -> bool:
        with self._cond:
            return self._processing

    # ------------------------------------------------------------------
    # watches
    # ------------------------------------------------------------------

    def _watch_folder(self, path: str) -> None:
        observer = self._observer
        if observer is None:
            return
        try:
            watch = observer.schedule(
                self._handler, path, recursive=True, event_filter=self._event_kinds,
            )
        except Exception as exc:  # noqa: BLE001
            # The folder vanished, or the OS refused the watch (e.g. the
            # inotify watch limit). Recorded without a watch so the idle
            # reconcile does not retry it every tick; the folder is tried
            # again when it next appears.
            logger.warning("file_watcher: cannot watch %s: %s", path, exc)
            watch = None
        self._folder_watches[path] = watch

    def _unwatch_folder(self, path: str) -> None:
        watch = self._folder_watches.pop(path, None)
        observer = self._observer
        if watch is None or observer is None:
            return
        try:
            observer.unschedule(watch)
        except KeyError:
            pass  # its emitter never started, so there is nothing to remove

    def _reconcile_folders(self) -> None:
        """Drop the watches of top-level folders that are gone and watch the
        ones that have none yet. Runs on the worker thread when idle."""
        for path in list(self._folder_watches):
            if not Path(path).is_dir():
                self._unwatch_folder(path)
        try:
            children = sorted(self._memory_root.iterdir())
        except OSError:
            return
        for child in children:
            if str(child) not in self._folder_watches and _is_watchable(child):
                self._on_folder_appeared(str(child))

    def _on_folder_appeared(self, path: str, *, moved_from: str | None = None) -> None:
        """A folder was created or moved in; only top-level folders matter
        here, since nested ones are covered by their top-level folder's
        recursive watch. Runs on the observer's dispatch thread, or on the
        worker thread from the idle reconcile."""
        folder = Path(path)
        if self._observer is None or folder.parent != self._memory_root:
            return  # stopped, or a nested folder
        if moved_from is not None:
            # The old watch follows the moved folder but would report it
            # under its old name.
            self._unwatch_folder(moved_from)
        if folder.name in _UNWATCHED_FOLDERS:
            return
        # A watch left over from an earlier folder of the same name stopped
        # when that folder was deleted, so it is replaced, not reused.
        self._unwatch_folder(path)
        self._watch_folder(path)
        # Files written between the folder's creation and its watch going
        # live produced no event; queue whatever it holds now. A moved-in
        # folder's files also had rows under the old path, which go.
        try:
            files = [p for p in folder.rglob("*.md") if p.is_file()]
        except OSError:
            return
        for file in files:
            self._enqueue_path(str(file))
            if moved_from is not None:
                self._enqueue_path(str(Path(moved_from) / file.relative_to(folder)))

    # ------------------------------------------------------------------
    # work queue
    # ------------------------------------------------------------------

    def _enqueue_path(self, path: str) -> None:
        """Queue one changed file for re-indexing.

        Only `.md` files under memory/ outside `archive/` and `pending/`
        (the `walk_memory` exclusion contract) are queued. A path already
        waiting is not queued again: the worker reads the file when it gets
        to it, so one re-index covers every write before that. While a
        rescan waits, nothing is queued — the rescan will see the change.
        """
        if not path.endswith(".md"):
            return
        try:
            parts = Path(path).relative_to(self._memory_root).parts
        except ValueError:
            return
        if parts and parts[0] in ("archive", "pending"):
            return
        with self._cond:
            if path in self._pending or _RESCAN in self._pending:
                return
            if len(self._pending) < _MAX_PENDING_PATHS:
                self._pending[path] = path
            else:
                # Keep the control items, drop every path, rescan instead.
                self._pending = {
                    key: item for key, item in self._pending.items()
                    if not isinstance(key, str)
                }
                self._pending[_RESCAN] = _RESCAN
                logger.warning(
                    "file_watcher: more than %d memory files changed faster "
                    "than they could be re-indexed; dropping the per-file "
                    "queue and rescanning memory/ instead",
                    _MAX_PENDING_PATHS,
                )
            self._cond.notify()

    def _put_control(self, key: object, item: object) -> None:
        with self._cond:
            self._pending.setdefault(key, item)
            self._cond.notify()

    def _take(self) -> object | None:
        """Pop the oldest waiting item, marking the worker busy; None when
        nothing arrived within the wait."""
        with self._cond:
            if not self._pending:
                self._cond.wait(timeout=0.5)
                if not self._pending:
                    return None
            key = next(iter(self._pending))
            item = self._pending.pop(key)
            if item is not _STOP_SENTINEL:
                self._processing = True
            return item

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get_vector_index(self):
        """Lazily build the VectorIndex (N2). None when no model is configured or
        the embedder can't load — the FTS half still runs."""
        if self._vector_attempted:
            return self._vector_index
        self._vector_attempted = True
        if not self._embedding_model:
            return None
        try:
            from durin.config.loader import load_config
            from durin.memory.embedding import provider_from_config
            from durin.memory.vector_index import VectorIndex
            self._vector_index = VectorIndex(
                self._workspace,
                provider_from_config(load_config(), model=self._embedding_model))
        except Exception as exc:  # noqa: BLE001
            logger.warning("file_watcher: vector index init failed: %s", exc)
            self._vector_index = None
        return self._vector_index

    def _reindex_path(self, path: Path) -> None:
        """Re-index one changed file: FTS always, vector for entity pages when an
        embedder is configured (N2 — nothing else embeds them reactively)."""
        from durin.memory.indexer import reindex_one_file, reindex_one_file_vector
        reindex_one_file(self._workspace, path)
        vi = self._get_vector_index()
        if vi is not None:
            reindex_one_file_vector(self._workspace, path, vi)

    def _run_backfill(self, cursor: tuple[str, str] | None) -> None:
        """Embed one chunk of entries that have an FTS row but no vector row.

        No-op when no embedding model is configured (`_get_vector_index`
        returns None). Best-effort — a failure here must not kill the
        worker thread, since the watcher keeps draining live events
        after it. When the chunk leaves entries behind, the next chunk
        is queued *behind* whatever live events arrived meanwhile.
        """
        vi = self._get_vector_index()
        if vi is None:
            return
        from durin.memory.indexer import backfill_missing_vectors
        try:
            result = backfill_missing_vectors(
                self._workspace, vi, cursor=cursor, limit=_BACKFILL_CHUNK,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("file_watcher: backfill failed: %s", exc)
            return
        for class_name, count in result.done.items():
            if count:
                logger.info(
                    "file_watcher: backfilled %d vector row(s) for %s",
                    count, class_name,
                )
        if result.cursor is not None:
            self._put_control(_BACKFILL_KEY, _Backfill(result.cursor))

    def _run_rescan(self) -> None:
        """Re-index every memory file the FTS index is behind on — the
        answer to a backlog too large to queue path by path.

        A file with no FTS row, or changed on disk since its row was
        written, is re-indexed (FTS and vector); an unchanged file costs one
        `stat`. Rows of files deleted during the burst are left to the
        health check, which prunes rows whose file is gone.
        """
        from durin.memory.paths import walk_memory
        from durin.memory.search import IndexCoverage

        coverage = IndexCoverage.load(self._workspace)
        count = 0
        for path in walk_memory(self._workspace):
            if not coverage.needs_scan(self._workspace, path):
                continue
            try:
                self._reindex_path(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("file_watcher: reindex %s failed: %s", path, exc)
            count += 1
        logger.info("file_watcher: rescan re-indexed %d file(s)", count)

    def _worker_loop(self) -> None:
        """Drains the work queue. One thread, FIFO, serial.

        A fresh thread has no bound telemetry logger, so
        `emit_tool_event` silently drops every `memory.index.write` /
        `memory.index.backfill` row this thread would otherwise produce
        (`reindex_one_file`, `reindex_one_file_vector`,
        `backfill_missing_vectors`). Bind the gateway's own session
        logger for the thread's lifetime, mirroring how other
        background threads bind their own key (`dream_supervisor` ->
        `get_session_logger("dream_supervisor")`).
        """
        from durin.telemetry.logger import (
            bind_telemetry,
            get_session_logger,
            reset_telemetry,
        )

        token = bind_telemetry(get_session_logger("gateway"), purpose="memory_index")
        try:
            while True:
                item = self._take()
                if item is None:
                    try:
                        self._reconcile_folders()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("file_watcher: folder reconcile failed: %s", exc)
                    continue
                if item is _STOP_SENTINEL:
                    return
                try:
                    if isinstance(item, _Backfill):
                        self._run_backfill(item.cursor)
                    elif item is _RESCAN:
                        try:
                            self._run_rescan()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("file_watcher: rescan failed: %s", exc)
                    else:
                        path = Path(str(item))
                        try:
                            self._reindex_path(path)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "file_watcher: reindex %s failed: %s",
                                path, exc,
                            )
                finally:
                    with self._cond:
                        self._processing = False
        finally:
            reset_telemetry(token)
