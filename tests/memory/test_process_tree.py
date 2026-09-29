"""RSS sampling helpers (dream observability + supervisor watchdog)."""
from __future__ import annotations

import os
import subprocess
import sys

from durin.utils.process_tree import process_rss_mb, tree_rss_mb


def test_own_process_rss_is_positive() -> None:
    assert process_rss_mb() > 0


def test_unknown_pid_reports_zero() -> None:
    assert process_rss_mb(2**22 + 12345) == 0.0
    assert tree_rss_mb(2**22 + 12345) == (0.0, 0.0)


def test_tree_rss_counts_a_live_child() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; x = 'a' * (30 * 2**20); time.sleep(30)"],
    )
    try:
        import time

        # The child allocates ~30MB then sleeps; poll briefly until visible.
        for _ in range(50):
            _root, descendants = tree_rss_mb(os.getpid())
            if descendants >= 20:
                break
            time.sleep(0.1)
        root, descendants = tree_rss_mb(os.getpid())
        assert root > 0
        assert descendants >= 20
    finally:
        child.kill()
        child.wait()


def test_memory_snapshot_shape() -> None:
    from durin.utils.process_tree import memory_snapshot

    snap = memory_snapshot()
    assert snap["rss_mb"] > 0
    assert snap["threads"] >= 1
    assert len(snap["gc_counts"]) == 3


def test_memory_snapshot_has_malloc_fields() -> None:
    from durin.utils.glibc_malloc import malloc_stats_mb
    from durin.utils.process_tree import memory_snapshot

    snap = memory_snapshot()
    # Always present; 0.0 = not glibc / unknown (same convention as
    # available_memory_mb).
    if malloc_stats_mb() is not None:
        assert snap["malloc_system_mb"] > 0.0
        assert snap["malloc_in_use_mb"] > 0.0
        assert snap["malloc_free_mb"] >= 0.0
        assert snap["malloc_resident_mb"] > 0.0
    else:
        assert snap["malloc_system_mb"] == 0.0
        assert snap["malloc_in_use_mb"] == 0.0
        assert snap["malloc_free_mb"] == 0.0
        assert snap["malloc_resident_mb"] == 0.0


def _malloc(system_mb: float, resident_mb: float | None = None) -> dict:
    return {"system_mb": system_mb, "in_use_mb": 170.0, "free_mb": system_mb - 170.0,
            "resident_mb": system_mb if resident_mb is None else resident_mb}


def test_memory_snapshot_reports_resident_memory_outside_malloc(monkeypatch) -> None:
    """Growth that never touches glibc malloc (CPython's own object arenas,
    native libraries that map memory themselves) must be visible on its own,
    not hidden inside rss_mb."""
    from durin.utils.process_tree import memory_snapshot

    monkeypatch.setattr("durin.utils.process_tree.tree_rss_mb", lambda: (900.0, 0.0))
    monkeypatch.setattr("durin.utils.glibc_malloc.malloc_stats_mb", lambda: _malloc(420.0))
    snap = memory_snapshot()
    assert snap["non_malloc_mb"] == 480.0
    assert snap["malloc_resident_mb"] == 420.0

    # After a trim the arenas still count the released pages (system 420)
    # while only 190MB of them is resident: what lives outside malloc is
    # rss minus the resident part, not minus the books.
    monkeypatch.setattr("durin.utils.process_tree.tree_rss_mb", lambda: (300.0, 0.0))
    monkeypatch.setattr(
        "durin.utils.glibc_malloc.malloc_stats_mb", lambda: _malloc(420.0, resident_mb=190.0))
    assert memory_snapshot()["non_malloc_mb"] == 110.0

    # Residency unreadable: unknown, not a figure that hides trimmed pages.
    monkeypatch.setattr(
        "durin.utils.glibc_malloc.malloc_stats_mb", lambda: _malloc(420.0, resident_mb=0.0))
    assert memory_snapshot()["non_malloc_mb"] == 0.0

    # No allocator signal (not glibc): unknown, not "all of rss".
    monkeypatch.setattr("durin.utils.glibc_malloc.malloc_stats_mb", lambda: None)
    snap = memory_snapshot()
    assert snap["non_malloc_mb"] == 0.0
    assert snap["malloc_resident_mb"] == 0.0


def test_a_trim_does_not_move_the_memory_outside_malloc() -> None:
    """On real glibc: a trim returns freed arena pages to the OS and nothing
    else, so the resident memory outside malloc must read the same on both
    sides of it (instead of dropping by what the trim released)."""
    from durin.utils.glibc_malloc import malloc_stats_mb, malloc_trim
    from durin.utils.process_tree import memory_snapshot

    if malloc_stats_mb() is None:
        return
    blocks = [bytearray(64 * 1024) for _ in range(1600)]     # ~100MB, touched
    live = blocks[1::2]
    del blocks
    before = memory_snapshot()
    malloc_trim()
    after = memory_snapshot()
    assert before["rss_mb"] - after["rss_mb"] >= 40            # the trim did release
    assert abs(after["non_malloc_mb"] - before["non_malloc_mb"]) <= 10
    del live
