"""Mid-turn precheck signal (OpenClaw-inspired Tier 2 A2).

After the sanitize pipeline runs (``_snip_history`` + orphan repair), the
runner estimates whether the post-sanitize prompt fits the input budget.
If it doesn't — which happens when a single oversized tool result late in
the conversation survives snipping because of role-alternation safety
guarantees — the runner aborts the turn with
``stop_reason="mid_turn_precheck_overflow"`` BEFORE calling the LLM,
saving the wasted call that would have returned a 400 anyway.

The next turn re-runs A1 (pre-emptive compaction) which compacts the
session before the runner is invoked again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    from durin.agent import runner as runner_mod
    monkeypatch.setattr(runner_mod, "current_telemetry", lambda: sink)


@pytest.mark.asyncio
async def test_overflow_terminates_turn_before_llm_call(monkeypatch):
    """When the estimator says we're over budget, the runner must abort
    before any provider call — verified by ``chat_with_retry`` never being
    awaited."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="never"))
    # Force the chain estimator to report overflow regardless of inputs.
    from durin.agent import runner as runner_mod
    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_args, **_kwargs: (500_000, "test-counter"),
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=10_000,  # 500K >> 10K-ish budget
        max_tokens=2000,
    ))

    assert result.stop_reason == "mid_turn_precheck_overflow"
    assert result.error is not None
    assert "prompt overflow" in result.error.lower()
    provider.chat_with_retry.assert_not_awaited()

    events = [e for e in telemetry.events if e[0] == "mid_turn_precheck.overflow"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["estimated_tokens"] == 500_000
    assert payload["budget_tokens"] > 0
    assert payload["iteration"] == 0


@pytest.mark.asyncio
async def test_in_budget_skips_precheck_path(monkeypatch):
    """When the estimator says we're fine, the precheck must not interfere
    with the normal LLM call path."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[]))
    from durin.agent import runner as runner_mod
    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_args, **_kwargs: (1000, "test-counter"),
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=100_000,
    ))

    assert result.stop_reason == "completed"
    assert result.final_content == "done"
    provider.chat_with_retry.assert_awaited_once()
    assert [e for e in telemetry.events if e[0] == "mid_turn_precheck.overflow"] == []


@pytest.mark.asyncio
async def test_no_context_window_skips_precheck(monkeypatch):
    """When the caller didn't supply ``context_window_tokens``, the
    precheck must be a no-op (we can't compute a budget). The LLM call
    proceeds as before."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        # context_window_tokens not set → default None → skip precheck.
    ))

    assert result.stop_reason == "completed"
    provider.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_estimator_exception_does_not_break_turn(monkeypatch):
    """If the token estimator raises (rare but possible for unusual
    message shapes), the precheck must fall back to the normal path —
    never block a turn on a broken estimator."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="recovered", tool_calls=[]))
    from durin.agent import runner as runner_mod
    def _explode(*_args, **_kwargs):
        raise RuntimeError("estimator went wrong")
    monkeypatch.setattr(runner_mod, "estimate_prompt_tokens_chain", _explode)
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=10_000,
        max_tokens=2000,
    ))

    assert result.stop_reason == "completed"
    assert result.final_content == "recovered"


def test_precheck_helper_returns_none_when_under_budget(monkeypatch):
    """Direct unit test of the helper for clean failure semantics."""
    from durin.agent.runner import AgentRunner, AgentRunSpec
    from durin.agent import runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_args, **_kwargs: (1000, "test"),
    )
    provider = MagicMock()
    runner = AgentRunner(provider)
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="m",
        max_iterations=1,
        max_tool_result_chars=1000,
        context_window_tokens=10_000,
        max_tokens=2000,
    )
    assert runner._mid_turn_precheck(spec, [{"role": "user", "content": "hi"}]) is None


def test_precheck_helper_returns_estimate_when_over_budget(monkeypatch):
    from durin.agent.runner import AgentRunner, AgentRunSpec
    from durin.agent import runner as runner_mod

    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_args, **_kwargs: (99_999, "test"),
    )
    provider = MagicMock()
    runner = AgentRunner(provider)
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="m",
        max_iterations=1,
        max_tool_result_chars=1000,
        context_window_tokens=10_000,
        max_tokens=2000,
    )
    result = runner._mid_turn_precheck(spec, [{"role": "user", "content": "hi"}])
    assert result is not None
    estimate, budget = result
    assert estimate == 99_999
    assert budget < estimate
