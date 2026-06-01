"""B3: the consumer must register a session's pending queue *synchronously*
before spawning the dispatch task.

The bug: registration happened inside ``_dispatch`` (the spawned task), which
runs only after ``create_task`` yields. A second same-session message consumed
in that gap finds no registered queue and spawns a *competing* task — so
mid-turn injection is defeated and the pending-queue dict gets overwritten.

This pins the contract: feeding two same-session messages back-to-back must
produce exactly one dispatch task, with the second message routed into the
pending queue (injection), not a second task.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus


def _make_loop() -> tuple[AgentLoop, MessageBus]:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())
    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop, bus


@pytest.mark.asyncio
async def test_second_same_session_message_routes_to_pending_not_a_second_task():
    loop, bus = _make_loop()

    calls: list[InboundMessage] = []
    release = asyncio.Event()

    async def fake_dispatch(msg, *args, **kwargs):
        # Stand in for a turn in progress: record it and hold the turn open
        # so the pending queue stays alive while the consumer keeps draining.
        calls.append(msg)
        await release.wait()

    loop._dispatch = fake_dispatch  # type: ignore[method-assign]
    loop._connect_mcp = _noop  # type: ignore[method-assign]
    loop._warmup_memory_embedding = _noop  # type: ignore[method-assign]

    m1 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="first")
    m2 = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="second")
    await bus.publish_inbound(m1)
    await bus.publish_inbound(m2)

    key = loop._effective_session_key(m1)
    runner = asyncio.create_task(loop.run())
    try:
        # Wait until both messages are accounted for (either dispatched as a
        # task or routed into a pending queue), bounded so a regression can't
        # hang the test.
        for _ in range(200):
            await asyncio.sleep(0.005)
            pending = sum(q.qsize() for q in loop._pending_queues.values())
            if len(calls) + pending >= 2:
                break

        assert len(calls) == 1, f"expected one dispatch task, got {len(calls)}"
        assert key in loop._pending_queues, "pending queue not registered synchronously"
        assert loop._pending_queues[key].qsize() == 1, "second message not routed to pending queue"
    finally:
        loop._running = False
        release.set()
        runner.cancel()
        with pytest.raises((asyncio.CancelledError,)):
            await runner


async def _noop(*args, **kwargs):
    return None
