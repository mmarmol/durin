"""The turn in flight at a graceful shutdown is answered after the restart.

The shutdown drain journals the follow-ups a turn kept in its pending queues,
but the message the turn itself was answering used to be dropped: its user
message sat in the session with the ``pending_user_turn`` flag, and the next
contact closed it as "Error: Task interrupted" with no answer ever given.
The drain now journals that message too, ahead of its follow-ups, so the
replay at the next start runs it again in order.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus


def _make_loop(workspace: Path) -> tuple[AgentLoop, MessageBus]:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop, bus


def _msg(content: str, **kwargs) -> InboundMessage:
    return InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content=content, **kwargs)


@pytest.mark.asyncio
async def test_drain_journals_the_turn_in_flight_ahead_of_its_follow_ups(tmp_path: Path) -> None:
    loop, bus = _make_loop(tmp_path)
    turn_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_dispatch(msg, pending=None):
        turn_started.set()
        try:
            await release.wait()
        finally:
            # What the real ``_dispatch`` does on its way out: hand the
            # deferred follow-ups back to the bus.
            queues = loop._pending_queues.pop("telegram:c1", None)
            while queues is not None:
                try:
                    item = queues.deferred.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await bus.publish_inbound(item)

    loop._dispatch = fake_dispatch  # type: ignore[method-assign]
    loop._start_turn_task(_msg("the turn"), "telegram:c1")
    await turn_started.wait()
    loop._pending_queues["telegram:c1"].deferred.put_nowait(_msg("typed mid-turn"))

    journaled = await loop.drain_inbound_for_shutdown()

    assert journaled == 2
    assert [m.content for m in loop._inbound_journal.drain()] == ["the turn", "typed mid-turn"]


@pytest.mark.asyncio
async def test_a_turn_that_finished_before_the_drain_is_not_journaled(tmp_path: Path) -> None:
    loop, bus = _make_loop(tmp_path)

    async def fake_dispatch(msg, pending=None):
        loop._pending_queues.pop("telegram:c1", None)

    loop._dispatch = fake_dispatch  # type: ignore[method-assign]
    task = loop._start_turn_task(_msg("answered already"), "telegram:c1")
    await task

    assert await loop.drain_inbound_for_shutdown() == 0
    assert loop._inbound_journal.drain() == []


@pytest.mark.asyncio
async def test_a_trigger_only_turn_in_flight_is_not_journaled(tmp_path: Path) -> None:
    """An automation trigger that was being evaluated is not a conversation;
    replaying it later would fire the alert out of time."""
    loop, bus = _make_loop(tmp_path)
    turn_started = asyncio.Event()

    async def fake_dispatch(msg, pending=None):
        turn_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            loop._pending_queues.pop("telegram:c1", None)

    loop._dispatch = fake_dispatch  # type: ignore[method-assign]
    loop._start_turn_task(_msg("alert", trigger_only=True), "telegram:c1")
    await turn_started.wait()

    assert await loop.drain_inbound_for_shutdown() == 0
