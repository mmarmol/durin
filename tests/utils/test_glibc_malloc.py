"""glibc allocator introspection: stats shape and trim safety.

On glibc Linux the helpers return real numbers; everywhere else (macOS,
musl) they must degrade to None/False without raising — the callers treat
"no signal" and "not glibc" identically.
"""
from __future__ import annotations

import sys

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


def test_trim_is_safe_and_bool() -> None:
    released = malloc_trim()
    assert isinstance(released, bool)
    if not _LINUX:
        assert released is False
