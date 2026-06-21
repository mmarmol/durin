"""Volatile per-turn state sidecar persistence.

Covers:
- ``save_runtime_state`` writes the sidecar without touching the ``.jsonl``
- Volatile keys are excluded from the ``.jsonl`` line-0 metadata
- Round-trip: a fresh ``SessionManager`` load recovers volatile keys from sidecar
- Full ``save()`` still writes volatile keys to sidecar (not line-0)

See docs/architecture/concurrency.md for the sidecar split design.
"""

from __future__ import annotations

import json
from pathlib import Path

from durin.session.manager import SessionManager
from durin.session.session_meta import meta_path_for, read_meta


def _jsonl_bytes(sm: SessionManager, key: str) -> bytes:
    return sm._get_session_path(key).read_bytes()


def _line0_metadata(sm: SessionManager, key: str) -> dict:
    path = sm._get_session_path(key)
    with path.open("r", encoding="utf-8") as f:
        return json.loads(f.readline().strip())


# ---------------------------------------------------------------------------
# save_runtime_state: sidecar-only write, jsonl untouched
# ---------------------------------------------------------------------------


def test_save_runtime_state_does_not_change_jsonl(tmp_path: Path):
    """``save_runtime_state`` must write the sidecar but leave the ``.jsonl``
    byte-for-byte identical — no rewrite of messages or metadata line."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata["agent_mode"] = "build"
    sm.save(session)

    before = _jsonl_bytes(sm, "test")

    session.metadata["runtime_checkpoint"] = {"turn": 1, "messages_before": 3}
    sm.save_runtime_state(session)

    after = _jsonl_bytes(sm, "test")
    assert before == after, "save_runtime_state must not modify the .jsonl"


def test_save_runtime_state_writes_sidecar(tmp_path: Path):
    """``save_runtime_state`` persists ``runtime_checkpoint`` into the sidecar."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    sm.save(session)

    checkpoint = {"turn": 5, "messages_before": 10}
    session.metadata["runtime_checkpoint"] = checkpoint
    sm.save_runtime_state(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"]["runtime_checkpoint"] == checkpoint


def test_save_runtime_state_writes_pending_user_turn(tmp_path: Path):
    """``save_runtime_state`` persists ``pending_user_turn`` into the sidecar."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    sm.save(session)

    session.metadata["pending_user_turn"] = True
    sm.save_runtime_state(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"]["pending_user_turn"] is True


def test_save_runtime_state_preserves_derived_keys(tmp_path: Path):
    """``save_runtime_state`` must include derived keys (e.g. _last_summary)
    in the sidecar alongside volatile keys — a sidecar-only write must not
    clobber previously persisted derived state."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata["_last_summary"] = {"text": "prior summary", "last_active": "2026-06-20"}
    sm.save(session)

    session.metadata["runtime_checkpoint"] = {"turn": 2}
    sm.save_runtime_state(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"]["_last_summary"]["text"] == "prior summary"
    assert sidecar["derived"]["runtime_checkpoint"] == {"turn": 2}


# ---------------------------------------------------------------------------
# Round-trip: volatile keys survive process restart via sidecar
# ---------------------------------------------------------------------------


def test_volatile_keys_survive_fresh_load(tmp_path: Path):
    """After ``save_runtime_state``, a fresh ``SessionManager`` (no cache)
    must recover ``runtime_checkpoint`` from the sidecar into
    ``session.metadata``."""
    sm1 = SessionManager(workspace=tmp_path)
    session = sm1.get_or_create("test")
    session.metadata["agent_mode"] = "build"
    sm1.save(session)

    checkpoint = {"turn": 3, "messages_before": 7}
    session.metadata["runtime_checkpoint"] = checkpoint
    sm1.save_runtime_state(session)

    # Simulate process restart: fresh SessionManager, empty cache.
    sm2 = SessionManager(workspace=tmp_path)
    reloaded = sm2.get_or_create("test")
    assert reloaded.metadata.get("runtime_checkpoint") == checkpoint
    assert reloaded.metadata.get("agent_mode") == "build"


def test_pending_user_turn_survives_fresh_load(tmp_path: Path):
    """``pending_user_turn`` persisted via ``save_runtime_state`` is
    readable from a fresh ``SessionManager`` load."""
    sm1 = SessionManager(workspace=tmp_path)
    session = sm1.get_or_create("ch")
    sm1.save(session)

    session.metadata["pending_user_turn"] = True
    sm1.save_runtime_state(session)

    sm2 = SessionManager(workspace=tmp_path)
    reloaded = sm2.get_or_create("ch")
    assert reloaded.metadata.get("pending_user_turn") is True


# ---------------------------------------------------------------------------
# Full save(): volatile keys go to sidecar, NOT to .jsonl line-0
# ---------------------------------------------------------------------------


def test_full_save_excludes_volatile_keys_from_line0(tmp_path: Path):
    """When ``save()`` is called with volatile keys in session.metadata,
    those keys must NOT appear in the .jsonl line-0 metadata block."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata["agent_mode"] = "build"
    session.metadata["runtime_checkpoint"] = {"turn": 1}
    session.metadata["pending_user_turn"] = True
    sm.save(session)

    line0 = _line0_metadata(sm, "test")
    assert "runtime_checkpoint" not in line0["metadata"]
    assert "pending_user_turn" not in line0["metadata"]
    assert line0["metadata"]["agent_mode"] == "build"


def test_full_save_writes_volatile_keys_to_sidecar(tmp_path: Path):
    """``save()`` must persist volatile keys to the sidecar derived block."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    checkpoint = {"turn": 9, "messages_before": 20}
    session.metadata["runtime_checkpoint"] = checkpoint
    sm.save(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert sidecar["derived"]["runtime_checkpoint"] == checkpoint


def test_full_save_volatile_keys_round_trip(tmp_path: Path):
    """Full save + reload sees volatile keys merged back into session.metadata."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata["agent_mode"] = "plan"
    session.metadata["runtime_checkpoint"] = {"turn": 4}
    sm.save(session)

    sm.invalidate("test")
    reloaded = sm.get_or_create("test")
    assert reloaded.metadata.get("runtime_checkpoint") == {"turn": 4}
    assert reloaded.metadata.get("agent_mode") == "plan"


# ---------------------------------------------------------------------------
# Clearing volatile keys
# ---------------------------------------------------------------------------


def test_clear_volatile_key_propagates_to_sidecar_on_save(tmp_path: Path):
    """When a volatile key is removed from session.metadata and ``save()``
    is called, the sidecar should no longer carry that key."""
    sm = SessionManager(workspace=tmp_path)
    session = sm.get_or_create("test")
    session.metadata["runtime_checkpoint"] = {"turn": 1}
    sm.save(session)

    # Verify it was written.
    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert "runtime_checkpoint" in sidecar["derived"]

    # Clear and re-save.
    session.metadata.pop("runtime_checkpoint", None)
    sm.save(session)

    sidecar = read_meta(meta_path_for("test", sm.sessions_dir))
    assert "runtime_checkpoint" not in sidecar["derived"]
