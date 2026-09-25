"""A pending approval reaches the webui and is answered there by the server.

The card is drawn from the session's goal-state snapshot, which is replayed
when a client attaches. After a crash, the saved metadata can outlive its
waiter, so attach only replays the card while the approval record is still
pending in the approval store; a decided or expired record is dropped
instead of showing a phantom card that a click could no longer resolve.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent import approval_executors as ex
from durin.agent import approval_store, pending_answers
from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.channels.websocket import WebSocketChannel
from durin.providers.base import GenerationSettings, LLMResponse
from durin.session.manager import SessionManager


def _bus_turn_key(tmp_path_factory, *, unified: bool):
    """A real AgentLoop.bus_turn_key, so the tests below exercise the actual
    gateway function (what the loop hands the channel as session_turn_key)
    instead of a hand-rolled stand-in."""
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    response = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=response)
    provider.chat_stream_with_retry = AsyncMock(return_value=response)
    loop = AgentLoop(
        bus=MessageBus(), provider=provider,
        workspace=tmp_path_factory.mktemp("loop-workspace"),
        model="test-model", unified_session=unified,
    )
    return loop.bus_turn_key


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
async def test_attach_replays_a_pending_approval_stored_under_the_unified_session_key(
    tmp_path, tmp_path_factory,
):
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
        session_turn_key=_bus_turn_key(tmp_path_factory, unified=True),
    )
    ws = AsyncMock()

    await channel._dispatch_envelope(ws, "client-1", {"type": "attach", "chat_id": "c5"})

    frames = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
    goal = [f for f in frames if f.get("event") == "goal_state"]
    assert len(goal) == 1
    assert goal[0]["goal_state"]["pending_approval"]["approval_id"] == approval_id


@pytest.mark.asyncio
async def test_attach_reads_the_per_chat_session_when_unified_session_is_off(
    tmp_path, tmp_path_factory,
):
    """session_turn_key is always handed to the channel by the gateway; with
    unified_session off it is AgentLoop.bus_turn_key returning its argument
    unchanged. Attach must still read the per-chat "websocket:<chat_id>"
    session — this task's wiring must not hardcode the unified key."""
    sm = SessionManager(tmp_path)
    approval_id = _seed_pending_approval(tmp_path, sm, "c6")
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"]}, MagicMock(), session_manager=sm,
        session_turn_key=_bus_turn_key(tmp_path_factory, unified=False),
    )
    ws = AsyncMock()

    await channel._dispatch_envelope(ws, "client-1", {"type": "attach", "chat_id": "c6"})

    frames = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
    goal = [f for f in frames if f.get("event") == "goal_state"]
    assert len(goal) == 1
    assert goal[0]["goal_state"]["pending_approval"]["approval_id"] == approval_id


@pytest.mark.asyncio
async def test_attach_drops_an_expired_approval_under_the_unified_session_key(
    tmp_path, tmp_path_factory,
):
    """The write side of the drop guard (test_attach_drops_an_expired_approval_
    and_does_not_replay_it) under the unified key: popping the stale
    pending_approval back out of the *saved* session must also target the
    loop's effective key, not "websocket:<chat_id>", which unified mode
    never wrote to in the first place — otherwise the pop would silently
    write a fresh, empty "websocket:<chat_id>" session while leaving the
    real, unified one stale."""
    sm = SessionManager(tmp_path)
    _seed_pending_approval(tmp_path, sm, "c7", status="expired", session_key="unified:default")
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"]}, MagicMock(), session_manager=sm,
        session_turn_key=_bus_turn_key(tmp_path_factory, unified=True),
    )
    ws = AsyncMock()

    await channel._dispatch_envelope(ws, "client-1", {"type": "attach", "chat_id": "c7"})

    frames = [json.loads(call.args[0]) for call in ws.send_text.await_args_list]
    goal = [f for f in frames if f.get("event") == "goal_state"]
    assert goal == []
    session = sm.get_or_create("unified:default")
    assert "pending_approval" not in session.metadata


@pytest.fixture(autouse=True)
def _reset_waiters():
    pending_answers.reset()
    yield
    pending_answers.reset()


def _channel(tmp_path, **kwargs):
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"]}, MagicMock(),
        session_manager=SessionManager(tmp_path), **kwargs,
    )
    ws = AsyncMock()
    channel._attach(ws, "c1")
    return channel, ws


def _record(tmp_path, session_key="websocket:c1", kind="exec_command"):
    return approval_store.create(
        tmp_path, kind=kind, summary="run `make clean`",
        detail={"command": "make clean"},
        payload={"command": "make clean", "cwd": str(tmp_path)},
        change_hash="h1", session_key=session_key, context="interactive",
    )


async def _decide(channel, ws, **fields):
    """Send one approval_decision frame and return the approval_decided reply."""
    await channel._dispatch_envelope(
        ws, "client-1", {"type": "approval_decision", "request_id": "r1", **fields})
    await asyncio.gather(*list(channel._approval_tasks))
    return json.loads(ws.send_text.await_args.args[0])


@pytest.mark.asyncio
async def test_click_hands_the_verdict_to_the_waiting_turn(tmp_path):
    channel, ws = _channel(tmp_path)
    rec = _record(tmp_path)
    fut = pending_answers.create("websocket:c1", kind="approval", ref=rec["id"])

    reply = await _decide(channel, ws, approval_id=rec["id"], decision="approve")

    assert await fut == "approve"
    assert reply["event"] == "approval_decided" and reply["request_id"] == "r1"
    assert reply["ok"] is True and reply["status"] == "pending"
    # The waiting turn runs it; the socket handler did not.
    assert approval_store.get(tmp_path, rec["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_click_after_the_turn_stopped_waiting_runs_with_the_gateway_handles(
    tmp_path, monkeypatch,
):
    """An out-of-turn click executes with the gateway's live handles for
    every kind but ``exec_command``, which never runs out of turn (see the
    dedicated test below); ``mcp_change`` stands in for them here."""
    seen: list = []

    async def _execute(workspace, payload, deps):
        seen.append(deps)
        return {"ok": True}

    monkeypatch.setitem(
        ex._REGISTRY, "mcp_change", (lambda workspace, payload: "h1", _execute))
    gateway_deps = ex.ExecDeps(extra={"from": "gateway"})
    channel, ws = _channel(tmp_path, approval_deps=lambda: gateway_deps)
    rec = _record(tmp_path, kind="mcp_change")

    reply = await _decide(channel, ws, approval_id=rec["id"], decision="approve")

    assert reply["ok"] is True and reply["status"] == "applied"
    assert seen == [gateway_deps]
    stored = approval_store.get(tmp_path, rec["id"])
    assert stored["status"] == "applied"
    assert stored["decided_by"] == {"kind": "user", "channel": "websocket"}


@pytest.mark.asyncio
async def test_click_after_the_turn_stopped_waiting_on_exec_command_is_refused(
    tmp_path,
):
    """An ``exec_command`` approval runs only inside the chat turn that asked
    for it. A click that lands after that turn stopped waiting must not hand
    the request the gateway's live ``exec_run`` — even though the gateway's
    own ExecDeps carries one for other, in-turn uses. It is refused with the
    same answer the CLI gets, nothing runs, and the record stays pending
    until the turn that asked closes it."""
    from durin.agent.approval_kinds_exec import exec_hash

    calls: list = []

    async def run(**kw):
        calls.append(kw)
        return "should never happen"

    gateway_deps = ex.ExecDeps(exec_run=run)
    channel, ws = _channel(tmp_path, approval_deps=lambda: gateway_deps)
    payload = {"command": "make clean", "cwd": str(tmp_path)}
    rec = approval_store.create(
        tmp_path, kind="exec_command", summary="run `make clean`",
        detail={"command": "make clean"}, payload=payload,
        change_hash=exec_hash("make clean", str(tmp_path), None),
        session_key="websocket:c1", context="interactive",
    )

    reply = await _decide(channel, ws, approval_id=rec["id"], decision="approve")

    assert reply["ok"] is False and reply["status"] == "refused"
    assert reply["message"] == (
        "an exec request can only be approved in the chat that asked; "
        "it closes when that turn stops waiting")
    assert calls == []
    assert approval_store.get(tmp_path, rec["id"])["status"] == "pending"


@pytest.mark.asyncio
async def test_click_on_another_chats_approval_is_refused(tmp_path):
    channel, ws = _channel(tmp_path)
    rec = _record(tmp_path, session_key="websocket:other")

    reply = await _decide(channel, ws, approval_id=rec["id"], decision="approve")

    assert reply["ok"] is False and reply["status"] == "refused"
    assert "another chat" in reply["message"]
    assert approval_store.get(tmp_path, rec["id"])["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(("fields", "reason"), [
    ({"approval_id": "../../secrets", "decision": "approve"}, "Invalid approval id"),
    ({"approval_id": "a1b2c3d4e5f6", "decision": "maybe"}, "Invalid decision"),
    ({"approval_id": "a1b2c3d4e5f6", "decision": "approve"}, "No approval request"),
])
async def test_malformed_or_unknown_decision_is_refused(tmp_path, fields, reason):
    channel, ws = _channel(tmp_path)
    reply = await _decide(channel, ws, **fields)
    assert reply["ok"] is False and reply["status"] == "refused"
    assert reason in reply["message"]


@pytest.mark.asyncio
async def test_unified_mode_matches_the_shared_session_key(tmp_path):
    channel, ws = _channel(tmp_path, session_turn_key=lambda key: "unified:default")
    rec = _record(tmp_path, session_key="unified:default")
    fut = pending_answers.create("unified:default", kind="approval", ref=rec["id"])

    reply = await _decide(channel, ws, approval_id=rec["id"], decision="reject")

    assert reply["ok"] is True
    assert await fut == "reject"


@pytest.mark.asyncio
async def test_stop_cancels_and_awaits_a_decision_still_running(tmp_path, monkeypatch):
    """X13: a decision blocked inside its executor when the process shuts down
    must not leave the record stuck ``approved`` forever — ``stop()`` cancels
    and awaits every task in ``_approval_tasks`` before the rest of its own
    cleanup, and ``_run_approved`` (core) turns that cancellation into
    ``failed`` rather than leaving the record mid-flight."""
    entered = asyncio.Event()

    async def _execute(workspace, payload, deps):
        entered.set()
        await asyncio.sleep(10)
        return {"ok": True}  # pragma: no cover — cancelled before returning

    monkeypatch.setitem(
        ex._REGISTRY, "mcp_change", (lambda workspace, payload: "h1", _execute))
    channel, ws = _channel(tmp_path)
    await channel.start()
    rec = _record(tmp_path, kind="mcp_change")

    await channel._dispatch_envelope(
        ws, "client-1", {"type": "approval_decision", "request_id": "r1",
                        "approval_id": rec["id"], "decision": "approve"})
    await entered.wait()

    await channel.stop()

    assert channel._approval_tasks == set()
    assert approval_store.get(tmp_path, rec["id"])["status"] == "failed"
