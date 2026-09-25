"""A turn waiting on a person gives its lane and ceiling slots back, and
takes them again before it continues; no cancellation can over-release or
leak a slot."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from durin.agent import pending_answers as pa
from durin.agent import turn_slots
from durin.agent.approval_prompt import make_chat_asker
from durin.agent.loop import AgentLoop
from durin.agent.tools.ask_user import AskUserQuestionTool
from durin.agent.tools.context import RequestContext
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.utils.resizable_semaphore import ResizableSemaphore

A = "websocket:a"
B = "websocket:b"


@pytest.fixture(autouse=True)
def _consumer():
    pa.reset()
    pa.set_consumer_active(True)
    yield
    pa.reset()


def _loop(tmp_path: Path, monkeypatch, *, lanes: int = 1) -> AgentLoop:
    # Built with the cap rather than resized after: shrinking a live gate
    # withholds permits as holders exit, which would blur the counters below.
    monkeypatch.setenv("DURIN_MAX_CONCURRENT_REQUESTS", str(lanes))
    provider = MagicMock()
    provider.get_default_model.return_value = "m"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="m")
    assert loop._interactive_lane.limit == lanes
    return loop


def _msg(session_key: str, text: str = "hi") -> InboundMessage:
    channel, chat_id = session_key.split(":", 1)
    return InboundMessage(channel=channel, sender_id="u", chat_id=chat_id, content=text)


def _free(gate: ResizableSemaphore) -> int:
    """Permits the gate can hand out right now (the semaphore's own counter)."""
    return gate._sem._value


def _assert_at_rest(loop: AgentLoop, lanes: int) -> None:
    """Every slot is back, and not one more than the cap: an over-release
    would leave more free permits than the limit, a leak fewer."""
    assert loop._interactive_lane.active == 0 and _free(loop._interactive_lane) == lanes
    assert loop._ceiling.active == 0
    assert _free(loop._ceiling) == loop._ceiling.limit


async def _approval_wait(loop: AgentLoop, session_key: str) -> str | None:
    """What a gated tool does in a chat turn: put the request to the person
    through the real in-chat asker, and wait."""
    channel, chat_id = session_key.split(":", 1)
    ask = make_chat_asker(
        sessions=loop.sessions, bus=loop.bus, timeout_s=30,
        request_ctx=RequestContext(channel=channel, chat_id=chat_id, session_key=session_key))
    assert ask is not None
    return await ask({"id": "r-" + chat_id, "kind": "exec_command",
                      "summary": "run `rm -rf build`", "detail": {}})


async def _question_wait(loop: AgentLoop, session_key: str) -> str | None:
    """A blocking ask_user in a chat turn."""
    channel, chat_id = session_key.split(":", 1)
    tool = AskUserQuestionTool(sessions=loop.sessions, bus=loop.bus, answer_timeout_s=30)
    tool.set_context(RequestContext(channel=channel, chat_id=chat_id, session_key=session_key))
    return await tool.execute(question="Which colour?")


async def _until(predicate, what: str) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


def _turns(loop: AgentLoop, wait, *, b_gate: asyncio.Event | None = None):
    """Replace the turn body: session A waits on a person, B runs to its end
    (after *b_gate*, when given). Returns what each turn saw."""
    seen: dict[str, object] = {}

    async def _turn(msg, **_kw):
        if msg.session_key == A:
            seen["a_answer"] = await wait(loop, A)
            seen["a_lane_after_wait"] = loop._interactive_lane.active
        else:
            seen["b_ran"] = True
            if b_gate is not None:
                await b_gate.wait()
        return None

    loop._process_message = _turn  # type: ignore[method-assign]
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [_approval_wait, _question_wait], ids=["approval", "question"])
async def test_another_chat_runs_while_a_turn_waits_on_a_person(tmp_path, monkeypatch, wait):
    loop = _loop(tmp_path, monkeypatch, lanes=1)
    seen = _turns(loop, wait)

    a = asyncio.create_task(loop._dispatch(_msg(A)))
    await _until(lambda: pa.is_waiting(A), "turn A to wait")
    # A waits with its slots given back: the only lane is free.
    assert loop._interactive_lane.active == 0 and _free(loop._interactive_lane) == 1

    await asyncio.wait_for(loop._dispatch(_msg(B)), 5)
    assert seen.get("b_ran") is True
    assert not a.done()

    assert pa.resolve(A, "approve")
    await asyncio.wait_for(a, 5)
    assert seen["a_answer"] is not None
    # A continued holding its lane again.
    assert seen["a_lane_after_wait"] == 1
    _assert_at_rest(loop, lanes=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [_approval_wait, _question_wait], ids=["approval", "question"])
async def test_cancelling_a_turn_during_its_wait_leaks_no_slot(tmp_path, monkeypatch, wait):
    loop = _loop(tmp_path, monkeypatch, lanes=1)
    _turns(loop, wait)

    a = asyncio.create_task(loop._dispatch(_msg(A)))
    await _until(lambda: pa.is_waiting(A), "turn A to wait")
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    _assert_at_rest(loop, lanes=1)
    # The lane really is usable: another turn gets it.
    await asyncio.wait_for(loop._dispatch(_msg(B)), 5)
    _assert_at_rest(loop, lanes=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait", [_approval_wait, _question_wait], ids=["approval", "question"])
async def test_cancelling_a_turn_queued_to_take_its_slot_back_leaks_no_slot(tmp_path, monkeypatch, wait):
    loop = _loop(tmp_path, monkeypatch, lanes=1)
    b_gate = asyncio.Event()
    _turns(loop, wait, b_gate=b_gate)

    a = asyncio.create_task(loop._dispatch(_msg(A)))
    await _until(lambda: pa.is_waiting(A), "turn A to wait")
    b = asyncio.create_task(loop._dispatch(_msg(B)))
    await _until(lambda: loop._interactive_lane.active == 1, "turn B to take the lane")

    # Answered while B holds the only lane: A queues to take it back.
    assert pa.resolve(A, "approve")
    await _until(lambda: loop._interactive_lane.waiting == 1, "turn A to queue for the lane")
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    assert loop._interactive_lane.waiting == 0

    b_gate.set()
    await asyncio.wait_for(b, 5)
    _assert_at_rest(loop, lanes=1)


@pytest.mark.asyncio
async def test_cancelled_while_queued_for_the_ceiling_gives_the_lane_back():
    """Taking the slots is two steps; a cancellation between them must not
    keep the first."""
    lane = ResizableSemaphore(2, name="interactive")
    ceiling = ResizableSemaphore(1, name="ceiling")
    other = turn_slots.TurnSlots(lane, ceiling, session_key=B)
    await other.__aenter__()

    slots = turn_slots.TurnSlots(lane, ceiling, session_key=A)
    entering = asyncio.create_task(slots.__aenter__())
    await _until(lambda: ceiling.waiting == 1, "the ceiling queue")
    assert slots.held == (True, False)
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering
    assert slots.held == (False, False)
    assert lane.active == 1 and _free(lane) == 1

    await other.__aexit__(None, None, None)
    assert lane.active == 0 and _free(lane) == 2
    assert ceiling.active == 0 and _free(ceiling) == 1


@pytest.mark.asyncio
async def test_outside_a_dispatched_turn_the_wait_changes_nothing():
    """The CLI and tests run no dispatched turn: nothing is bound."""
    async with turn_slots.released_while_waiting(A):
        pass


@pytest.mark.asyncio
async def test_a_wait_for_another_session_leaves_the_turn_s_slots_alone():
    """A sub-agent (or a background run) started by the turn copies its
    context, so it sees the turn's slots; a wait under its own session key
    must not give them away."""
    lane = ResizableSemaphore(1, name="interactive")
    ceiling = ResizableSemaphore(1, name="ceiling")
    slots = turn_slots.TurnSlots(lane, ceiling, session_key=A)
    token = turn_slots.bind(slots)
    try:
        async with slots:
            async with turn_slots.released_while_waiting("subagent:x"):
                assert slots.held == (True, True) and lane.active == 1
            async with turn_slots.released_while_waiting(A):
                assert slots.held == (False, False) and lane.active == 0
            assert slots.held == (True, True) and lane.active == 1
    finally:
        turn_slots.unbind(token)
    assert lane.active == 0 and _free(lane) == 1


@pytest.mark.asyncio
async def test_once_the_turn_is_over_a_late_wait_takes_no_slot():
    lane = ResizableSemaphore(1, name="interactive")
    ceiling = ResizableSemaphore(1, name="ceiling")
    slots = turn_slots.TurnSlots(lane, ceiling, session_key=A)
    token = turn_slots.bind(slots)
    try:
        async with slots:
            pass
        async with turn_slots.released_while_waiting(A):
            pass
        await slots.acquire()
    finally:
        turn_slots.unbind(token)
    assert slots.held == (False, False)
    assert lane.active == 0 and _free(lane) == 1 and _free(ceiling) == 1
