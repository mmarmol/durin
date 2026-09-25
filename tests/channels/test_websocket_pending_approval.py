"""A pending approval reaches the webui and is answered there by the server.

The card is drawn from the session's goal-state snapshot, which is replayed
when a client attaches. After a crash, the saved metadata can outlive its
waiter, so attach only replays the card while the approval record is still
pending in the approval store; a decided or expired record is dropped
instead of showing a phantom card that a click could no longer resolve.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent import approval_store
from durin.channels.websocket import WebSocketChannel
from durin.session.manager import SessionManager


def _seed_pending_approval(tmp_path, sm, chat_id, *, status="pending"):
    """Create a real approval record and mirror it into session metadata,
    the way ``approval_prompt.make_chat_asker`` does while a gated tool waits."""
    record = approval_store.create(
        tmp_path, kind="exec_command", summary="run `make clean`",
        detail={"command": "make clean"}, payload={"command": "make clean"},
        change_hash="h", session_key=f"websocket:{chat_id}", context="chat",
    )
    if status != "pending":
        approval_store.transition(
            tmp_path, record["id"], expect=("pending",), to=status,
            decided_by={"kind": "user", "channel": f"websocket:{chat_id}"},
        )
    session = sm.get_or_create(f"websocket:{chat_id}")
    session.metadata["pending_approval"] = {
        "approval_id": record["id"], "kind": record["kind"],
        "summary": record["summary"], "detail": record["detail"],
    }
    sm.save(session)
    return record["id"]


@pytest.mark.asyncio
async def test_attach_replays_a_pending_approval_without_an_active_goal(tmp_path):
    sm = SessionManager(tmp_path)
    approval_id = _seed_pending_approval(tmp_path, sm, "c2")
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"]}, MagicMock(), session_manager=sm)
    ws = AsyncMock()

    await channel._dispatch_envelope(ws, "client-1", {"type": "attach", "chat_id": "c2"})

    frames = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
    goal = [f for f in frames if f.get("event") == "goal_state"]
    assert len(goal) == 1
    assert goal[0]["goal_state"]["pending_approval"]["approval_id"] == approval_id


@pytest.mark.asyncio
async def test_attach_drops_a_decided_approval_and_does_not_replay_it(tmp_path):
    sm = SessionManager(tmp_path)
    _seed_pending_approval(tmp_path, sm, "c3", status="applied")
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"]}, MagicMock(), session_manager=sm)
    ws = AsyncMock()

    await channel._dispatch_envelope(ws, "client-1", {"type": "attach", "chat_id": "c3"})

    frames = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
    goal = [f for f in frames if f.get("event") == "goal_state"]
    assert goal == []
    session = sm.get_or_create("websocket:c3")
    assert "pending_approval" not in session.metadata


@pytest.mark.asyncio
async def test_attach_drops_an_expired_approval_and_does_not_replay_it(tmp_path):
    sm = SessionManager(tmp_path)
    _seed_pending_approval(tmp_path, sm, "c4", status="expired")
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"]}, MagicMock(), session_manager=sm)
    ws = AsyncMock()

    await channel._dispatch_envelope(ws, "client-1", {"type": "attach", "chat_id": "c4"})

    frames = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
    goal = [f for f in frames if f.get("event") == "goal_state"]
    assert goal == []
    session = sm.get_or_create("websocket:c4")
    assert "pending_approval" not in session.metadata
