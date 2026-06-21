"""Cross-process advisory file lock (reentrant, multi-platform).

Extracted from durin/channels/msteams.py:_refs_file_lock and generalized so
every whole-file read-modify-write can serialize across the gateway, the TUI's
own AgentLoop, cron and heartbeat processes that share one DURIN_HOME.

See docs/architecture/concurrency.md for lock-ordering invariants and the
Phase-A residual ledger.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

# Per-thread reentrancy: paths whose lock this thread already holds.
_held: threading.local = threading.local()
# In-process fallback locks when no OS primitive is available.
_fallback_locks: dict[str, threading.Lock] = {}
_fallback_guard = threading.Lock()


def _held_set() -> set[str]:
    s = getattr(_held, "paths", None)
    if s is None:
        s = set()
        _held.paths = s
    return s


@contextmanager
def cross_process_lock(target: Path, *, timeout: float = 15.0) -> Iterator[None]:
    lock_path = Path(f"{target}.lock")
    key = str(lock_path)
    if key in _held_set():
        yield  # reentrant: already held by this thread
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        with _fallback_guard:
            lk = _fallback_locks.setdefault(key, threading.Lock())
        if not lk.acquire(timeout=timeout):
            raise TimeoutError(f"lock timeout: {key}")
        _held_set().add(key)
        try:
            yield
        finally:
            _held_set().discard(key)
            lk.release()
        return

    fp = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # pragma: no cover - Windows
                    msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock timeout: {key}") from exc
                time.sleep(0.025)
        _held_set().add(key)
        yield
    finally:
        _held_set().discard(key)
        try:
            if fcntl is not None:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                try:
                    msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            fp.close()
