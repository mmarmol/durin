"""approval_store: durable approval records with a CAS lifecycle."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from durin.agent import approval_store as st


def _mk(ws, **kw):
    base = dict(kind="skill_edit", summary="edit skill 'a'", detail={"diff": "-x\n+y"},
                payload={"name": "a", "file": "SKILL.md", "old": "x", "new": "y"},
                change_hash="h1", session_key="websocket:s1", context="interactive")
    base.update(kw)
    return st.create(ws, **base)


def test_create_and_get_roundtrip(tmp_path):
    rec = _mk(tmp_path)
    assert rec["status"] == "pending"
    assert len(rec["id"]) == 12
    assert (tmp_path / ".approvals" / f"{rec['id']}.json").is_file()
    got = st.get(tmp_path, rec["id"])
    assert got["payload"]["new"] == "y"
    assert got["expires_at"] > got["requested_at"]


def test_unknown_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _mk(tmp_path, kind="nope")


def test_transition_is_compare_and_set(tmp_path):
    rec = _mk(tmp_path)
    first = st.transition(tmp_path, rec["id"], expect=("pending",), to="approved",
                          decided_by={"kind": "user"})
    assert first["status"] == "approved"
    # A second decision from 'pending' loses: the record already moved on.
    assert st.transition(tmp_path, rec["id"], expect=("pending",), to="rejected") is None
    assert st.get(tmp_path, rec["id"])["status"] == "approved"


def test_find_pending_dedupes_on_session_kind_and_hash(tmp_path):
    rec = _mk(tmp_path)
    assert st.find_pending(tmp_path, session_key="websocket:s1", kind="skill_edit",
                           change_hash="h1")["id"] == rec["id"]
    assert st.find_pending(tmp_path, session_key="websocket:s1", kind="skill_edit",
                           change_hash="other") is None


def test_list_includes_legacy_records_marked_legacy(tmp_path):
    legacy_dir = tmp_path / ".approvals" / "skills"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "abc123abc123.json").write_text(json.dumps({
        "id": "abc123abc123", "subsystem": "skills", "action": "edit",
        "summary": "old style", "detail": {}, "session_key": "cron:x",
        "requested_at": "2026-09-01T00:00:00+00:00", "status": "pending"}))
    _mk(tmp_path)
    recs = st.list_records(tmp_path)
    legacy = [r for r in recs if r.get("legacy")]
    assert len(legacy) == 1 and legacy[0]["id"] == "abc123abc123"
    assert st.discard(tmp_path, "abc123abc123") is True
    assert not (legacy_dir / "abc123abc123.json").exists()


def test_expire_and_prune(tmp_path):
    old_pending = _mk(tmp_path)
    done = _mk(tmp_path, change_hash="h2")
    st.transition(tmp_path, done["id"], expect=("pending",), to="rejected",
                  decided_by={"kind": "user"})
    later = datetime.now(timezone.utc) + timedelta(days=15)
    counts = st.expire_and_prune(tmp_path, now=later)
    assert counts["expired"] == 1
    assert st.get(tmp_path, old_pending["id"])["status"] == "expired"
    much_later = datetime.now(timezone.utc) + timedelta(days=31)
    counts = st.expire_and_prune(tmp_path, now=much_later)
    # The rejected record is 31 days past its decision; the expired one only
    # 16 days past its expiry, so it is kept.
    assert counts["pruned"] == 1
    assert st.get(tmp_path, done["id"]) is None
    assert st.get(tmp_path, old_pending["id"])["status"] == "expired"


def test_retention_of_an_expired_record_counts_from_its_expiry(tmp_path):
    rec = _mk(tmp_path)
    expired_at = datetime.now(timezone.utc) + timedelta(days=15)
    assert st.expire_and_prune(tmp_path, now=expired_at)["expired"] == 1
    assert st.get(tmp_path, rec["id"])["decided_at"] == expired_at.isoformat()

    st.expire_and_prune(tmp_path, now=expired_at + timedelta(days=29))
    assert st.get(tmp_path, rec["id"])["status"] == "expired"
    assert st.expire_and_prune(tmp_path, now=expired_at + timedelta(days=30))["pruned"] == 1
    assert st.get(tmp_path, rec["id"]) is None


def test_any_expiry_transition_stamps_decided_at(tmp_path):
    """The turn closing an unanswered exec request, and ``decide`` refusing a
    request past its TTL, expire through ``transition`` too."""
    rec = _mk(tmp_path)
    before = datetime.now(timezone.utc)
    st.transition(tmp_path, rec["id"], expect=("pending",), to="expired")
    stamped = st.get(tmp_path, rec["id"])["decided_at"]
    assert stamped is not None and datetime.fromisoformat(stamped) >= before


def test_a_record_stuck_approved_past_the_bound_ends_failed_interrupted(tmp_path):
    """A hard kill mid-run leaves a record ``approved`` with nothing left to
    finish it; past the bound it is closed as interrupted."""
    rec = _mk(tmp_path)
    approved = st.transition(tmp_path, rec["id"], expect=("pending",), to="approved",
                             decided_by={"kind": "user"})
    decided_at = datetime.fromisoformat(approved["decided_at"])

    still_running = st.expire_and_prune(
        tmp_path, now=decided_at + st.APPROVED_RUN_BOUND - timedelta(minutes=1))
    assert still_running["interrupted"] == 0
    assert st.get(tmp_path, rec["id"])["status"] == "approved"

    counts = st.expire_and_prune(tmp_path, now=decided_at + st.APPROVED_RUN_BOUND)
    assert counts["interrupted"] == 1
    stuck = st.get(tmp_path, rec["id"])
    assert stuck["status"] == "failed" and stuck["result"] == {"error": "interrupted"}
    assert stuck["decided_by"] == {"kind": "user"}


def test_closing_a_stuck_record_loses_to_a_run_that_just_finished(tmp_path, monkeypatch):
    """Compare-and-set: if the run lands between the scan and the close, the
    applied record stays applied."""
    rec = _mk(tmp_path)
    st.transition(tmp_path, rec["id"], expect=("pending",), to="approved",
                  decided_by={"kind": "user"})
    real_transition = st.transition

    def _finish_first(ws, approval_id, **kw):
        if kw.get("to") == "failed":
            real_transition(ws, approval_id, expect=("approved",), to="applied",
                            result={"ok": True})
        return real_transition(ws, approval_id, **kw)

    monkeypatch.setattr(st, "transition", _finish_first)
    counts = st.expire_and_prune(
        tmp_path, now=datetime.now(timezone.utc) + st.APPROVED_RUN_BOUND + timedelta(minutes=1))
    assert counts["interrupted"] == 0
    assert st.get(tmp_path, rec["id"])["status"] == "applied"
