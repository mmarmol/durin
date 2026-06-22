"""Telemetry retention/rotation.

JSONL telemetry files under `~/.cache/durin/telemetry/` are:

- **Compressed** (`.jsonl` → `.jsonl.gz`) when older than 30 days.
- **Deleted** when older than 90 days (archive horizon).

The cron lives in the health-check tick (P2.4) so it runs daily
without a separate scheduler. Tests drive it directly.
"""

from __future__ import annotations

import gzip
import os
import time
from pathlib import Path

import pytest

from durin.telemetry.retention import (
    COMPRESSION_AGE_DAYS,
    DELETION_AGE_DAYS,
    run_retention,
)


def _make_jsonl(
    path: Path, age_days: float, content: str = "{}",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))


def test_constants_match_spec() -> None:
    assert COMPRESSION_AGE_DAYS == 30
    assert DELETION_AGE_DAYS == 90


def test_recent_files_untouched(tmp_path: Path) -> None:
    """Files newer than 30 days should not be compressed or deleted."""
    p = tmp_path / "recent.jsonl"
    _make_jsonl(p, age_days=5)
    summary = run_retention(tmp_path)
    assert summary["compressed"] == 0
    assert summary["deleted"] == 0
    assert p.exists()


def test_old_file_gets_compressed(tmp_path: Path) -> None:
    p = tmp_path / "old.jsonl"
    _make_jsonl(p, age_days=45, content='{"event": 1}')
    summary = run_retention(tmp_path)
    assert summary["compressed"] == 1
    # Original removed, .jsonl.gz exists with the same content.
    assert not p.exists()
    gz = tmp_path / "old.jsonl.gz"
    assert gz.exists()
    decompressed = gzip.decompress(gz.read_bytes()).decode("utf-8")
    assert '"event": 1' in decompressed


def test_very_old_gz_gets_deleted(tmp_path: Path) -> None:
    """A `.jsonl.gz` older than 90 days is removed (archive horizon)."""
    p = tmp_path / "ancient.jsonl.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(b"{}\n"))
    when = time.time() - 100 * 86400
    os.utime(p, (when, when))
    summary = run_retention(tmp_path)
    assert summary["deleted"] == 1
    assert not p.exists()


def test_does_not_touch_non_jsonl_files(tmp_path: Path) -> None:
    """Other files (e.g. config, scratch) under the dir are ignored."""
    other = tmp_path / "other.txt"
    _make_jsonl(other, age_days=200, content="just text")
    summary = run_retention(tmp_path)
    assert summary["compressed"] == 0
    assert summary["deleted"] == 0
    assert other.exists()


def test_missing_dir_no_op(tmp_path: Path) -> None:
    summary = run_retention(tmp_path / "does_not_exist")
    assert summary == {"compressed": 0, "deleted": 0, "errors": 0}


def test_compression_failure_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gzip failure increments `errors` but doesn't abort the run."""
    p = tmp_path / "broken.jsonl"
    _make_jsonl(p, age_days=45)

    real_compress = gzip.compress

    def fake_compress(data, *a, **kw):
        if p.exists() and p.read_bytes() in (data, data.encode() if isinstance(data, str) else data):
            raise OSError("simulated disk full")
        return real_compress(data, *a, **kw)

    monkeypatch.setattr("gzip.compress", fake_compress)
    summary = run_retention(tmp_path)
    assert summary["errors"] >= 1
