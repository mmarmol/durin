"""Each message of a turn carries the time it happened, not the time the turn
was saved: a 25-minute turn otherwise shows every step at its last second."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest


@pytest.mark.asyncio
async def test_tool_round_messages_are_stamped_when_they_happen():
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    calls = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                content="using tool",
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "x"})],
                usage={},
            )
        return LLMResponse(content="final answer", tool_calls=[], usage={})

    async def slow_tool(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "file content"

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = slow_tool

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
    ))

    asked = next(m for m in result.messages if m.get("role") == "assistant" and m.get("tool_calls"))
    answered = next(m for m in result.messages if m.get("role") == "tool")
    asked_at = datetime.fromisoformat(asked["timestamp"])
    answered_at = datetime.fromisoformat(answered["timestamp"])
    # The tool took 50 ms: its result is stamped after the call that asked for it.
    assert (answered_at - asked_at).total_seconds() >= 0.04
