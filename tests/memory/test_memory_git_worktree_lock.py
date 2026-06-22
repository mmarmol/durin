"""Cross-process serialization of git working-tree mutations.

Sub-hazard A: `_commit_dirty_as_user` (porcelain add+commit) and
`_fast_forward_working_tree` (porcelain reset --hard) both mutate the git
working tree and .git/index.  Under concurrent processes (gateway, TUI, cron)
without serialization, the unlink-then-recreate behavior of dulwich's
`_transition_to_file` can corrupt .git/index.

Sub-hazard B: dulwich reset --hard transiently removes files before recreating
them (_transition_to_file = unlink + write).  A concurrent reader that sees
is_file()==False during this window would permanently prune the FTS/vector row
for a file that is still valid.  The fix: prune paths acquire the same
git-worktree lock before deleting and re-check is_file() after acquiring it.
If the file is present on re-check the absence was transient and the row is
kept.

This file verifies both A (mutation serialization) and B (prune-path recheck).
Tests 1 and 2 are load-bearing for A: they fail if the lock is removed from
_commit_dirty_as_user / _fast_forward_working_tree.
Tests 3 and 4 are load-bearing for B: they fail if the recheck is removed from
reindex_one_file / prune_orphan_rows.

.git-worktree.lock is the OUTERMOST memory lock; FTS/LanceDB deletes (inner)
are always taken after it.
"""
from __future__ import annotations

import multiprocessing
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from dulwich import porcelain

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


# ---------------------------------------------------------------------------
# Test 1: lock blocks a second process from entering the critical section
# (load-bearing for sub-hazard A: fails if lock removed from
# _commit_dirty_as_user)
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
    from durin.memory.memory_writer import git_worktree_lock_path
    lock_target = git_worktree_lock_path(mem)

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


# ---------------------------------------------------------------------------
# Test 2: lock blocks reset while lock is held
# (load-bearing for sub-hazard A: fails if lock removed from
# _fast_forward_working_tree)
# ---------------------------------------------------------------------------

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
    from durin.memory.memory_writer import git_worktree_lock_path
    lock_target = git_worktree_lock_path(mem)

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
# Test 3: reindex_one_file skips prune on transient absence (sub-hazard B)
# (load-bearing: fails if the lock+recheck is removed from reindex_one_file)
# ---------------------------------------------------------------------------

def test_reindex_one_file_skips_prune_on_transient_absent(tmp_path):
    """Simulates a dulwich reset --hard absent-file window for reindex_one_file.

    A file appears absent on the FIRST is_file() call (mid-reset) but present
    on the SECOND call (after acquiring the git-worktree lock, reset complete).
    The FTS row must SURVIVE (not be pruned).

    Without the lock+recheck in reindex_one_file (the fix for sub-hazard B),
    the row is deleted on the first absent observation — this test fails.
    """
    from durin.memory.fts_index import FTSIndex
    from durin.memory.indexer import reindex_one_file

    workspace = tmp_path
    mem = workspace / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    entities = mem / "entities" / "person"
    entities.mkdir(parents=True, exist_ok=True)

    # Create a real entity page (needs frontmatter with type + name to index).
    md_path = entities / "alice.md"
    md_path.write_text(
        "---\ntype: person\nname: Alice\n---\n\nAlice is a test entity.\n",
        encoding="utf-8",
    )

    # Index the file so there is a row to potentially prune.
    reindex_one_file(workspace, md_path, trigger="test")
    with FTSIndex.open(workspace) as idx:
        row_exists_before = "person:alice" in idx.uris_with_prefix("person:alice")
    assert row_exists_before, "setup: row must exist before the transient-absent test"

    # Simulate transient absence: is_file() returns False on the first call
    # (the watcher sees the file is gone mid-reset) then True on subsequent
    # calls (the reset completed, the file is back).
    # The lock+recheck in reindex_one_file acquires the git-worktree lock and
    # calls is_file() a second time — that second call must return True so the
    # prune is skipped.
    call_count = {"n": 0}
    real_is_file = Path.is_file

    def _patched_is_file(self: Path) -> bool:
        if self == md_path:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return False   # first call: simulate mid-reset absence
            return True        # subsequent calls: file is back
        return real_is_file(self)

    with patch.object(Path, "is_file", _patched_is_file):
        reindex_one_file(workspace, md_path, trigger="test")

    # The row must STILL EXIST after the transient-absent call.
    with FTSIndex.open(workspace) as idx:
        row_exists_after = "person:alice" in idx.uris_with_prefix("person:alice")

    assert row_exists_after, (
        "reindex_one_file pruned the FTS row on a TRANSIENT absent-file "
        "observation — the lock+recheck (sub-hazard B fix) is missing or broken"
    )
    # Confirm we actually hit the transient path (at least 2 is_file calls).
    assert call_count["n"] >= 2, (
        "is_file was called fewer than 2 times — the recheck path was not taken"
    )


# ---------------------------------------------------------------------------
# Test 4: prune_orphan_rows skips prune on transient absence (sub-hazard B)
# (load-bearing: fails if the lock+recheck is removed from prune_orphan_rows)
# ---------------------------------------------------------------------------

def test_prune_orphan_rows_skips_prune_on_transient_absent(tmp_path):
    """Simulates a dulwich reset --hard absent-file window for prune_orphan_rows.

    A Lance row whose backing file appears absent on the FIRST is_file() call
    must NOT be deleted if the file is present after the git-worktree lock is
    acquired (transient absence during a reset).

    Without the lock+recheck in prune_orphan_rows (the fix for sub-hazard B),
    the row is deleted on the first absent observation — this test fails.
    """
    lancedb = pytest.importorskip("lancedb")
    from durin.memory.vector_index import _INDEX_PATH, _TABLE_NAME, prune_orphan_rows

    workspace = tmp_path
    mem = workspace / "memory"
    entities = mem / "entities" / "person"
    entities.mkdir(parents=True, exist_ok=True)

    md_path = entities / "carol.md"
    md_path.write_text("# Carol\n", encoding="utf-8")
    rel_path = md_path.relative_to(workspace)

    # Insert a minimal Lance row directly (avoid embedding model dependency).
    # The schema that prune_orphan_rows cares about: "id" and "path".
    lance_uri = str(workspace.joinpath(*_INDEX_PATH))
    Path(lance_uri).mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(lance_uri)
    row = {
        "id": "person:carol",
        "path": str(rel_path),
        "vector": [0.0, 0.0, 0.0, 0.0],
    }
    db.create_table(_TABLE_NAME, data=[row])

    # Confirm the row is there before the test.
    t = db.open_table(_TABLE_NAME)
    assert t.count_rows() == 1, "setup: Lance row must exist before test"

    # Simulate transient absence: first is_file() (initial scan) → False,
    # subsequent calls (recheck under the lock) → True.
    call_count = {"n": 0}
    real_is_file = Path.is_file

    def _patched_is_file(self: Path) -> bool:
        if self == workspace / rel_path:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return False   # first scan: simulate mid-reset absence
            return True        # recheck (under lock): reset completed, file is back
        return real_is_file(self)

    with patch.object(Path, "is_file", _patched_is_file):
        pruned = prune_orphan_rows(workspace)

    # Row must NOT have been pruned (transient absence).
    assert "person:carol" not in pruned, (
        "prune_orphan_rows deleted the vector row on a TRANSIENT absent-file "
        "observation — the lock+recheck (sub-hazard B fix) is missing or broken"
    )
    assert call_count["n"] >= 2, (
        "is_file was called fewer than 2 times — the recheck path was not taken"
    )
