"""Per-turn provider snapshot isolation.

Hazard #8 (docs/architecture/concurrency.md): the gateway runs a single shared
AgentRunner whose self.provider is mutated by _apply_provider_snapshot on each
concurrent session's /model swap. A turn must be immune to that mutation — it
must use the provider that was active when run() was called, not whatever
self.provider points to after a concurrent session has swapped it.

AgentRunSpec.provider (optional) carries the per-turn snapshot. resolver:
    provider = spec.provider or self.provider   # resolved once in run()
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.providers.base import GenerationSettings, LLMProvider, LLMResponse


class _RecordingProvider(LLMProvider):
    """Minimal stub that records how many times its chat methods are called."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.calls: int = 0
        self.generation = GenerationSettings(max_tokens=1024)

    async def chat_with_retry(self, *, messages, model=None, tools=None, **kwargs):
        self.calls += 1
        return LLMResponse(content=f"done-{self.name}", tool_calls=[], usage={})

    async def chat_stream_with_retry(self, *, messages, model=None, tools=None,
                                      on_content_delta=None, **kwargs):
        self.calls += 1
        if on_content_delta:
            await on_content_delta(f"done-{self.name}")
        return LLMResponse(content=f"done-{self.name}", tool_calls=[], usage={})

    # LLMProvider ABC requires these two concrete implementations
    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, tool_choice=None):
        self.calls += 1
        return LLMResponse(content=f"done-{self.name}", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"


def _make_tools() -> MagicMock:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")
    return tools


def _minimal_spec(**overrides) -> AgentRunSpec:
    defaults = dict(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=_make_tools(),
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=8192,
    )
    defaults.update(overrides)
    return AgentRunSpec(**defaults)


@pytest.mark.asyncio
async def test_spec_provider_used_not_default():
    """When spec.provider is set, ALL model calls go to it; self.provider gets 0 calls."""
    p_default = _RecordingProvider("default")
    p_turn = _RecordingProvider("turn")

    runner = AgentRunner(p_default)
    spec = _minimal_spec(provider=p_turn)

    result = await runner.run(spec)

    assert result.stop_reason in ("completed", "max_iterations")
    assert p_turn.calls > 0, "spec.provider must have been called"
    assert p_default.calls == 0, "self.provider must NOT be called when spec.provider is set"


@pytest.mark.asyncio
async def test_mid_flight_swap_does_not_affect_in_flight_turn():
    """Mutating runner.provider mid-turn must not change an in-flight turn's provider.

    We simulate a concurrent session's /model swap by replacing runner.provider
    inside a progress_callback that fires during the turn.  The turn was started
    with spec.provider=p_turn, so all model calls — including those after the
    swap — must still go to p_turn.
    """
    p_default = _RecordingProvider("default")
    p_turn = _RecordingProvider("turn")
    p_other = _RecordingProvider("other")

    runner = AgentRunner(p_default)

    swap_done = False

    async def _swap_callback(delta: str) -> None:
        nonlocal swap_done
        if not swap_done:
            # Simulate concurrent /model swap arriving mid-stream
            runner.provider = p_other
            swap_done = True

    spec = _minimal_spec(
        provider=p_turn,
        progress_callback=_swap_callback,
        stream_progress_deltas=True,
    )

    result = await runner.run(spec)

    assert result.stop_reason in ("completed", "max_iterations")
    assert p_turn.calls > 0, "spec.provider (p_turn) must be used throughout"
    assert p_other.calls == 0, "post-swap runner.provider must NOT be used for this turn"
    assert p_default.calls == 0, "original self.provider must NOT be used"


@pytest.mark.asyncio
async def test_no_spec_provider_falls_back_to_self():
    """When spec.provider is None, the runner uses self.provider (back-compat)."""
    p_default = _RecordingProvider("default")

    runner = AgentRunner(p_default)
    spec = _minimal_spec()  # no provider= kwarg → defaults to None

    result = await runner.run(spec)

    assert result.stop_reason in ("completed", "max_iterations")
    assert p_default.calls > 0, "self.provider must be used when spec.provider is None"
