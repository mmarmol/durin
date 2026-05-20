"""Unknown-tool loop guard (OpenClaw-inspired Tier 2 B2).

durin's 1A hash-based loop detection blocks repeats of the EXACT
``(tool_name, arguments)`` pair after a failure. But a hallucinated tool
name often comes with varying args each iteration (the model is
experimenting), so 1A doesn't catch it. B2 tracks calls to unknown names
per-turn; once any name's count exceeds the threshold, the turn
terminates with ``stop_reason="unknown_tool_loop_guard"`` so callers can
distinguish from generic errors.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    from durin.agent import runner as runner_mod
    monkeypatch.setattr(runner_mod, "current_telemetry", lambda: sink)


def _make_tools(known: list[str]):
    """ToolRegistry-like mock: ``tool_names`` reports the list of known
    names. The guard probes ``tool_names`` (not ``__contains__``) because
    MagicMock's default ``__contains__`` returns a mock that confuses
    membership checks. Real ToolRegistry exposes a list property."""
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = list(known)  # real list, not a Mock attr
    tools.execute = AsyncMock(return_value="ok")
    return tools


def _hallucinated_call(name: str, i: int) -> ToolCallRequest:
    """Vary the args each call so 1A's signature-loop guard does NOT fire
    (the differentiator for B2)."""
    return ToolCallRequest(id=f"call_{i}", name=name, arguments={"q": f"v{i}"})


@pytest.mark.asyncio
async def test_third_unknown_call_trips_breaker(monkeypatch):
    """Default threshold is 2 → first two calls allowed (model gets the
    error and might recover); third call to the same unknown name aborts
    the turn."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    # Return the same hallucinated name three times with different args.
    responses = [
        LLMResponse(content="", tool_calls=[_hallucinated_call("search_web", i)], finish_reason="tool_calls", usage={})
        for i in range(3)
    ]
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    tools = _make_tools(["web_search"])  # the real tool — model hallucinated "search_web"

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "search for X"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.stop_reason == "unknown_tool_loop_guard"
    assert "search_web" in result.error
    # Three iterations: first two "not found" errors, third one trips the breaker.
    assert provider.chat_with_retry.await_count == 3

    events = [e for e in telemetry.events if e[0] == "unknown_tool.loop_guard"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["tool_name"] == "search_web"
    assert payload["attempts"] == 3
    assert payload["threshold"] == 2


@pytest.mark.asyncio
async def test_known_tool_calls_do_not_trip(monkeypatch):
    """A turn with many calls to a REAL tool must never fire B2."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    # Model uses the real tool, then completes.
    responses = [
        LLMResponse(content="", tool_calls=[ToolCallRequest(id=f"c{i}", name="web_search", arguments={"q": "x"})], finish_reason="tool_calls", usage={})
        for i in range(3)
    ] + [LLMResponse(content="done", tool_calls=[], usage={})]
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    tools = _make_tools(["web_search"])

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "search"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.stop_reason == "completed"
    assert result.final_content == "done"
    assert [e for e in telemetry.events if e[0] == "unknown_tool.loop_guard"] == []


@pytest.mark.asyncio
async def test_different_unknown_names_each_count_separately(monkeypatch):
    """The counter is per-name. Calling ``foo`` once and ``bar`` once
    should NOT trip — neither is at the threshold individually."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    # foo → not found, bar → not found, then model gives up and replies.
    responses = [
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="foo", arguments={})], finish_reason="tool_calls", usage={}),
        LLMResponse(content="", tool_calls=[ToolCallRequest(id="c2", name="bar", arguments={})], finish_reason="tool_calls", usage={}),
        LLMResponse(content="giving up", tool_calls=[], usage={}),
    ]
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    tools = _make_tools(["web_search"])

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "x"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.stop_reason == "completed"
    assert [e for e in telemetry.events if e[0] == "unknown_tool.loop_guard"] == []


@pytest.mark.asyncio
async def test_configurable_via_env_var(monkeypatch):
    """``DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS=0`` → trips on the FIRST unknown
    call (zero tolerance)."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    monkeypatch.setenv("DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS", "0")
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="",
        tool_calls=[ToolCallRequest(id="c1", name="hallu", arguments={})],
        finish_reason="tool_calls",
        usage={},
    ))
    tools = _make_tools(["web_search"])

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "x"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    # First call already counts as 1 > 0 threshold → trip immediately.
    assert result.stop_reason == "unknown_tool_loop_guard"
    assert provider.chat_with_retry.await_count == 1


def test_threshold_reader_default_and_override(monkeypatch):
    from durin.agent.runner import _max_unknown_tool_attempts

    monkeypatch.delenv("DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS", raising=False)
    assert _max_unknown_tool_attempts() == 2

    monkeypatch.setenv("DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS", "5")
    assert _max_unknown_tool_attempts() == 5

    monkeypatch.setenv("DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS", "garbage")
    assert _max_unknown_tool_attempts() == 2

    monkeypatch.setenv("DURIN_MAX_UNKNOWN_TOOL_ATTEMPTS", "-3")
    # Negative clamps to 0 → first call already trips.
    assert _max_unknown_tool_attempts() == 0
