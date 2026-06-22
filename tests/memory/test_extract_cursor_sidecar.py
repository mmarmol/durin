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


def _worker_set_cursor(jsonl_path: str, n_iters: int) -> None:
    """Child process: set cursor repeatedly."""
    p = Path(jsonl_path)
    for i in range(n_iters):
        set_extract_cursor(p, i + 1)
        time.sleep(0.0005)


def _worker_save_derived(workspace: str, session_key: str, n_iters: int) -> None:
    """Child process: save_runtime_state() repeatedly with a derived sentinel."""
    from durin.session.manager import SessionManager

    mgr = SessionManager(Path(workspace))
    for i in range(n_iters):
        session = mgr.reload(session_key)
        session.metadata["runtime_checkpoint"] = {"iteration": i}
        mgr.save_runtime_state(session)
        time.sleep(0.0005)


def test_concurrent_cursor_and_save_no_lost_update(tmp_path: Path) -> None:
    """Concurrent set_extract_cursor and save_runtime_state must not drop each other."""
    mgr = SessionManager(tmp_path)
    session = mgr.get_or_create("test:race")
    mgr.save(session)  # create the .jsonl so _load finds it after invalidate
    jsonl_path = mgr._get_session_path(session.key)

    N = 20
    p1 = multiprocessing.Process(
        target=_worker_set_cursor, args=(str(jsonl_path), N)
    )
    p2 = multiprocessing.Process(
        target=_worker_save_derived, args=(str(tmp_path), session.key, N)
    )
    p1.start()
    p2.start()
    p1.join(timeout=15)
    p2.join(timeout=15)
    assert p1.exitcode == 0, "cursor worker crashed"
    assert p2.exitcode == 0, "save worker crashed"

    # After both finish, the sidecar must have BOTH a cursor > 0 AND the
    # runtime_checkpoint key (derived block). Neither writer should have
    # clobbered the other's last write.
    cursor = get_extract_cursor(jsonl_path)
    assert cursor > 0, f"cursor lost after concurrent writes (got {cursor})"

    mgr.invalidate(session.key)
    reloaded = mgr.get_or_create(session.key)
    assert "runtime_checkpoint" in reloaded.metadata, (
        "runtime_checkpoint lost after concurrent writes"
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
