"""build_service_registry best-effort sweeps: pending approval records past
their TTL expire, and resolved ones past retention are pruned, at gateway
boot (durin/service/wiring.py), the same way stale automation claims are."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from durin.agent import approval_store as st
from durin.service.wiring import build_service_registry


@dataclass
class _FakeSessionManager:
    workspace: object


def _mk(ws):
    return st.create(ws, kind="skill_edit", summary="edit skill 'a'", detail={},
                     payload={}, change_hash="h", session_key="cron:x",
                     context="autonomous")


def test_expired_pending_record_is_expired_on_registry_build(tmp_path):
    rec = _mk(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    st.transition(tmp_path, rec["id"], expect=("pending",), to="pending", expires_at=past)

    build_service_registry(config=None, session_manager=_FakeSessionManager(tmp_path))

    assert st.get(tmp_path, rec["id"])["status"] == "expired"


def test_old_resolved_record_is_pruned_on_registry_build(tmp_path):
    rec = _mk(tmp_path)
    st.transition(tmp_path, rec["id"], expect=("pending",), to="rejected",
                  decided_by={"kind": "operator", "channel": "cli"})
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    st.transition(tmp_path, rec["id"], expect=("rejected",), to="rejected", decided_at=old)

    build_service_registry(config=None, session_manager=_FakeSessionManager(tmp_path))

    assert st.get(tmp_path, rec["id"]) is None


def test_fresh_pending_record_survives_registry_build(tmp_path):
    rec = _mk(tmp_path)

    build_service_registry(config=None, session_manager=_FakeSessionManager(tmp_path))

    assert st.get(tmp_path, rec["id"])["status"] == "pending"
