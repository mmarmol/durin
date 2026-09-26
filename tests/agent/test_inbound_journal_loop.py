"""Queued follow-ups survive a gateway restart.

A turn in flight keeps later same-session messages in in-memory pending
queues; the turn's ``finally`` re-publishes them to the in-memory bus, and a
stopping gateway used to exit without ever consuming that bus again, so every
restart with a turn in flight silently discarded the follow-ups (no channel
redelivers them). The loop now journals them at shutdown and replays the
journal at the next start.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from durin.agent.loop import AgentLoop, PendingQueues
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus


def _make_loop(workspace: Path, *, process_kind: str = "gateway") -> tuple[AgentLoop, MessageBus]:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace, process_kind=process_kind)
    return loop, bus


def _msg(content: str, **kwargs) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content=content, **kwargs)


@pytest.mark.asyncio
async def test_drain_at_shutdown_journals_bus_and_pending_queue_messages(tmp_path: Path) -> None:
    loop, bus = _make_loop(tmp_path)
    # A follow-up already re-published to the bus by a finished turn…
    await bus.publish_inbound(_msg("re-published"))
    # …and two still sitting in a live session's pending queues.
    queues = PendingQueues.create()
    queues.inject.put_nowait(_msg("steer", metadata={"steer": True}))
    queues.deferred.put_nowait(_msg("deferred"))
    loop._pending_queues["telegram:c1"] = queues

    journaled = await loop.drain_inbound_for_shutdown()

    assert journaled == 3
    assert bus.inbound.qsize() == 0
    assert queues.inject.qsize() == 0 and queues.deferred.qsize() == 0
    replayed = loop._inbound_journal.drain()
    assert sorted(m.content for m in replayed) == ["deferred", "re-published", "steer"]


@pytest.mark.asyncio
async def test_drain_at_shutdown_cancels_the_turn_in_flight_first(tmp_path: Path) -> None:
    """The turn's own ``finally`` is what moves its pending queue to the bus;
    the drain must cancel and await the turn so that hand-off happens before
    the bus is read."""
    loop, bus = _make_loop(tmp_path)
    turn_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_dispatch(msg, pending=None):
        turn_started.set()
        try:
            await release.wait()
        finally:
            # Mirror what the real ``_dispatch`` does on its way out.
            queues = loop._pending_queues.pop("telegram:c1", None)
            while queues is not None:
                try:
                    item = queues.deferred.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await bus.publish_inbound(item)

    loop._dispatch = fake_dispatch  # type: ignore[method-assign]
    queues = PendingQueues.create()
    queues.deferred.put_nowait(_msg("typed mid-turn"))
    loop._pending_queues["telegram:c1"] = queues
    task = asyncio.create_task(loop._dispatch(_msg("the turn"), queues))
    loop._active_tasks["telegram:c1"] = [task]
    await turn_started.wait()

    journaled = await loop.drain_inbound_for_shutdown()

    assert task.done()
    assert journaled == 1
    assert [m.content for m in loop._inbound_journal.drain()] == ["typed mid-turn"]


@pytest.mark.asyncio
async def test_trigger_only_messages_are_not_journaled(tmp_path: Path) -> None:
    loop, bus = _make_loop(tmp_path)
    await bus.publish_inbound(_msg("alert", trigger_only=True))
    await bus.publish_inbound(_msg("conversation"))

    assert await loop.drain_inbound_for_shutdown() == 1
    assert [m.content for m in loop._inbound_journal.drain()] == ["conversation"]


@pytest.mark.asyncio
async def test_start_replays_the_journal_into_the_bus(tmp_path: Path) -> None:
    stopping, _ = _make_loop(tmp_path)
    stopping._inbound_journal.append([_msg("a"), _msg("b")])

    starting, bus = _make_loop(tmp_path)
    replayed = await starting._replay_inbound_journal()

    assert replayed == 2
    assert bus.inbound.qsize() == 2
    assert [m.content for m in (bus.inbound.get_nowait(), bus.inbound.get_nowait())] == ["a", "b"]
    # Replayed once: the journal is gone.
    assert await starting._replay_inbound_journal() == 0


@pytest.mark.asyncio
async def test_run_replays_the_journal_before_consuming(tmp_path: Path) -> None:
    stopping, _ = _make_loop(tmp_path)
    stopping._inbound_journal.append([_msg("from before the restart")])

    loop, bus = _make_loop(tmp_path)
    seen: list[str] = []

    async def fake_dispatch(msg, pending=None):
        seen.append(msg.content)

    async def _noop(*args, **kwargs):
        return None

    loop._dispatch = fake_dispatch  # type: ignore[method-assign]
    loop._connect_mcp = _noop  # type: ignore[method-assign]
    loop._warmup_memory_embedding = _noop  # type: ignore[method-assign]
    runner = asyncio.create_task(loop.run())
    try:
        for _ in range(200):
            await asyncio.sleep(0.005)
            if seen:
                break
        assert seen == ["from before the restart"]
    finally:
        loop._running = False
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


@pytest.mark.asyncio
async def test_a_tui_written_entry_survives_a_gateway_replay_and_is_replayed_by_the_tui(
    tmp_path: Path,
) -> None:
    """A gateway and a TUI (or legacy REPL) can share one workspace. A
    gateway starting up must not steal a turn the TUI itself still owes, and
    the reverse: each process's replay only ever takes its own kind's
    entries out of the shared journal file."""
    tui_stopping, _ = _make_loop(tmp_path, process_kind="tui")
    tui_stopping._inbound_journal.append([_msg("from the tui")], kind="tui")

    gateway_starting, gateway_bus = _make_loop(tmp_path, process_kind="gateway")
    assert await gateway_starting._replay_inbound_journal() == 0
    assert gateway_bus.inbound.qsize() == 0

    tui_starting, tui_bus = _make_loop(tmp_path, process_kind="tui")
    assert await tui_starting._replay_inbound_journal() == 1
    assert [m.content for m in (tui_bus.inbound.get_nowait(),)] == ["from the tui"]


@pytest.mark.asyncio
async def test_the_gateways_own_entries_survive_a_tui_replay_and_are_replayed_by_the_gateway(
    tmp_path: Path,
) -> None:
    gateway_stopping, _ = _make_loop(tmp_path, process_kind="gateway")
    gateway_stopping._inbound_journal.append([_msg("from the gateway")], kind="gateway")

    tui_starting, tui_bus = _make_loop(tmp_path, process_kind="tui")
    assert await tui_starting._replay_inbound_journal() == 0
    assert tui_bus.inbound.qsize() == 0

    gateway_starting, gateway_bus = _make_loop(tmp_path, process_kind="gateway")
    assert await gateway_starting._replay_inbound_journal() == 1
    assert [m.content for m in (gateway_bus.inbound.get_nowait(),)] == ["from the gateway"]
