"""Shared SQLite connection helpers for multi-process WAL databases.

See `docs/architecture/concurrency.md` for the broader concurrency model.
This module provides two primitives used wherever durin opens a SQLite
database that may have concurrent cross-process writers:

- ``connect`` — open with WAL mode, busy_timeout, and a DELETE fallback for
  network filesystems that reject WAL.
- ``execute_write`` — run a mutation inside ``BEGIN IMMEDIATE`` with
  jittered retry on SQLITE_BUSY.
"""

from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

__all__ = ["connect", "execute_write"]

# Phrases in the OperationalError message that indicate the filesystem cannot
# support WAL locking (NFS/SMB/FUSE).  We fall back to DELETE mode for these.
_WAL_FALLBACK_PHRASES = ("locking protocol", "not authorized", "disk i/o error")


def connect(
    path: str | Path,
    *,
    read_only: bool = False,
    busy_timeout_ms: int = 5000,
) -> sqlite3.Connection:
    """Open *path* as a SQLite database with sensible multi-process defaults.

    - ``check_same_thread=False`` — the caller is responsible for serialising
      concurrent access within the same process.
    - WAL mode is set unconditionally unless the on-disk header already
      reports a mode incompatible with WAL on this filesystem (NFS/SMB/FUSE),
      in which case we fall back to DELETE.  We never downgrade a database
      whose header already says WAL.
    - ``busy_timeout_ms`` configures SQLite's own wait before raising
      SQLITE_BUSY; the higher-level ``execute_write`` adds application-layer
      jitter on top.

    See `docs/architecture/concurrency.md` §SQLite helpers.
    """
    if read_only:
        # URI mode with mode=ro: never acquires a write lock.
        # WAL/journal/busy pragmas are intentionally skipped — a read-only
        # connection cannot set them and does not need to.
        return sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )

    # isolation_level=None disables Python's implicit transaction management
    # so we can issue BEGIN IMMEDIATE ourselves in execute_write.
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)

    # Check the on-disk journal mode *before* trying to set WAL so we never
    # downgrade a database whose header already reports WAL.
    current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    if current_mode != "wal":
        # Retry the WAL pragma: two processes opening the same newly-created
        # database simultaneously can race here (one holds a DDL lock during
        # schema CREATE while the other tries to switch journal mode).
        _last_exc: sqlite3.OperationalError | None = None
        for _ in range(20):
            try:
                conn.execute("PRAGMA journal_mode=WAL").fetchone()
                _last_exc = None
                break
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if any(phrase in msg for phrase in _WAL_FALLBACK_PHRASES):
                    conn.execute("PRAGMA journal_mode=DELETE")
                    _last_exc = None
                    break
                if "locked" in msg or "busy" in msg:
                    _last_exc = exc
                    time.sleep(random.uniform(0.02, 0.15))
                    continue
                raise
        if _last_exc is not None:
            raise _last_exc

    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def execute_write(
    conn: sqlite3.Connection,
    fn: Callable[[sqlite3.Connection], Any],
    *,
    attempts: int = 15,
) -> Any:
    """Run *fn(conn)* inside ``BEGIN IMMEDIATE … COMMIT`` with retry.

    ``BEGIN IMMEDIATE`` acquires a write lock at transaction start, ensuring
    no other writer can interleave.  On SQLITE_BUSY / locked errors it rolls
    back, sleeps a random 20–150 ms (jitter pattern from
    ``durin/memory/memory_writer.py``), and retries up to *attempts* times.

    Other ``OperationalError`` exceptions are re-raised immediately.

    See `docs/architecture/concurrency.md` §SQLite helpers.
    """
    last_exc: sqlite3.OperationalError | None = None
    for _ in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
                return result
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                last_exc = exc
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                time.sleep(random.uniform(0.02, 0.15))
                continue
            raise

    raise last_exc  # type: ignore[misc]
