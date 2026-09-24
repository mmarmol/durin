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


@pytest.mark.asyncio
async def test_tool_schema_is_not_counted_twice_after_the_first_call(monkeypatch):
    """Real estimator, no stub. From the second call of a turn the estimate
    is anchored on the provider's own prompt count, which already includes
    the tool definitions. Adding the schema again made a turn that fits
    abort with a false "prompt overflow"."""
    from durin.agent.runner import AgentRunner, AgentRunSpec
    from durin.providers.base import LLMProvider, ToolCallRequest

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    calls = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                content="reading",
                tool_calls=[ToolCallRequest(id="c1", name="big", arguments={})],
                usage={"prompt_tokens": 45_000, "completion_tokens": 10},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    # About 20K tokens of schema: fits once (45K real prompt), not twice.
    tools.get_definitions.return_value = [{
        "type": "function",
        "function": {"name": "big", "description": "word " * 20_000,
                     "parameters": {"type": "object", "properties": {}}},
    }]
    tools.execute = AsyncMock(return_value="small result")

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=60_000,
        max_tokens=2000,
    ))

    assert result.stop_reason == "completed"
    assert result.final_content == "done"
    assert calls["n"] == 2
    assert [e for e in telemetry.events if e[0] == "mid_turn_precheck.overflow"] == []


@pytest.mark.asyncio
async def test_overflow_after_the_model_ran_says_the_request_is_unfinished(monkeypatch):
    """An abort at iteration > 0 happens after the model already worked on the
    request, and nothing re-sends it later. The user-facing error and the
    persisted placeholder must say the request was not finished, not that the
    model never ran or that a retry will happen by itself."""
    from durin.agent import runner as runner_mod
    from durin.agent.runner import AgentRunner, AgentRunSpec
    from durin.providers.base import ToolCallRequest

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    def _estimate(_provider, _model, messages, _tools=None):
        # Over budget only once a tool result exists: iteration 1, not 0.
        over = any(m.get("role") == "tool" for m in messages)
        return (500_000 if over else 1000), "test-counter"

    monkeypatch.setattr(runner_mod, "estimate_prompt_tokens_chain", _estimate)
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="reading",
        tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={})],
    ))
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="result")

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=10_000,
        max_tokens=2000,
    ))

    assert result.stop_reason == "mid_turn_precheck_overflow"
    assert provider.chat_with_retry.await_count == 1  # the model did run
    assert "not finished" in result.error
    assert "will retry" not in result.error
    placeholder = result.messages[-1]["content"]
    assert "before the model ran" not in placeholder
    assert "not finished" in placeholder
