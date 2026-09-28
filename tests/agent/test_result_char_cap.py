"""The per-result cap follows the model's context window unless set explicitly."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec, result_char_cap
from durin.agent.tools.context import current_result_char_cap
from durin.config.schema import AgentDefaults
from durin.providers.base import GenerationSettings, LLMResponse, ToolCallRequest


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


@pytest.mark.parametrize(
    ("window", "input_budget", "expected"),
    [
        (16_384, 7_168, 8_601),  # 30% of the input budget, not of the window
        (231_072, 225_952, 64_000),  # on a large window the tier still wins
        (8_000, 0, 9_600),  # no usable budget: the window's share
        (8_000, None, 9_600),
    ],
)
def test_the_cap_is_a_share_of_the_input_budget(
    window: int, input_budget: int | None, expected: int,
) -> None:
    assert result_char_cap(None, window, input_budget) == expected


def _loop_settings(window: int, max_output: int, block_limit: int | None = None) -> SimpleNamespace:
    """The loop attributes its save-time cap reads, as a real loop has them."""
    return SimpleNamespace(
        max_tool_result_chars=None,
        context_window_tokens=window,
        context_block_limit=block_limit,
        provider=SimpleNamespace(generation=GenerationSettings(max_tokens=max_output)),
    )


def test_the_loop_saves_under_the_cap_its_runs_use() -> None:
    from durin.agent.loop import AgentLoop

    # 16,384 − 8,192 held for the answer − 1,024 margin = 7,168 input tokens.
    assert AgentLoop._saved_result_cap(_loop_settings(16_384, 8_192)) == 8_601
    # A block limit replaces the computed input budget, as it does in a run.
    assert AgentLoop._saved_result_cap(_loop_settings(16_384, 8_192, 5_000)) == 6_000


async def _cap_seen_by_a_tool(
    window: int | None, configured: int | None, max_output: int | None = None,
) -> int | None:
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
    if max_output is not None:
        provider.generation = GenerationSettings(max_tokens=max_output)
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


@pytest.mark.asyncio
async def test_a_run_on_a_small_window_caps_by_its_input_budget() -> None:
    assert await _cap_seen_by_a_tool(16_384, None, max_output=8_192) == 8_601
