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
    st.transition(tmp_path, done["id"], expect=("pending",), to="rejected")
    later = datetime.now(timezone.utc) + timedelta(days=15)
    counts = st.expire_and_prune(tmp_path, now=later)
    assert counts["expired"] == 1
    assert st.get(tmp_path, old_pending["id"])["status"] == "expired"
    much_later = datetime.now(timezone.utc) + timedelta(days=31)
    counts = st.expire_and_prune(tmp_path, now=much_later)
    assert counts["pruned"] == 2
    assert st.get(tmp_path, done["id"]) is None
