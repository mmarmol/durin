"""The per-result cap follows the model's context window unless set explicitly."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec, result_char_cap
from durin.agent.tools.context import current_result_char_cap
from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest


@pytest.mark.parametrize(
    ("configured", "window", "expected"),
    [
        (None, None, 16_000),
        (None, 65_536, 16_000),
        (None, 150_000, 32_000),
        (None, 231_072, 64_000),
        (None, 8_000, 9_600),  # never more than 30% of the window
        (12_345, 231_072, 12_345),  # an explicit setting wins
    ],
)
def test_the_cap_follows_the_window(configured: int | None, window: int | None, expected: int) -> None:
    assert result_char_cap(configured, window) == expected


def test_the_config_leaves_the_cap_to_the_window_by_default() -> None:
    assert AgentDefaults().max_tool_result_chars is None


async def _cap_seen_by_a_tool(window: int | None, configured: int | None) -> int | None:
    seen: dict[str, Any] = {}
    calls = {"n": 0}

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="probe", arguments={})],
                usage={},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    async def execute(name: str, params: Any) -> str:
        seen["cap"] = current_result_char_cap()
        return "ok"

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = execute
    await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="m",
        max_iterations=3,
        max_tool_result_chars=configured,
        context_window_tokens=window,
    ))
    return seen["cap"]


@pytest.mark.asyncio
async def test_a_run_without_an_explicit_cap_uses_its_windows_cap() -> None:
    assert await _cap_seen_by_a_tool(231_072, None) == 64_000
    assert await _cap_seen_by_a_tool(65_536, None) == 16_000
    assert await _cap_seen_by_a_tool(231_072, 8_000) == 8_000
