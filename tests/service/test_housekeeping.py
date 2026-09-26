"""The gateway's workspace janitor.

Approval records and automation claims age out on their own schedule, and
nothing else revisits them while the gateway runs: a boot-only sweep leaves a
long-running gateway with pending requests past their TTL and claims nobody
released. The janitor repeats the boot sweep every interval, and stops with
the gateway without holding its shutdown open.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from durin.agent import approval_store as st
from durin.automations import claims
from durin.service import housekeeping
from durin.service.housekeeping import (
    HOUSEKEEPING_INTERVAL_S,
    WorkspaceJanitor,
    sweep_workspace,
)


def _pending(ws, *, expired: bool = False) -> dict:
    rec = st.create(ws, kind="skill_edit", summary="edit skill 'a'", detail={}, payload={},
                    change_hash="h", session_key="cron:x", context="autonomous")
    if expired:
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        st.transition(ws, rec["id"], expect=("pending",), to="pending", expires_at=past)
    return rec


def _old_resolved(ws) -> dict:
    rec = _pending(ws)
    st.transition(ws, rec["id"], expect=("pending",), to="rejected",
                  decided_by={"kind": "operator", "channel": "cli"})
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    st.transition(ws, rec["id"], expect=("rejected",), to="rejected", decided_at=old)
    return rec


def _claim(ws, key: str, *, age_s: float = 0.0) -> None:
    claims.register(ws, key=key, automation="a1", run_id="run1")
    if age_s:
        data = claims._load_claims(ws)
        data[key]["registered_at"] = time.time() - age_s
        claims.claims_path(ws).write_text(json.dumps(data))


def test_the_interval_is_hourly():
    assert HOUSEKEEPING_INTERVAL_S == 3600.0


def test_a_sweep_expires_and_prunes_approvals_and_prunes_stale_claims(tmp_path):
    expired = _pending(tmp_path, expired=True)
    old = _old_resolved(tmp_path)
    fresh = _pending(tmp_path)
    _claim(tmp_path, "stale", age_s=8 * 24 * 3600)
    _claim(tmp_path, "fresh")

    counts = sweep_workspace(tmp_path)

    assert st.get(tmp_path, expired["id"])["status"] == "expired"
    assert st.get(tmp_path, old["id"]) is None
    assert st.get(tmp_path, fresh["id"])["status"] == "pending"
    assert claims.lookup(tmp_path, "stale") is None
    assert claims.lookup(tmp_path, "fresh") is not None
    assert counts == {"approvals": {"expired": 1, "interrupted": 0, "pruned": 1},
                      "claims_released": 1}


def test_a_failing_store_does_not_stop_the_other(tmp_path, monkeypatch):
    def _broken(_ws):
        raise OSError("approvals unreadable")

    monkeypatch.setattr(housekeeping.approval_store, "expire_and_prune", _broken)
    _claim(tmp_path, "stale", age_s=8 * 24 * 3600)

    counts = sweep_workspace(tmp_path)

    assert claims.lookup(tmp_path, "stale") is None
    assert counts == {"claims_released": 1}


@pytest.mark.asyncio
async def test_the_janitor_sweeps_each_interval_while_the_gateway_runs(tmp_path):
    _claim(tmp_path, "stale", age_s=8 * 24 * 3600)
    expired = _pending(tmp_path, expired=True)
    janitor = WorkspaceJanitor(lambda: tmp_path, interval_s=0.01)

    janitor.start()
    try:
        async with asyncio.timeout(5):
            while claims.lookup(tmp_path, "stale") is not None:
                await asyncio.sleep(0.01)
    finally:
        await janitor.stop()

    assert st.get(tmp_path, expired["id"])["status"] == "expired"


@pytest.mark.asyncio
async def test_the_janitor_stops_on_shutdown(tmp_path, monkeypatch):
    sweeps: list[object] = []
    monkeypatch.setattr(housekeeping, "sweep_workspace", lambda ws: sweeps.append(ws))
    janitor = WorkspaceJanitor(lambda: tmp_path, interval_s=0.01)

    janitor.start()
    async with asyncio.timeout(5):
        while not sweeps:
            await asyncio.sleep(0.01)
    await janitor.stop()
    seen = len(sweeps)
    await asyncio.sleep(0.05)

    assert not janitor.running
    assert len(sweeps) == seen


@pytest.mark.asyncio
async def test_stopping_does_not_wait_out_a_stuck_sweep(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def _stuck(_ws):
        entered.set()
        release.wait(10)

    monkeypatch.setattr(housekeeping, "sweep_workspace", _stuck)
    janitor = WorkspaceJanitor(lambda: tmp_path, interval_s=0.01)
    janitor.start()
    async with asyncio.timeout(5):
        while not entered.is_set():
            await asyncio.sleep(0.01)

    started = time.monotonic()
    await janitor.stop()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 1.0
    assert not janitor.running


@pytest.mark.asyncio
async def test_starting_twice_runs_one_janitor(tmp_path, monkeypatch):
    sweeps: list[object] = []
    monkeypatch.setattr(housekeeping, "sweep_workspace", lambda ws: sweeps.append(ws))
    janitor = WorkspaceJanitor(lambda: tmp_path, interval_s=0.05)

    janitor.start()
    first = janitor._task
    janitor.start()

    assert janitor._task is first
    await janitor.stop()
