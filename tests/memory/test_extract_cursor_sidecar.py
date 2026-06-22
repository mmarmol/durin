"""Tests for extract_cursor isolation from the .meta.json derived block.

Covers three hazards:
(A) ERASE: SessionManager.save() / save_runtime_state() must not wipe extract_cursor.
(B) RACE: set_extract_cursor and save_runtime_state may be called concurrently;
    neither the cursor nor the derived keys should be lost.
(C) BACKWARD-COMPAT: legacy files with derived.extract_cursor must still be read.
"""
from __future__ import annotations

import json
import multiprocessing
import time
from datetime import datetime
from pathlib import Path

import pytest

from durin.memory.extract_runner import get_extract_cursor, set_extract_cursor
from durin.session.manager import Session, SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(sessions_dir: Path, key: str) -> tuple[Session, Path]:
    """Create a minimal session on disk and return (session, jsonl_path)."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    safe = key.replace(":", "_")
    jsonl_path = sessions_dir / f"{safe}.jsonl"
    meta = {
        "_type": "metadata",
        "key": key,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "metadata": {},
        "last_consolidated": 0,
    }
    jsonl_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return Session(key=key), jsonl_path


# ---------------------------------------------------------------------------
# (A) ERASE — cursor must survive SessionManager.save() / save_runtime_state()
# ---------------------------------------------------------------------------


def test_cursor_survives_manager_save(tmp_path: Path) -> None:
    """set_extract_cursor then SessionManager.save() must not wipe the cursor."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("test:erase_save")
    jsonl_path = mgr._get_session_path(session.key)

    set_extract_cursor(jsonl_path, 5)
    assert get_extract_cursor(jsonl_path) == 5, "sanity: cursor set"

    mgr.save(session)

    assert get_extract_cursor(jsonl_path) == 5, "cursor erased by save()"


def test_cursor_survives_save_runtime_state(tmp_path: Path) -> None:
    """set_extract_cursor then save_runtime_state() must not wipe the cursor."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("test:erase_runtime")
    jsonl_path = mgr._get_session_path(session.key)

    set_extract_cursor(jsonl_path, 7)
    assert get_extract_cursor(jsonl_path) == 7, "sanity: cursor set"

    session.metadata["runtime_checkpoint"] = {"turn": 1, "state": "ok"}
    mgr.save_runtime_state(session)

    assert get_extract_cursor(jsonl_path) == 7, "cursor erased by save_runtime_state()"


def test_cursor_does_not_wipe_last_summary(tmp_path: Path) -> None:
    """set_extract_cursor must not wipe _last_summary that SessionManager wrote."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("test:cursor_vs_summary")
    jsonl_path = mgr._get_session_path(session.key)

    # SessionManager writes _last_summary into derived
    session.metadata["_last_summary"] = "compact summary text"
    mgr.save(session)

    # Now extract runner advances the cursor
    set_extract_cursor(jsonl_path, 3)

    # Re-load the session; _last_summary must still be there
    mgr.invalidate(session.key)
    reloaded = mgr.get_or_create(session.key)
    assert reloaded.metadata.get("_last_summary") == "compact summary text", (
        "set_extract_cursor wiped _last_summary"
    )
    assert get_extract_cursor(jsonl_path) == 3


# ---------------------------------------------------------------------------
# (B) RACE — cursor and derived keys must not be lost under concurrency
# ---------------------------------------------------------------------------
#
# Lost-update anatomy (hazard #15-B):
#
#   set_extract_cursor:  READ .meta.json → patch extract_cursor → WRITE whole file
#   save_runtime_state:  READ .meta.json → patch derived block → WRITE whole file
#
# Without a shared lock both workers can read a stale snapshot BEFORE the
# other's write lands, then both commit their own snapshot — erasing the
# other's update.  The classic example:
#
#   T1  cursor-worker reads file  (no checkpoint present)
#   T2  checkpoint-worker writes  (checkpoint now in file)
#   T3  cursor-worker writes its stale snapshot  ← erases checkpoint!
#
# The key probe: after the checkpoint worker writes, it reads the file back
# immediately.  Without the lock, a concurrent cursor-write that started
# with a stale snapshot can land BETWEEN the checkpoint's write and its
# read-back, erasing the checkpoint the read-back then misses.  We count
# every such "checkpoint just written but already gone" event in a shared
# counter; any count > 0 is a proven lost-update.  With the lock, the
# counter stays at 0 because the cursor-worker's entire RMW is serialised
# against the checkpoint-worker's RMW — neither can read a snapshot that
# the other has already committed.


def _worker_set_cursor_tight(jsonl_path: str, n_iters: int, stop: "multiprocessing.Value") -> None:  # type: ignore[type-arg]
    """Child process: set cursor in a tight loop without any sleep."""
    p = Path(jsonl_path)
    for i in range(n_iters):
        if stop.value:
            break
        set_extract_cursor(p, i + 1)


def _worker_checkpoint_and_verify(
    workspace: str,
    session_key: str,
    n_iters: int,
    clobber_count: "multiprocessing.Value",  # type: ignore[type-arg]
    stop: "multiprocessing.Value",  # type: ignore[type-arg]
) -> None:
    """Child process: write a checkpoint, then immediately verify it survived.

    Each iteration:
    1. Write runtime_checkpoint via save_runtime_state().
    2. Re-read .meta.json directly.
    3. If runtime_checkpoint is absent from ``derived``, a cursor-only write
       landed between step 1 and step 2 and clobbered it.  Increment
       clobber_count.
    """
    from durin.session.manager import SessionManager
    from durin.session.session_meta import meta_path_for, read_derived

    mgr = SessionManager(Path(workspace))
    sessions_dir = Path(workspace) / "sessions"

    for i in range(n_iters):
        if stop.value:
            break
        # Write checkpoint
        session = mgr.reload(session_key)
        sentinel = {"iteration": i}
        session.metadata["runtime_checkpoint"] = sentinel
        mgr.save_runtime_state(session)

        # Immediately read the sidecar back — without the lock a cursor
        # write that started with a pre-checkpoint snapshot can land here,
        # wiping the checkpoint we just wrote.
        derived = read_derived(meta_path_for(session_key, sessions_dir))
        if "runtime_checkpoint" not in derived:
            with clobber_count.get_lock():
                clobber_count.value += 1
            stop.value = 1  # one clobber is enough evidence; stop early


def test_concurrent_cursor_and_save_no_lost_update(tmp_path: Path) -> None:
    """Concurrent set_extract_cursor and save_runtime_state must not clobber each other.

    This test is load-bearing: it detects the actual lost-update by having the
    checkpoint worker verify its write immediately after committing it.  Without
    the cross_process_lock in set_extract_cursor the clobber counter will be > 0
    in the vast majority of runs; with the lock it stays at 0.
    """
    ctx = multiprocessing.get_context("spawn")

    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("test:race")
    mgr.save(session)
    jsonl_path = mgr._get_session_path(session.key)

    N = 200
    clobber_count = ctx.Value("i", 0)
    stop = ctx.Value("i", 0)

    p1 = ctx.Process(
        target=_worker_set_cursor_tight,
        args=(str(jsonl_path), N, stop),
    )
    p2 = ctx.Process(
        target=_worker_checkpoint_and_verify,
        args=(str(tmp_path), session.key, N, clobber_count, stop),
    )
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)
    assert p1.exitcode == 0, f"cursor worker crashed (exit {p1.exitcode})"
    assert p2.exitcode == 0, f"checkpoint worker crashed (exit {p2.exitcode})"

    assert clobber_count.value == 0, (
        f"lost-update detected: checkpoint was erased {clobber_count.value} time(s) "
        "by a concurrent cursor-only write — cross_process_lock is not protecting "
        "the .meta.json read-modify-write correctly"
    )


# ---------------------------------------------------------------------------
# (C) BACKWARD-COMPAT — legacy derived.extract_cursor must still be readable
# ---------------------------------------------------------------------------


def test_get_extract_cursor_reads_legacy_derived(tmp_path: Path) -> None:
    """A .meta.json with the old derived.extract_cursor must still be read."""
    jsonl_path = tmp_path / "session.jsonl"
    jsonl_path.write_text('{"_type":"metadata","key":"x"}\n', encoding="utf-8")
    meta_path = jsonl_path.with_suffix(".meta.json")
    legacy = {
        "session_key": "x",
        "events": [],
        "derived": {"extract_cursor": 42, "_last_summary": "old"},
    }
    meta_path.write_text(json.dumps(legacy), encoding="utf-8")

    assert get_extract_cursor(jsonl_path) == 42, (
        "backward-compat read of derived.extract_cursor failed"
    )
