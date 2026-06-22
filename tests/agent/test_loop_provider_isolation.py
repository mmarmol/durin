"""Per-turn provider isolation in AgentLoop.

The gateway runs a single shared AgentLoop.  _apply_provider_snapshot mutates
self.provider on every concurrent session's /model swap.  AgentLoop must pass
provider=self.provider into the AgentRunSpec so the in-flight turn carries its
own reference and is immune to a concurrent swap of self.provider /
self.runner.provider.
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
async def test_subagent_spec_captures_provider_at_spawn_time(tmp_path):
    """SubagentManager must pass provider= into AgentRunSpec at spawn time.

    set_provider() mutates manager.runner.provider (simulating a concurrent
    /model swap from another session).  The subagent turn that was already
    in flight must use the provider that was active when the spec was built,
    not the post-swap provider.

    This test verifies that WITHOUT the fix (not passing provider= into
    AgentRunSpec), p_b.calls would be > 0, so the assertion would fail —
    confirming the test is not vacuous.
    """
    from durin.agent.subagent import SubagentManager
    from durin.bus.queue import MessageBus

    p_a = _RecordingProvider("A")
    p_b = _RecordingProvider("B")

    manager = SubagentManager(
        provider=p_a,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=8192,
    )
    manager.runner.tools = MagicMock()
    manager.runner.tools.get_definitions = MagicMock(return_value=[])
    manager.runner.tools.execute = AsyncMock(return_value="ok")

    captured_provider: list = []
    original_run = manager.runner.run

    async def intercepting_run(spec):
        captured_provider.append(spec.provider)
        # Simulate a concurrent /model swap arriving while spec is built but run is starting
        manager.set_provider(p_b, "test-model")
        return await original_run(spec)

    manager.runner.run = intercepting_run  # type: ignore[method-assign]

    import time
    from durin.agent.subagent import SubagentStatus

    status = SubagentStatus(
        task_id="test-01",
        label="test",
        task_description="hello",
        started_at=time.monotonic(),
    )
    await manager._run_subagent(
        task_id="test-01",
        task="hello",
        label="test",
        origin={"channel": "cli", "chat_id": "c1", "session_key": None},
        status=status,
    )

    assert len(captured_provider) == 1, "runner.run must have been called once"
    assert captured_provider[0] is p_a, (
        "spec.provider must be the provider active at spec-build time (p_a), "
        f"not the post-swap provider; got {captured_provider[0]}"
    )
    assert p_b.calls == 0, "post-swap provider p_b must NOT have been used"
