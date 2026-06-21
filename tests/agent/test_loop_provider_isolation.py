"""Per-turn provider isolation in AgentLoop.

Hazard #8 (docs/architecture/concurrency.md): the gateway runs a single shared
AgentLoop.  _apply_provider_snapshot mutates self.provider on every concurrent
session's /model swap.  AgentLoop must pass provider=self.provider into the
AgentRunSpec so the in-flight turn carries its own reference and is immune to a
concurrent swap of self.provider / self.runner.provider.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
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

    async def chat_stream_with_retry(
        self,
        *,
        messages,
        model=None,
        tools=None,
        on_content_delta=None,
        **kwargs,
    ):
        self.calls += 1
        if on_content_delta:
            await on_content_delta(f"done-{self.name}")
        return LLMResponse(content=f"done-{self.name}", tool_calls=[], usage={})

    async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                   temperature=0.7, reasoning_effort=None, tool_choice=None):
        self.calls += 1
        return LLMResponse(content=f"done-{self.name}", tool_calls=[], usage={})

    def get_default_model(self) -> str:
        return "test-model"


def _make_loop(tmp_path, provider: _RecordingProvider) -> AgentLoop:
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.execute = AsyncMock(return_value="ok")
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=False)  # type: ignore[method-assign]
    return loop


@pytest.mark.asyncio
async def test_spec_captures_provider_at_turn_start(tmp_path):
    """AgentRunSpec built by the loop must carry provider=self.provider at build time.

    We intercept runner.run, capture the spec, then swap self.provider after the
    spec has been built and assert the spec still holds the original provider.
    """
    p_a = _RecordingProvider("A")
    p_b = _RecordingProvider("B")

    loop = _make_loop(tmp_path, p_a)

    captured_spec = {}
    original_run = loop.runner.run

    async def intercepting_run(spec):
        captured_spec["provider"] = spec.provider
        # Simulate concurrent /model swap AFTER the spec was built but before run() finishes
        loop.provider = p_b
        loop.runner.provider = p_b
        return await original_run(spec)

    loop.runner.run = intercepting_run  # type: ignore[method-assign]

    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="hello")
    await loop._process_message(msg)

    assert "provider" in captured_spec, "runner.run must have been called"
    assert captured_spec["provider"] is p_a, (
        "spec.provider must be the provider active at turn start (p_a), "
        f"not the post-swap provider; got {captured_spec['provider']}"
    )


@pytest.mark.asyncio
async def test_mid_turn_swap_does_not_affect_in_flight_turn(tmp_path):
    """Swapping loop.provider mid-turn must not change the running turn's provider.

    p_a is the provider when the turn starts.  During the turn's progress_callback
    we swap loop.provider and loop.runner.provider to p_b (simulating a concurrent
    /model command).  All LLM calls within the turn must still go to p_a.
    """
    p_a = _RecordingProvider("A")
    p_b = _RecordingProvider("B")

    loop = _make_loop(tmp_path, p_a)

    swap_done = False

    async def on_stream(delta: str) -> None:
        nonlocal swap_done
        if not swap_done:
            loop.provider = p_b
            loop.runner.provider = p_b
            swap_done = True

    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content="hello")
    result = await loop._process_message(msg, on_stream=on_stream, on_stream_end=AsyncMock())

    assert result is not None
    assert p_a.calls > 0, "p_a must have served the in-flight turn"
    assert p_b.calls == 0, "p_b (post-swap provider) must NOT be used for this turn"
