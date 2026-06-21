"""Unit tests for durin.utils.sqlite_util.

Tests verify:
- connect() sets WAL journal mode and a non-zero busy_timeout.
- execute_write() retries on a simulated "database is locked" error then
  commits successfully.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from durin.utils.sqlite_util import connect, execute_write


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

def test_connect_sets_wal_mode(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    conn = connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"expected wal, got {mode!r}"
    finally:
        conn.close()


def test_connect_sets_busy_timeout(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    conn = connect(db, busy_timeout_ms=3000)
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(timeout) > 0, "busy_timeout should be > 0"
    finally:
        conn.close()


def test_connect_busy_timeout_value(tmp_path: Path) -> None:
    db = tmp_path / "test.sqlite"
    conn = connect(db, busy_timeout_ms=7777)
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert int(timeout) == 7777
    finally:
        conn.close()


def test_connect_does_not_downgrade_existing_wal(tmp_path: Path) -> None:
    """Re-opening an already-WAL database must not downgrade it."""
    db = tmp_path / "existing.sqlite"
    c1 = connect(db)
    c1.close()
    c2 = connect(db)
    try:
        mode = c2.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        c2.close()


# ---------------------------------------------------------------------------
# execute_write()
# ---------------------------------------------------------------------------

def test_execute_write_retries_on_locked(tmp_path: Path) -> None:
    """execute_write must retry once when the first attempt raises 'database is locked'."""
    db = tmp_path / "retry.sqlite"
    conn = connect(db)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()

    call_count = 0

    def flaky_fn(c: sqlite3.Connection) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("database is locked")
        c.execute("INSERT INTO t VALUES (42)")

    with patch("durin.utils.sqlite_util.time.sleep"):
        execute_write(conn, flaky_fn)

    assert call_count == 2, "should have been called twice (one retry)"
    row = conn.execute("SELECT v FROM t").fetchone()
    assert row is not None and row[0] == 42, "value should be committed"
    conn.close()


def test_execute_write_retries_on_busy(tmp_path: Path) -> None:
    """'busy' in the error message also triggers retry."""
    db = tmp_path / "busy.sqlite"
    conn = connect(db)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()

    attempts_remaining = [2]

    def fn(c: sqlite3.Connection) -> None:
        if attempts_remaining[0] > 1:
            attempts_remaining[0] -= 1
            raise sqlite3.OperationalError("database is busy")
        c.execute("INSERT INTO t VALUES (99)")

    with patch("durin.utils.sqlite_util.time.sleep"):
        execute_write(conn, fn)

    row = conn.execute("SELECT v FROM t").fetchone()
    assert row is not None and row[0] == 99
    conn.close()


def test_execute_write_reraises_non_lock_errors(tmp_path: Path) -> None:
    """Non-lock OperationalErrors must propagate immediately."""
    db = tmp_path / "err.sqlite"
    conn = connect(db)

    def bad_fn(c: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("no such table: missing")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        execute_write(conn, bad_fn)

    conn.close()


def test_execute_write_exhausts_attempts(tmp_path: Path) -> None:
    """After all attempts are exhausted, the last OperationalError is re-raised."""
    db = tmp_path / "exhaust.sqlite"
    conn = connect(db)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()

    def always_locked(c: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("database is locked")

    with patch("durin.utils.sqlite_util.time.sleep"):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            execute_write(conn, always_locked, attempts=3)

    conn.close()


def test_execute_write_commits_on_success(tmp_path: Path) -> None:
    """A successful fn result is committed and visible on a second connection."""
    db = tmp_path / "commit.sqlite"
    conn = connect(db)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()

    execute_write(conn, lambda c: c.execute("INSERT INTO t VALUES (7)"))
    conn.close()

    conn2 = sqlite3.connect(str(db))
    row = conn2.execute("SELECT v FROM t").fetchone()
    conn2.close()
    assert row is not None and row[0] == 7
