"""Filesystem watcher for the memory subsystem (doc 02 §6.3).

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

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace).resolve()
        self._memory_root = self._workspace / "memory"
        self._queue: "Queue[object]" = Queue()
        self._processing_lock = threading.Lock()
        self._processing = False
        self._worker: Optional[threading.Thread] = None
        self._observer = None
        self._running = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._memory_root.mkdir(parents=True, exist_ok=True)
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

    def _worker_loop(self) -> None:
        """Drains the event queue. One thread, FIFO, serial."""
        from durin.memory.indexer import reindex_one_file

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
                    reindex_one_file(self._workspace, path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "file_watcher: reindex %s failed: %s",
                        path, exc,
                    )
            finally:
                with self._processing_lock:
                    self._processing = False
