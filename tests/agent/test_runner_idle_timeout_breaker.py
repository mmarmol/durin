"""Idle-timeout circuit breaker (OpenClaw-inspired Tier 1).

Provider-level retries already absorb individual transient timeouts. But when
they exhaust and the runner keeps continuing (because the caller injects
follow-up user messages after each error), the agent burns tokens against a
clearly-stalled endpoint. The breaker counts consecutive iterations that
ended in a timeout response without any forward progress in between and trips
once the threshold is crossed, terminating with a distinct ``stop_reason``.
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


def _timeout_response() -> LLMResponse:
    return LLMResponse(
        content="Error calling LLM: timed out after 60s",
        finish_reason="error",
        error_kind="timeout",
        tool_calls=[],
        usage={},
    )


def _progress_response(content: str = "still working") -> LLMResponse:
    """A response that counts as forward progress: non-empty content
    with a non-error finish_reason (so it resets the counter)."""
    return LLMResponse(
        content=content,
        finish_reason="stop",
        tool_calls=[],
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )


def _tool_call_response() -> LLMResponse:
    return LLMResponse(
        content="",
        finish_reason="tool_calls",
        tool_calls=[ToolCallRequest(id="call_1", name="list_dir", arguments={"path": "."})],
        usage={"prompt_tokens": 10, "completion_tokens": 2},
    )


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    from durin.agent import runner as runner_mod
    monkeypatch.setattr(runner_mod, "current_telemetry", lambda: sink)


def _injection_callback_factory(items: list[list[dict]]):
    """Return a callback that pops from ``items`` on each call, returning [] when empty."""
    async def _callback(**_kwargs):
        if not items:
            return []
        return items.pop(0)
    return _callback


@pytest.mark.asyncio
async def test_breaker_opens_on_threshold_plus_one_with_injections(monkeypatch):
    """Two consecutive timeouts (threshold=1, default) → breaker opens.
    Injections force the loop to continue past the first timeout."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        _timeout_response(),
        _timeout_response(),
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []

    # Inject a follow-up after the first timeout so the run continues
    # into a second iteration (where the breaker should trip).
    injection_queue = [[{"role": "user", "content": "try again"}]]

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        injection_callback=_injection_callback_factory(injection_queue),
    ))

    assert result.stop_reason == "circuit_breaker_idle_timeout"
    assert result.error is not None
    assert "consecutive idle timeouts" in result.error.lower()
    assert provider.chat_with_retry.await_count == 2

    cb_events = [e for e in telemetry.events if e[0] == "circuit_breaker.idle_timeout"]
    assert len(cb_events) == 1
    payload = cb_events[0][1]
    assert payload["consecutive_timeouts"] == 2
    assert payload["threshold"] == 1


@pytest.mark.asyncio
async def test_single_timeout_does_not_open_breaker(monkeypatch):
    """One timeout is below the default threshold (1) so the breaker
    must not fire — the run still terminates with the regular error
    path (no injections to continue it)."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=_timeout_response())
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
    ))

    assert result.stop_reason == "error"
    cb_events = [e for e in telemetry.events if e[0] == "circuit_breaker.idle_timeout"]
    assert cb_events == []


@pytest.mark.asyncio
async def test_progress_resets_consecutive_counter(monkeypatch):
    """A timeout followed by forward progress (tool_calls) followed by a
    second timeout must NOT trip the breaker — the intervening output
    resets the counter, so the second timeout counts as ``1``, not ``2``.

    Sequence:
      iter 0: timeout            → counter=1, injection continues run
      iter 1: tool_calls         → counter reset to 0, tool executes
      iter 2: timeout            → counter=1 (NOT 2), no injection → break
                                   via the regular error path, NOT the breaker.
    """
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        _timeout_response(),
        _tool_call_response(),
        _timeout_response(),
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="tool result")

    # Only one injection — used to bridge from iter 0's timeout into iter 1's
    # tool call. After that the queue is empty, so the iter-2 timeout
    # terminates via the regular error path. The key assertion is that the
    # BREAKER did not fire; if the counter hadn't reset between iter 0 and
    # iter 2, the second timeout would have tripped it.
    injection_queue = [
        [{"role": "user", "content": "retry once"}],
    ]

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        injection_callback=_injection_callback_factory(injection_queue),
    ))

    assert provider.chat_with_retry.await_count == 3
    assert result.stop_reason == "error"  # regular error path, NOT the breaker
    cb_events = [e for e in telemetry.events if e[0] == "circuit_breaker.idle_timeout"]
    assert cb_events == []


@pytest.mark.asyncio
async def test_threshold_is_configurable_via_env(monkeypatch):
    """With threshold=2, three consecutive timeouts (with injections in
    between) trip the breaker; two do not."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    monkeypatch.setenv("DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS", "2")
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        _timeout_response(),
        _timeout_response(),
        _timeout_response(),
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []

    injection_queue = [
        [{"role": "user", "content": "retry"}],
        [{"role": "user", "content": "retry"}],
    ]

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        injection_callback=_injection_callback_factory(injection_queue),
    ))

    assert result.stop_reason == "circuit_breaker_idle_timeout"
    cb_events = [e for e in telemetry.events if e[0] == "circuit_breaker.idle_timeout"]
    assert len(cb_events) == 1
    assert cb_events[0][1]["consecutive_timeouts"] == 3
    assert cb_events[0][1]["threshold"] == 2


@pytest.mark.asyncio
async def test_non_timeout_error_does_not_increment_counter(monkeypatch):
    """A non-timeout error (e.g. 500) shouldn't increment the idle-timeout
    counter. After it the run breaks via the regular error path; no
    breaker event is emitted."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    server_error = LLMResponse(
        content="Error calling LLM: 500 Internal Server Error",
        finish_reason="error",
        error_kind="server_error",
        usage={},
    )
    provider.chat_with_retry = AsyncMock(side_effect=[
        _timeout_response(),
        server_error,
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []

    injection_queue = [[{"role": "user", "content": "try again"}]]

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        injection_callback=_injection_callback_factory(injection_queue),
    ))

    # Run ends via the regular error path (server_error), not the breaker.
    assert result.stop_reason == "error"
    cb_events = [e for e in telemetry.events if e[0] == "circuit_breaker.idle_timeout"]
    assert cb_events == []


def test_max_consecutive_idle_timeouts_default_and_override(monkeypatch):
    """The threshold reader returns the OpenClaw default (1), honors valid
    overrides, and falls back to default on garbage values."""
    from durin.agent.runner import _max_consecutive_idle_timeouts

    monkeypatch.delenv("DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS", raising=False)
    assert _max_consecutive_idle_timeouts() == 1

    monkeypatch.setenv("DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS", "3")
    assert _max_consecutive_idle_timeouts() == 3

    monkeypatch.setenv("DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS", "not-a-number")
    assert _max_consecutive_idle_timeouts() == 1

    monkeypatch.setenv("DURIN_MAX_CONSECUTIVE_IDLE_TIMEOUTS", "-5")
    # Negative clamps to 0 (immediate breaker on first timeout).
    assert _max_consecutive_idle_timeouts() == 0
