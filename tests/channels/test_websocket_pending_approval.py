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


def _seed_pending_approval(tmp_path, sm, chat_id, *, status="pending", session_key=None):
    """Create a real approval record and mirror it into session metadata,
    the way ``approval_prompt.make_chat_asker`` does while a gated tool waits.

    *session_key* is the session the metadata is written to — normally
    "websocket:<chat_id>", but the loop's unified-session key
    ("unified:default") when agents.defaults.unified_session is on.
    """
    key = session_key or f"websocket:{chat_id}"
    record = approval_store.create(
        tmp_path, kind="exec_command", summary="run `make clean`",
        detail={"command": "make clean"}, payload={"command": "make clean"},
        change_hash="h", session_key=key, context="chat",
    )
    if status != "pending":
        approval_store.transition(
            tmp_path, record["id"], expect=("pending",), to=status,
            decided_by={"kind": "user", "channel": key},
        )
    session = sm.get_or_create(key)
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


@pytest.mark.asyncio
async def test_attach_replays_a_pending_approval_stored_under_the_unified_session_key(tmp_path):
    """With agents.defaults.unified_session on, AgentLoop.bus_turn_key folds
    every channel's conversation into "unified:default" regardless of chat_id,
    and that is where the asker actually saved pending_approval. The gateway
    hands the channel that same function (session_turn_key); attach must
    replay from the unified key, not from the "websocket:<chat_id>" session,
    which unified mode never writes to."""
    sm = SessionManager(tmp_path)
    approval_id = _seed_pending_approval(tmp_path, sm, "c5", session_key="unified:default")
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"]}, MagicMock(), session_manager=sm,
        session_turn_key=lambda _key: "unified:default",
    )
    ws = AsyncMock()

    await channel._dispatch_envelope(ws, "client-1", {"type": "attach", "chat_id": "c5"})

    frames = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
    goal = [f for f in frames if f.get("event") == "goal_state"]
    assert len(goal) == 1
    assert goal[0]["goal_state"]["pending_approval"]["approval_id"] == approval_id


@pytest.mark.asyncio
async def test_attach_reads_the_per_chat_session_when_unified_session_is_off(tmp_path):
    """session_turn_key is always handed to the channel by the gateway; with
    unified_session off it is AgentLoop.bus_turn_key returning its argument
    unchanged. Attach must still read the per-chat "websocket:<chat_id>"
    session — this task's wiring must not hardcode the unified key."""
    sm = SessionManager(tmp_path)
    approval_id = _seed_pending_approval(tmp_path, sm, "c6")
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"]}, MagicMock(), session_manager=sm,
        session_turn_key=lambda key: key,
    )
    ws = AsyncMock()

    await channel._dispatch_envelope(ws, "client-1", {"type": "attach", "chat_id": "c6"})

    frames = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
    goal = [f for f in frames if f.get("event") == "goal_state"]
    assert len(goal) == 1
    assert goal[0]["goal_state"]["pending_approval"]["approval_id"] == approval_id
