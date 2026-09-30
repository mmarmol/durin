"""A tool call can conclude the turn: the run stops after that round of tool calls.

A workflow node's `route` verdict is the last thing its turn needs. Without a
way to stop there, the model was asked again after the verdict was recorded —
one more request over the node's whole context — and the text that came with
the verdict (a FAIL's feedback) was replaced by whatever it said next.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.agent.tools.base import Tool
from durin.agent.tools.registry import ToolRegistry
from durin.providers.base import LLMResponse, ToolCallRequest


class _Decide(Tool):
    def __init__(self, decided: dict) -> None:
        self._decided = decided

    @property
    def name(self) -> str:
        return "decide"

    @property
    def description(self) -> str:
        return "Record the decision."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}

    async def execute(self, label: str, **kwargs: Any) -> str:
        if label in ("PASS", "FAIL"):
            self._decided["label"] = label
            return "Decision recorded."
        return f"'{label}' is not an allowed label."


def _provider(replies: list[LLMResponse]) -> tuple[MagicMock, list[dict]]:
    requests: list[dict] = []

    async def chat_with_retry(**kwargs: Any) -> LLMResponse:
        requests.append(kwargs)
        return replies.pop(0)

    provider = MagicMock()
    provider.chat_with_retry = chat_with_retry
    return provider, requests


def _decide(label: str, content: str | None, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[
        ToolCallRequest(id=call_id, name="decide", arguments={"label": label})])


async def _run(replies: list[LLMResponse], *, max_iterations: int = 5, seen: list | None = None):
    decided: dict = {}
    tools = ToolRegistry()
    tools.register(_Decide(decided))
    provider, requests = _provider(replies)

    def concluded(turn: list[dict]) -> bool:
        if seen is not None:
            seen.append(list(turn))
        return "label" in decided

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "judge it"}],
        tools=tools,
        model="m",
        max_iterations=max_iterations,
        max_tool_result_chars=16_000,
        end_turn_after_tools=concluded,
    ))
    return result, requests, decided


@pytest.mark.asyncio
async def test_a_concluding_call_ends_the_turn_with_the_text_sent_alongside() -> None:
    result, requests, decided = await _run(
        [_decide("FAIL", "The totals do not add up: recompute row 3.")])
    assert len(requests) == 1                      # no request after the verdict
    assert decided == {"label": "FAIL"}
    assert result.final_content == "The totals do not add up: recompute row 3."
    assert result.stop_reason == "completed"
    assert [m["role"] for m in result.messages[-2:]] == ["assistant", "tool"]


@pytest.mark.asyncio
async def test_the_check_is_given_the_turn_so_far() -> None:
    seen: list = []
    await _run([_decide("PASS", "All criteria met.")], seen=seen)
    turn = seen[-1]
    assert [m["role"] for m in turn] == ["assistant", "tool"]     # not the initial messages
    assert turn[0]["content"] == "All criteria met."


@pytest.mark.asyncio
async def test_a_concluding_call_without_text_ends_with_the_turns_latest_text() -> None:
    """The assessment came with an earlier call and the verdict alone after it: the
    turn's answer is the last thing the model wrote, not an empty reply."""
    result, requests, decided = await _run([
        _decide("MAYBE", "Row 3 is wrong: recompute it from the CSV.", call_id="c1"),
        _decide("FAIL", None, call_id="c2"),
    ])
    assert len(requests) == 2
    assert decided == {"label": "FAIL"}
    assert result.final_content == "Row 3 is wrong: recompute it from the CSV."


@pytest.mark.asyncio
async def test_a_call_that_does_not_conclude_lets_the_turn_go_on() -> None:
    result, requests, decided = await _run([
        _decide("MAYBE", "Not sure yet."),
        LLMResponse(content="Checked again; it holds.", tool_calls=[]),
    ])
    assert len(requests) == 2
    assert decided == {}
    assert result.final_content == "Checked again; it holds."


@pytest.mark.asyncio
async def test_a_concluding_call_closes_the_stream_it_left_open() -> None:
    """Before running tools the loop ends the stream as resuming, expecting a
    next request; when the turn ends there instead, the stream is closed."""
    from durin.agent.hook import AgentHook

    ends: list[bool] = []

    class _Streaming(AgentHook):
        def wants_streaming(self) -> bool:
            return True

        async def on_stream_end(self, context, *, resuming: bool) -> None:
            ends.append(resuming)

    decided: dict = {}
    tools = ToolRegistry()
    tools.register(_Decide(decided))
    provider = MagicMock()

    async def chat_stream_with_retry(**kwargs: Any) -> LLMResponse:
        return _decide("PASS", "All criteria met.")

    provider.chat_stream_with_retry = chat_stream_with_retry
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "judge it"}],
        tools=tools, model="m", max_iterations=5, max_tool_result_chars=16_000,
        hook=_Streaming(), end_turn_after_tools=lambda turn: "label" in decided,
    ))
    assert result.final_content == "All criteria met."
    assert ends == [True, False]


@pytest.mark.asyncio
async def test_a_concluding_call_on_the_last_round_completes_the_turn() -> None:
    result, requests, _ = await _run(
        [_decide("PASS", "All criteria met.")], max_iterations=1)
    assert len(requests) == 1
    assert result.stop_reason == "completed"
    assert result.final_content == "All criteria met."
