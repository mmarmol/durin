"""A direct turn (process_direct) is registered like a bus turn: /stop and the
stop route can cancel it, shutdown's drain bounds it without journaling it,
and ContextVars the turn sets still reach its caller."""

import asyncio
import contextvars
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus

_SET_IN_TURN: contextvars.ContextVar[bool] = contextvars.ContextVar("set_in_turn", default=False)


def _loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "m"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="m")

    async def _no_mcp():
        return None

    loop._connect_mcp = _no_mcp  # type: ignore[method-assign]
    return loop


def _hanging(started: asyncio.Event):
    async def _turn(*_a, **_kw):
        started.set()
        await asyncio.Event().wait()

    return _turn


@pytest.mark.asyncio
async def test_a_direct_turn_is_registered_and_stoppable(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    started = asyncio.Event()
    loop._process_message = _hanging(started)  # type: ignore[method-assign]
    caller = asyncio.create_task(
        loop.process_direct("hi", session_key="api:x", channel="api", chat_id="default"))
    await started.wait()
    assert loop._active_tasks.get("api:x")
    assert await loop.cancel_session_turns("api:x") == 1
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_a_finished_direct_turn_leaves_no_registration(tmp_path: Path) -> None:
    loop = _loop(tmp_path)

    async def _ok(*_a, **_kw):
        return None

    loop._process_message = _ok  # type: ignore[method-assign]
    await loop.process_direct("hi", session_key="cron:job:run:1", channel="cli", chat_id="direct")
    assert "cron:job:run:1" not in loop._active_tasks


@pytest.mark.asyncio
async def test_context_set_inside_the_turn_reaches_the_caller(tmp_path: Path) -> None:
    # Cron reads the message tool's "already delivered" flag (a ContextVar)
    # after the turn to avoid delivering the reply twice.
    loop = _loop(tmp_path)

    async def _turn(*_a, **_kw):
        _SET_IN_TURN.set(True)
        return None

    loop._process_message = _turn  # type: ignore[method-assign]

    async def _caller() -> bool:
        await loop.process_direct("hi", session_key="cron:job", channel="cli", chat_id="direct")
        return _SET_IN_TURN.get()

    assert await asyncio.create_task(_caller()) is True


@pytest.mark.asyncio
async def test_shutdown_cancels_a_direct_turn_without_journaling_it(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    journal = MagicMock()
    journal.append.side_effect = lambda msgs: len(msgs)
    loop._inbound_journal = journal
    started = asyncio.Event()
    loop._process_message = _hanging(started)  # type: ignore[method-assign]
    caller = asyncio.create_task(
        loop.process_direct("hi", session_key="cron:job", channel="cli", chat_id="direct"))
    await started.wait()
    # A regression must fail, not hang: bound the drain and the caller's end.
    async with asyncio.timeout(5):
        journaled = await loop.drain_inbound_for_shutdown()
        await asyncio.wait({caller})
    assert journaled == 0
    assert caller.cancelled()
    assert journal.append.call_args.args[0] == []


def test_dropping_the_last_task_of_a_key_removes_the_key(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    task = MagicMock()
    loop._active_tasks["cron:job:run:2"] = [task]
    loop._drop_active_task(task, "cron:job:run:2")
    assert "cron:job:run:2" not in loop._active_tasks


@pytest.mark.asyncio
async def test_a_nested_direct_turn_keeps_the_outer_turn_s_journal_entry(tmp_path: Path) -> None:
    # A bus turn that runs a direct turn (e.g. the cron tool running a job
    # inside a conversation) must still be journaled at shutdown.
    loop = _loop(tmp_path)

    async def _ok(*_a, **_kw):
        return None

    loop._process_message = _ok  # type: ignore[method-assign]

    async def _outer_bus_turn():
        me = asyncio.current_task()
        loop._in_flight_messages[me] = "outer inbound"
        await loop.process_direct("job", session_key="cron:job:run:3", channel="cli", chat_id="direct")
        return loop._in_flight_messages.get(me)

    assert await asyncio.create_task(_outer_bus_turn()) == "outer inbound"
