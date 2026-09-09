"""Filesystem watcher for the memory subsystem.

Watches `<workspace>/memory/` for `.md` mutations and triggers a
synchronous `reindex_one_file` for each change. Edits under
`memory/archive/**` and `memory/pending/**` are ignored (matches
the `walk_memory` exclusion contract).

Lifecycle is explicit (`start()`/`stop()`) so the agent loop can
wire it in and tests can drive it deterministically.

The watcher serializes event processing through a single worker
thread: bursts (e.g. `git checkout` touching many files) are
processed FIFO without contention against LanceDB / FTS5 writes.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from queue import Empty, Queue
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["MemoryFileWatcher"]


# Sentinel pushed onto the queue to signal the worker thread to exit.
_STOP_SENTINEL = object()

# Queued by `start()` so the worker backfills any vector rows missed while
# the process was down (crash, upgrade, or a run with no embedding model
# configured). Runs on the worker thread like every other queued item, so
# `start()` returns immediately — a large backlog never delays the gateway
# binding its port. The backfill is chunked: each item embeds at most
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
        self._queue: "Queue[object]" = Queue()
        self._processing_lock = threading.Lock()
        self._processing = False
        self._worker: Optional[threading.Thread] = None
        self._observer = None
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
        # Queued before the observer starts, so it's the first item the
        # worker thread drains — ahead of any live filesystem event.
        self._queue.put(_Backfill())
        # Lazy import keeps watchdog out of import-time when the
        # watcher isn't wired (CLI / tests that don't need it).
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        watcher_queue = self._queue

        class _Handler(FileSystemEventHandler):
            def on_modified(self, event):  # type: ignore[override]
                if event.is_directory:
                    return
                watcher_queue.put(event.src_path)

            def on_created(self, event):  # type: ignore[override]
                if event.is_directory:
                    return
                watcher_queue.put(event.src_path)

            def on_moved(self, event):  # type: ignore[override]
                # Moves can be split — we re-index both endpoints if
                # they're under our root.
                if not event.is_directory:
                    watcher_queue.put(event.src_path)
                    watcher_queue.put(getattr(event, "dest_path", ""))

        self._observer = Observer()
        self._observer.schedule(
            _Handler(), str(self._memory_root), recursive=True,
        )
        self._observer.start()

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
        self._queue.put(_STOP_SENTINEL)
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        self._running = False

    # ------------------------------------------------------------------
    # introspection (for tests + dashboards)
    # ------------------------------------------------------------------

    def pending_events(self) -> int:
        return self._queue.qsize()

    def is_processing(self) -> bool:
        with self._processing_lock:
            return self._processing

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
            self._queue.put(_Backfill(result.cursor))

    def _worker_loop(self) -> None:
        """Drains the event queue. One thread, FIFO, serial.

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

        token = bind_telemetry(get_session_logger("gateway"))
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except Empty:
                    continue
                if item is _STOP_SENTINEL:
                    return
                with self._processing_lock:
                    self._processing = True
                try:
                    if isinstance(item, _Backfill):
                        self._run_backfill(item.cursor)
                        continue
                    path_str = str(item)
                    if not path_str.endswith(".md"):
                        continue
                    path = Path(path_str)
                    # Honour the same exclusion contract as `walk_memory`:
                    # archive/ and pending/ are off-limits.
                    try:
                        rel = path.relative_to(self._memory_root)
                    except ValueError:
                        continue
                    parts = rel.parts
                    if parts and parts[0] in ("archive", "pending"):
                        continue
                    try:
                        self._reindex_path(path)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "file_watcher: reindex %s failed: %s",
                            path, exc,
                        )
                finally:
                    with self._processing_lock:
                        self._processing = False
        finally:
            reset_telemetry(token)
