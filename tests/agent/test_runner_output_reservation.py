"""Output-reservation budgeting + emergency-trim recovery (context-budget fix).

Root cause this suite locks in: the runner used to reserve the FULL
configured ``max_tokens`` as output headroom when computing the input
budget AND send that same ceiling to the provider. A model configured
with a large output ceiling (e.g. 128k on a 202k window) therefore had
its *input* budget collapse to ~74k, so a small tool-result read could
overflow and silently abort the turn.

The fix has three parts:

* **C** — cap the output *reservation* used for input budgeting at
  ``_MAX_OUTPUT_RESERVATION`` and send a *dynamic* ``max_tokens`` that
  fills the room actually left by the prompt (up to the configured
  ceiling), so a high ceiling no longer starves input.
* **A** — when the turn is genuinely unrecoverable, persist an
  overflow-specific placeholder instead of the generic "model error".
* **B** — on overflow, emergency-trim oversized tool results on the
  model-facing copy and proceed when that brings the prompt under
  budget, instead of dropping the in-flight request.
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


def _size_estimator(*_args, **_kwargs):
    """Estimate = total chars of string contents // 1 (deterministic)."""
    messages = _args[2]
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
    return total, "size-counter"


# --------------------------------------------------------------------------
# C: output reservation is capped, not the full ceiling
# --------------------------------------------------------------------------

def test_large_ceiling_does_not_starve_input_budget(monkeypatch):
    """A 128k ceiling on a 202k window must NOT collapse the input budget.

    Old behaviour: budget = 202800 - 128000 - 1024 = 73776, so an
    estimate of 100k overflowed. New behaviour: the reservation is
    capped, so 100k fits and the precheck returns None.
    """
    from durin.agent import runner as runner_mod
    from durin.agent.runner import AgentRunner, AgentRunSpec

    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_a, **_k: (100_000, "test"),
    )
    runner = AgentRunner(MagicMock())
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="glm-5.2",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=202_800,
        max_tokens=128_000,
    )
    spec.tools.get_definitions.return_value = []
    assert runner._mid_turn_precheck(spec, [{"role": "user", "content": "hi"}]) is None


def test_small_ceiling_budget_unchanged(monkeypatch):
    """When the ceiling is already below the reservation cap, behaviour
    is identical to before — small-window configs are untouched."""
    from durin.agent import runner as runner_mod
    from durin.agent.runner import AgentRunner, AgentRunSpec

    # 6976 budget = 10000 - 2000 - 1024 (reservation == ceiling == 2000).
    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_a, **_k: (7000, "test"),
    )
    runner = AgentRunner(MagicMock())
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="m",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=10_000,
        max_tokens=2_000,
    )
    spec.tools.get_definitions.return_value = []
    result = runner._mid_turn_precheck(spec, [{"role": "user", "content": "hi"}])
    assert result is not None
    estimate, budget = result
    assert budget == 6976


# --------------------------------------------------------------------------
# C: dynamic max_tokens sent to the provider
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dynamic_max_tokens_fills_available_room(monkeypatch):
    """The provider call must receive max_tokens = min(ceiling, room),
    where room = window - estimate - buffer — not the full ceiling, and
    not a tiny static value."""
    from durin.agent import runner as runner_mod
    from durin.agent.runner import AgentRunner, AgentRunSpec

    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_a, **_k: (50_000, "test"),
    )
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", tool_calls=[]),
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="glm-5.2",
        max_iterations=2,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=202_800,
        max_tokens=128_000,
    ))

    provider.chat_with_retry.assert_awaited_once()
    sent = provider.chat_with_retry.await_args.kwargs["max_tokens"]
    # room = 202800 - 50000 - 1024 = 151776; ceiling 128000 < room → 128000
    assert sent == 128_000


@pytest.mark.asyncio
async def test_dynamic_max_tokens_clamped_to_remaining_room(monkeypatch):
    """When the prompt is large, the sent max_tokens shrinks to what the
    window can still hold so the provider never gets input+output >
    window."""
    from durin.agent import runner as runner_mod
    from durin.agent.runner import AgentRunner, AgentRunSpec

    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_a, **_k: (160_000, "test"),
    )
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", tool_calls=[]),
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="glm-5.2",
        max_iterations=2,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=202_800,
        max_tokens=128_000,
    ))

    provider.chat_with_retry.assert_awaited_once()
    sent = provider.chat_with_retry.await_args.kwargs["max_tokens"]
    # room = 202800 - 160000 - 1024 = 41776 < ceiling 128000 → clamp
    assert sent == 41_776


@pytest.mark.asyncio
async def test_dynamic_max_tokens_uses_provider_ceiling_when_spec_unset(monkeypatch):
    """The real run path leaves ``spec.max_tokens`` unset — the output ceiling
    lives on ``provider.generation.max_tokens``. The clamp must still engage
    off that ceiling, or a capped input reservation lets input+output overflow
    the window (the regression this guards)."""
    from durin.agent import runner as runner_mod
    from durin.agent.runner import AgentRunner, AgentRunSpec

    # estimate 900k on a 1M window: under budget (966208) but room
    # (1_000_000 - 900_000 - 1024 = 98976) < provider ceiling (131072) → clamp.
    monkeypatch.setattr(
        runner_mod, "estimate_prompt_tokens_chain", lambda *_a, **_k: (900_000, "test"),
    )
    provider = MagicMock()
    provider.generation.max_tokens = 131_072  # the resolved catalog ceiling
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", tool_calls=[]),
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="glm-5.2",
        max_iterations=2,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=1_000_000,
        # max_tokens intentionally unset → mirrors loop.py / subagent.py
    ))

    provider.chat_with_retry.assert_awaited_once()
    sent = provider.chat_with_retry.await_args.kwargs["max_tokens"]
    assert sent == 98_976  # clamped to room, NOT the 131072 ceiling


# --------------------------------------------------------------------------
# B: emergency trim recovers a turn that would otherwise overflow
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emergency_trim_recovers_oversized_tool_result(monkeypatch):
    """A single oversized tool result that overflows must be trimmed on
    the model-facing copy and the turn proceeds (provider IS called),
    not aborted."""
    from durin.agent import runner as runner_mod
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)
    monkeypatch.setattr(runner_mod, "estimate_prompt_tokens_chain", _size_estimator)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="answer", tool_calls=[]),
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []

    huge = "X" * 400_000  # overflows; survives sanitize (big max_tool_result_chars)
    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[
            {"role": "user", "content": "look it up"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": huge},
        ],
        tools=tools,
        model="glm-5.2",
        max_iterations=2,
        max_tool_result_chars=10_000_000,
        context_window_tokens=100_000,
        max_tokens=2_000,
    ))

    provider.chat_with_retry.assert_awaited_once()
    assert result.stop_reason == "completed"
    assert result.final_content == "answer"
    assert [e for e in telemetry.events if e[0] == "mid_turn_precheck.recovered"]
    assert not [e for e in telemetry.events if e[0] == "mid_turn_precheck.overflow"]


# --------------------------------------------------------------------------
# A: unrecoverable overflow persists an overflow-specific placeholder
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unrecoverable_overflow_uses_overflow_placeholder(monkeypatch):
    """With nothing to trim and a prompt over budget, the turn aborts and
    the persisted assistant placeholder names the overflow — not a
    generic model error."""
    from durin.agent import runner as runner_mod
    from durin.agent.runner import (
        _PERSISTED_MODEL_ERROR_PLACEHOLDER,
        _PERSISTED_OVERFLOW_PLACEHOLDER,
        AgentRunner,
        AgentRunSpec,
    )

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)
    monkeypatch.setattr(
        runner_mod,
        "estimate_prompt_tokens_chain",
        lambda *_a, **_k: (500_000, "test"),
    )
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="never"))
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="glm-5.2",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        context_window_tokens=10_000,
        max_tokens=2_000,
    ))

    assert result.stop_reason == "mid_turn_precheck_overflow"
    provider.chat_with_retry.assert_not_awaited()
    assert _PERSISTED_OVERFLOW_PLACEHOLDER != _PERSISTED_MODEL_ERROR_PLACEHOLDER
    last = result.messages[-1]
    assert last.get("role") == "assistant"
    assert last.get("content") == _PERSISTED_OVERFLOW_PLACEHOLDER


# --------------------------------------------------------------------------
# Direct unit test of the emergency-trim helper
# --------------------------------------------------------------------------

def test_emergency_trim_helper_shrinks_largest_tool_result(monkeypatch):
    from durin.agent import runner as runner_mod
    from durin.agent.runner import AgentRunner, AgentRunSpec

    monkeypatch.setattr(runner_mod, "estimate_prompt_tokens_chain", _size_estimator)
    runner = AgentRunner(MagicMock())
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="m",
        max_iterations=1,
        max_tool_result_chars=10_000_000,
        context_window_tokens=100_000,
        max_tokens=2_000,
    )
    spec.tools.get_definitions.return_value = []
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "Y" * 80_000},
    ]
    trimmed, fit, new_estimate = runner._emergency_trim_for_budget(
        spec, messages, budget=5_000, provider=MagicMock(),
    )
    assert fit is True
    assert new_estimate <= 5_000
    # original list untouched (model-facing copy only)
    assert len(messages[2]["content"]) == 80_000
    assert len(trimmed[2]["content"]) < 80_000


def test_emergency_trim_helper_no_tool_results_cannot_recover():
    from durin.agent.runner import AgentRunner, AgentRunSpec

    runner = AgentRunner(MagicMock())
    spec = AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="m",
        max_iterations=1,
        max_tool_result_chars=10_000_000,
        context_window_tokens=100_000,
        max_tokens=2_000,
    )
    spec.tools.get_definitions.return_value = []
    messages = [{"role": "user", "content": "Z" * 80_000}]
    trimmed, fit, new_estimate = runner._emergency_trim_for_budget(
        spec, messages, budget=5_000, provider=MagicMock(),
    )
    assert fit is False
