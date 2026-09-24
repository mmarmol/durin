"""An answer to a blocked ask_user is acknowledged like a consumed queued
message, so a client waiting on its own message knows which turn_end ends it."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from durin.agent import pending_answers
from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus


def _loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "m"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="m")


def _answer(content: str = "blue") -> InboundMessage:
    return InboundMessage(
        channel="websocket", sender_id="api:t", chat_id="c1", content=content,
        metadata={"client_msg_id": "cm-answer"},
    )


@pytest.mark.asyncio
async def test_an_answer_is_acknowledged_as_consumed(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    waiter = pending_answers.create("websocket:c1")
    try:
        assert await loop._answer_pending_question(_answer(), "websocket:c1") is True
        assert await asyncio.wait_for(waiter, 1) == "blue"
        ack = await loop.bus.consume_outbound()
        assert ack.metadata["_queued_consumed"] is True
        assert ack.metadata["client_msg_ids"] == ["cm-answer"]
    finally:
        pending_answers.discard("websocket:c1", waiter)


@pytest.mark.asyncio
async def test_a_message_with_no_question_waiting_is_not_consumed(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    assert await loop._answer_pending_question(_answer(), "websocket:c1") is False
    assert loop.bus.outbound_size == 0
