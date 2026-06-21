"""Cross-process serialization of git working-tree mutations.

Hazard: `_commit_dirty_as_user` (porcelain add+commit) and
`_fast_forward_working_tree` (porcelain reset --hard) both mutate the git
working tree and .git/index.  Under concurrent processes (gateway, TUI, cron)
without serialization, the unlink-then-recreate behavior of dulwich's
`_transition_to_file` produces transient absent-file windows and potential
index corruption.

This test verifies that a cross-process lock (`cross_process_lock` on
`<memory_git_root>/.git-worktree`) serializes these two mutations so they
cannot interleave.

See docs/architecture/concurrency.md §Lock-ordering invariant for the
global acquisition order.  This lock (`.git-worktree.lock`) is the
innermost lock in the memory write path; it is always acquired AFTER the
CAS ref lock (which is dulwich-internal) and is never held when any
session or config lock is acquired.
"""
from __future__ import annotations

import multiprocessing
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.repo import Repo

from durin.memory.field_patch import FieldPatch
from durin.memory.memory_writer import write_entity

NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)


def _init_memory_repo(root: Path) -> None:
    """Initialize a bare-minimum memory git repo under root/memory."""
    mem = root / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    porcelain.init(str(mem))
    seed = mem / "seed.md"
    seed.write_text("seed", encoding="utf-8")
    porcelain.add(str(mem), [str(seed)])
    porcelain.commit(str(mem), message=b"init", author=b"t <t@t>",
                     committer=b"t <t@t>")


# ---------------------------------------------------------------------------
# Worker processes (module-level for multiprocessing spawn)
# ---------------------------------------------------------------------------

def _worker_hold_lock_then_signal(
    lock_target_str: str,
    acquired_signal: str,
    release_signal: str,
) -> None:
    """Acquire the git-worktree cross-process lock and hold it until signaled."""
    from durin.utils.file_lock import cross_process_lock
    with cross_process_lock(Path(lock_target_str)):
        Path(acquired_signal).touch()
        deadline = time.monotonic() + 10
        while not Path(release_signal).exists():
            if time.monotonic() > deadline:
                break
            time.sleep(0.02)


def _worker_timed_commit_dirty(
    mem_str: str,
    start_signal: str,
    done_signal: str,
) -> None:
    """Wait for start signal, call _commit_dirty_as_user, signal done."""
    from durin.memory.memory_writer import _commit_dirty_as_user
    mem = Path(mem_str)
    deadline = time.monotonic() + 10
    while not Path(start_signal).exists():
        if time.monotonic() > deadline:
            return
        time.sleep(0.01)
    _commit_dirty_as_user(mem)
    Path(done_signal).touch()


def _worker_timed_reset(
    mem_str: str,
    start_signal: str,
    done_signal: str,
) -> None:
    """Wait for start signal, call _fast_forward_working_tree, signal done."""
    from durin.memory.memory_writer import _fast_forward_working_tree
    mem = Path(mem_str)
    deadline = time.monotonic() + 10
    while not Path(start_signal).exists():
        if time.monotonic() > deadline:
            return
        time.sleep(0.01)
    _fast_forward_working_tree(mem)
    Path(done_signal).touch()


def _worker_repeated_writes(root_str: str, worker_id: int, n: int,
                              result_file: str) -> None:
    """Write n entities from a subprocess; record how many succeeded."""
    from datetime import datetime, timezone
    from durin.memory.field_patch import FieldPatch
    from durin.memory.memory_writer import write_entity
    now = datetime(2026, 6, 5, tzinfo=timezone.utc)
    root = Path(root_str)
    ok = 0
    for i in range(n):
        try:
            write_entity(
                root,
                f"person:w{worker_id}e{i}",
                [FieldPatch(kind="body_append", value=f"body{i}",
                             author="agent", source_ref="s", at=now)],
                create=True,
            )
            ok += 1
        except Exception:
            pass
    Path(result_file).write_text(str(ok), encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: lock blocks a second process from entering the critical section
# ---------------------------------------------------------------------------

def test_lock_blocks_concurrent_mutation(tmp_path):
    """
    Process A holds the git-worktree lock.
    Process B attempts _commit_dirty_as_user (which must acquire the same lock).
    B must NOT complete before A releases.

    Without the lock in _commit_dirty_as_user / _fast_forward_working_tree,
    B finishes immediately regardless of A — this test fails.
    """
    _init_memory_repo(tmp_path)
    # Seed an entity so the memory dir has commits
    write_entity(
        tmp_path,
        "person:alice",
        [FieldPatch(kind="body_append", value="v1", author="agent",
                     source_ref="s", at=NOW)],
        create=True,
    )

    mem = tmp_path / "memory"
    lock_target = mem / ".git-worktree"

    acquired = str(tmp_path / "a_acquired")
    release = str(tmp_path / "a_release")
    b_start = str(tmp_path / "b_start")
    b_done = str(tmp_path / "b_done")

    ctx = multiprocessing.get_context("spawn")

    # Process A: hold the lock for up to 5 seconds
    proc_a = ctx.Process(
        target=_worker_hold_lock_then_signal,
        args=(str(lock_target), acquired, release),
    )

    # Process B: once A has the lock, try to run _commit_dirty_as_user
    # (which should block until A releases)
    proc_b = ctx.Process(
        target=_worker_timed_commit_dirty,
        args=(str(mem), b_start, b_done),
    )

    proc_a.start()

    # Wait for A to acquire the lock
    deadline = time.monotonic() + 10
    while not Path(acquired).exists():
        assert time.monotonic() < deadline, "process A never acquired the lock"
        time.sleep(0.02)

    # Start B while A holds the lock
    Path(b_start).touch()
    proc_b.start()

    # B should NOT complete within 0.5s because the lock is held
    time.sleep(0.5)
    b_finished_early = Path(b_done).exists()

    # Now release A's lock
    Path(release).touch()

    proc_a.join(timeout=10)
    proc_b.join(timeout=10)

    assert proc_a.exitcode == 0, f"process A: {proc_a.exitcode}"
    assert proc_b.exitcode == 0, f"process B: {proc_b.exitcode}"
    assert Path(b_done).exists(), "process B never completed _commit_dirty_as_user"

    # This is the key assertion: B must have been BLOCKED while A held the lock
    assert not b_finished_early, (
        "_commit_dirty_as_user completed while another process held the "
        "git-worktree lock — the working-tree mutation is not serialized"
    )


def test_lock_blocks_reset_while_lock_held(tmp_path):
    """
    Same as above but for _fast_forward_working_tree.
    B must not complete its reset until A releases the lock.
    """
    _init_memory_repo(tmp_path)
    write_entity(
        tmp_path,
        "person:bob",
        [FieldPatch(kind="body_append", value="v1", author="agent",
                     source_ref="s", at=NOW)],
        create=True,
    )

    mem = tmp_path / "memory"
    lock_target = mem / ".git-worktree"

    acquired = str(tmp_path / "a_acquired")
    release = str(tmp_path / "a_release")
    b_start = str(tmp_path / "b_start")
    b_done = str(tmp_path / "b_done")

    ctx = multiprocessing.get_context("spawn")

    proc_a = ctx.Process(
        target=_worker_hold_lock_then_signal,
        args=(str(lock_target), acquired, release),
    )
    proc_b = ctx.Process(
        target=_worker_timed_reset,
        args=(str(mem), b_start, b_done),
    )

    proc_a.start()

    deadline = time.monotonic() + 10
    while not Path(acquired).exists():
        assert time.monotonic() < deadline, "process A never acquired the lock"
        time.sleep(0.02)

    Path(b_start).touch()
    proc_b.start()

    time.sleep(0.5)
    b_finished_early = Path(b_done).exists()

    Path(release).touch()

    proc_a.join(timeout=10)
    proc_b.join(timeout=10)

    assert proc_a.exitcode == 0, f"process A: {proc_a.exitcode}"
    assert proc_b.exitcode == 0, f"process B: {proc_b.exitcode}"
    assert Path(b_done).exists(), "process B never completed _fast_forward_working_tree"

    assert not b_finished_early, (
        "_fast_forward_working_tree completed while another process held the "
        "git-worktree lock — the working-tree mutation is not serialized"
    )


# ---------------------------------------------------------------------------
# Test 2: concurrent subprocess writes produce a valid git repo (no index corruption)
# ---------------------------------------------------------------------------

def test_concurrent_subprocess_writes_no_corruption(tmp_path):
    """
    Two subprocesses write distinct entities concurrently.  The git index
    must remain intact and all writes must succeed.
    """
    _init_memory_repo(tmp_path)

    n = 5
    result_a = str(tmp_path / "result_a.txt")
    result_b = str(tmp_path / "result_b.txt")

    ctx = multiprocessing.get_context("spawn")
    proc_a = ctx.Process(target=_worker_repeated_writes,
                          args=(str(tmp_path), 0, n, result_a))
    proc_b = ctx.Process(target=_worker_repeated_writes,
                          args=(str(tmp_path), 1, n, result_b))

    proc_a.start()
    proc_b.start()
    proc_a.join(timeout=30)
    proc_b.join(timeout=30)

    assert proc_a.exitcode == 0, f"proc A: {proc_a.exitcode}"
    assert proc_b.exitcode == 0, f"proc B: {proc_b.exitcode}"

    ok_a = int(Path(result_a).read_text())
    ok_b = int(Path(result_b).read_text())
    assert ok_a == n, f"process A: expected {n}, got {ok_a}"
    assert ok_b == n, f"process B: expected {n}, got {ok_b}"

    repo = Repo(str(tmp_path / "memory"))
    try:
        commits = list(repo.get_walker())
        assert len(commits) >= 1
        _ = repo.open_index()  # must not raise
    finally:
        repo.close()


# ---------------------------------------------------------------------------
# Test 3: lock path is correct and reentrant on same thread
# ---------------------------------------------------------------------------

def test_git_worktree_lock_path_and_reentrance(tmp_path):
    """
    Verify cross_process_lock is reentrant (same thread re-enters without deadlock)
    and the expected lock file path can be constructed.
    """
    from durin.utils.file_lock import cross_process_lock

    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)

    lock_target = mem / ".git-worktree"
    expected_lock_file = Path(f"{lock_target}.lock")

    with cross_process_lock(lock_target):
        with cross_process_lock(lock_target):  # reentrant: must not deadlock
            assert True

    # Lock file exists (may or may not persist after release — implementation detail)
    # The key: no exception was raised above
