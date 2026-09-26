"""A durin-posted note answers in the thread its session belongs to.

A system message (a background workflow's result, a sub-agent's result, the
note that says how an approval was decided) runs a turn in a session keyed to
a conversation, and the reply must land in that conversation. The session key
carries the thread for channels that scope sessions to one: the loop re-derives
it for the reply, since the note itself carries no channel metadata.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.providers.base import LLMResponse


def _loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok"))
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)

    async def _answer(initial_messages, **_kwargs):
        return ("noted", [], [*initial_messages, {"role": "assistant", "content": "noted"}],
                "stop", False, [])

    loop._run_agent_loop = _answer
    return loop


async def _reply_to_note(loop: AgentLoop, chat_id: str, session_key: str):
    return await loop._process_message(InboundMessage(
        channel="system", sender_id="approval_decision", chat_id=chat_id,
        content="[Approval decided outside this chat]\n\nRejected: x",
        session_key_override=session_key,
        metadata={"injected_event": "approval_decision"},
    ))


@pytest.mark.asyncio
async def test_a_telegram_topic_session_answers_in_its_topic(tmp_path):
    out = await _reply_to_note(_loop(tmp_path), "telegram:-1001", "telegram:-1001:topic:42")

    assert (out.channel, out.chat_id) == ("telegram", "-1001")
    assert out.metadata["message_thread_id"] == 42


@pytest.mark.asyncio
async def test_a_telegram_chat_with_no_topic_carries_no_thread(tmp_path):
    out = await _reply_to_note(_loop(tmp_path), "telegram:42", "telegram:42")

    assert "message_thread_id" not in out.metadata


@pytest.mark.asyncio
async def test_a_slack_thread_session_still_answers_in_its_thread(tmp_path):
    out = await _reply_to_note(_loop(tmp_path), "slack:C1", "slack:C1:1712345678.000100")

    assert out.metadata["slack"] == {"thread_ts": "1712345678.000100"}
