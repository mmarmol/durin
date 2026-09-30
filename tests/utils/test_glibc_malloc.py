"""glibc allocator introspection: stats shape and trim safety.

On glibc Linux the helpers return real numbers; everywhere else (macOS,
musl) they must degrade to None/False without raising — the callers treat
"no signal" and "not glibc" identically.
"""
from __future__ import annotations

import sys
import threading

import pytest

from durin.utils.glibc_malloc import malloc_stats_mb, malloc_trim

_LINUX = sys.platform.startswith("linux")


def test_stats_shape_on_glibc_none_elsewhere() -> None:
    stats = malloc_stats_mb()
    if not _LINUX:
        assert stats is None
        return
    # Linux CI runs glibc; a musl runner would legitimately return None.
    if stats is not None:
        assert stats["system_mb"] > 0.0
        assert stats["in_use_mb"] > 0.0
        assert stats["free_mb"] >= 0.0
        assert stats["system_mb"] >= stats["in_use_mb"]
        # What is in RAM of what malloc holds: never more than it holds.
        assert 0.0 < stats["resident_mb"] <= stats["system_mb"] + 0.2


def test_stats_track_a_large_allocation() -> None:
    stats = malloc_stats_mb()
    if stats is None:
        return
    blob = bytearray(64 * 2**20)
    grown = malloc_stats_mb()
    # A 64MB live allocation must be visible as in-use growth. (Large
    # blocks may be mmap'd — mallinfo2 counts those in hblkhd, which the
    # helper folds into system/in-use totals.)
    assert grown["in_use_mb"] >= stats["in_use_mb"] + 60
    del blob


def _resident_free_mb(stats: dict) -> float:
    return stats["resident_mb"] - stats["in_use_mb"]


def _resident_follows_trim_and_reuse() -> None:
    blocks = [bytearray(64 * 1024) for _ in range(1600)]     # ~100MB, touched
    live = blocks[1::2]                                       # fragments the heap
    del blocks
    held = malloc_stats_mb()
    assert _resident_free_mb(held) >= 40

    malloc_trim()
    trimmed = malloc_stats_mb()
    assert trimmed["resident_mb"] <= held["resident_mb"] - 40
    # The books still count the returned pages as free.
    assert trimmed["free_mb"] - _resident_free_mb(trimmed) >= 30

    churn = [bytearray(64 * 1024) for _ in range(640)]       # reuses the freed chunks
    del churn
    reused = malloc_stats_mb()
    assert _resident_free_mb(reused) >= _resident_free_mb(trimmed) + 30
    del live


@pytest.mark.parametrize("arena", ["main", "thread"])
def test_resident_follows_the_pages_a_trim_returns_and_reuse_brings_back(arena) -> None:
    """mallinfo2 keeps counting trimmed pages as free arena memory, so only
    ``resident_mb`` can tell what a trim actually handed back, and what
    later allocations paged back in without changing ``free_mb``. Both the
    main arena (the brk heap) and a worker thread's own arena count."""
    if malloc_stats_mb() is None:
        return
    if arena == "main":
        _resident_follows_trim_and_reuse()
        return
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            _resident_follows_trim_and_reuse()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            errors.append(exc)

    worker = threading.Thread(target=_run)
    worker.start()
    worker.join()
    if errors:
        raise errors[0]


def test_trim_is_safe_and_bool() -> None:
    released = malloc_trim()
    assert isinstance(released, bool)
    if not _LINUX:
        assert released is False
