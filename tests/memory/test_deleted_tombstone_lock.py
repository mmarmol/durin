"""Cross-process lost-update test for the .deleted.json tombstone set.

Two concurrent processes each call clear_delete_tombstone on a DIFFERENT ref;
without a lock one process's load happens before the other's save, so one
removal is overwritten and that ref remains tombstoned.

This test:
1. Seeds .deleted.json with two refs.
2. Spawns two processes, each clearing a different ref concurrently with no
   yield between load and save.
3. Asserts BOTH refs are absent after both processes finish.

The test FAILS (lockless) because the last writer wins and one removal is lost.
After the fix (_mutate_deleted wraps the RMW under cross_process_lock), both
removals survive and the test passes.
"""
from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Worker processes (module-level for multiprocessing "spawn" compatibility)
# ---------------------------------------------------------------------------

def _worker_clear(workspace: str, ref: str, ready_event_path: str,
                  start_event_path: str) -> None:
    """Subprocess: signal readiness, wait for start, then clear one tombstone."""
    from durin.memory.deletion import clear_delete_tombstone

    ready = Path(ready_event_path)
    start = Path(start_event_path)
    ready.touch()
    # Busy-wait for the start flag (cheap; short-lived test)
    deadline = time.monotonic() + 10.0
    while not start.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("start flag never appeared")
        time.sleep(0.002)
    clear_delete_tombstone(Path(workspace), ref)


def _worker_add_tombstone(workspace: str, ref: str, ready_event_path: str,
                          start_event_path: str) -> None:
    """Subprocess: signal readiness, wait for start, then add one tombstone.

    Uses _mutate_deleted (the locked path) to simulate the fixed delete_entity
    tombstone step without performing the archive file move.
    """
    from durin.memory.deletion import _mutate_deleted

    ready = Path(ready_event_path)
    start = Path(start_event_path)
    ws = Path(workspace)
    ready.touch()
    deadline = time.monotonic() + 10.0
    while not start.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("start flag never appeared")
        time.sleep(0.002)
    _mutate_deleted(ws, lambda refs: refs.add(ref))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_deleted(workspace: Path, refs: list[str]) -> None:
    p = workspace / "memory" / ".deleted.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(refs)), encoding="utf-8")


def _load_deleted(workspace: Path) -> set[str]:
    p = workspace / "memory" / ".deleted.json"
    if not p.exists():
        return set()
    return set(json.loads(p.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_concurrent_clear_tombstone_both_removals_survive(tmp_path):
    """Both concurrent clear_delete_tombstone calls must take effect.

    Without the lock the last writer wins and one removal is lost (the ref
    stays tombstoned); with the lock both removals survive.
    """
    ctx = multiprocessing.get_context("spawn")

    ref_a = "company:alpha"
    ref_b = "company:beta"
    bystander = "company:gamma"

    _seed_deleted(tmp_path, [ref_a, ref_b, bystander])

    # Coordination files
    ready_a = tmp_path / "ready_a"
    ready_b = tmp_path / "ready_b"
    start_flag = tmp_path / "start"

    p1 = ctx.Process(
        target=_worker_clear,
        args=(str(tmp_path), ref_a, str(ready_a), str(start_flag)),
        daemon=True,
    )
    p2 = ctx.Process(
        target=_worker_clear,
        args=(str(tmp_path), ref_b, str(ready_b), str(start_flag)),
        daemon=True,
    )
    p1.start()
    p2.start()

    # Wait until both workers have loaded .deleted.json (ready signal)
    deadline = time.monotonic() + 10.0
    while not (ready_a.exists() and ready_b.exists()):
        assert time.monotonic() < deadline, "workers did not signal readiness"
        time.sleep(0.005)

    # Inject a brief pause so both workers are mid-RMW before we fire start
    time.sleep(0.02)
    # Without the lock: both workers load the same snapshot here.
    # With the lock: one blocks until the other finishes, then re-reads.
    start_flag.touch()

    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode == 0, f"worker 1 exited with {p1.exitcode}"
    assert p2.exitcode == 0, f"worker 2 exited with {p2.exitcode}"

    remaining = _load_deleted(tmp_path)
    # Both refs must be gone; the bystander ref must be untouched
    assert ref_a not in remaining, (
        f"{ref_a!r} still tombstoned — lost-update: one clear was overwritten"
    )
    assert ref_b not in remaining, (
        f"{ref_b!r} still tombstoned — lost-update: one clear was overwritten"
    )
    assert bystander in remaining, "bystander ref incorrectly removed"


def test_concurrent_clear_and_add_both_mutations_survive(tmp_path):
    """Concurrent clear (process 1) + tombstone add (process 2) both land.

    Without the lock one update is lost; with the lock both survive.
    """
    ctx = multiprocessing.get_context("spawn")

    ref_clear = "company:alpha"
    ref_add = "company:delta"

    _seed_deleted(tmp_path, [ref_clear])

    ready_a = tmp_path / "ready_a"
    ready_b = tmp_path / "ready_b"
    start_flag = tmp_path / "start"

    p1 = ctx.Process(
        target=_worker_clear,
        args=(str(tmp_path), ref_clear, str(ready_a), str(start_flag)),
        daemon=True,
    )
    p2 = ctx.Process(
        target=_worker_add_tombstone,
        args=(str(tmp_path), ref_add, str(ready_b), str(start_flag)),
        daemon=True,
    )
    p1.start()
    p2.start()

    deadline = time.monotonic() + 10.0
    while not (ready_a.exists() and ready_b.exists()):
        assert time.monotonic() < deadline, "workers did not signal readiness"
        time.sleep(0.005)

    time.sleep(0.02)
    start_flag.touch()

    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode == 0, f"worker 1 exited with {p1.exitcode}"
    assert p2.exitcode == 0, f"worker 2 exited with {p2.exitcode}"

    remaining = _load_deleted(tmp_path)
    assert ref_clear not in remaining, (
        f"{ref_clear!r} still tombstoned — clear was lost"
    )
    assert ref_add in remaining, (
        f"{ref_add!r} not tombstoned — add was lost"
    )
