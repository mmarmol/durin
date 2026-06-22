"""Per-turn latency breakdown telemetry (`turn.latency`).

Splits a turn's wall-clock into the model round-trips (`llm_ms`), tool
execution (`tools_ms`), and everything else (`local_ms`) — plus the
per-state-machine durations — so a dashboard can answer "where did the 25s
go: the model or our local processing?".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.providers.base import LLMResponse


def _make_loop(tmp_path: Path) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")


@pytest.mark.asyncio
async def test_runner_accumulates_llm_ms(tmp_path):
    """The runner must record provider round-trip wall-clock in the result."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    async def _slow(*_a, **_k):
        await asyncio.sleep(0.02)
        return LLMResponse(content="ok", tool_calls=[])

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=_slow)
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="test-model",
        max_iterations=2,
        max_tool_result_chars=10_000,
    ))
    assert result.llm_ms >= 15.0  # ~20ms sleep, with slack


@pytest.mark.asyncio
async def test_turn_latency_event_emitted_with_breakdown(tmp_path, monkeypatch):
    loop = _make_loop(tmp_path)

    async def _slow_chat(*_a, **_k):
        await asyncio.sleep(0.02)
        return LLMResponse(content="Hi there.", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_slow_chat)
    loop.tools.get_definitions = MagicMock(return_value=[])

    events: list[tuple[str, dict]] = []

    class _Rec:
        def log(self, event_type, data=None):
            events.append((event_type, dict(data or {})))

    monkeypatch.setattr("durin.telemetry.logger.get_session_logger", lambda key: _Rec())

    msg = InboundMessage(channel="websocket", sender_id="u", chat_id="c", content="hi")
    await loop._process_message(msg)

    latency = [d for t, d in events if t == "turn.latency"]
    assert len(latency) == 1, "exactly one turn.latency event per turn"
    p = latency[0]
    assert {"total_ms", "llm_ms", "tools_ms", "local_ms", "states"} <= set(p)
    assert p["llm_ms"] >= 15.0, "model round-trip time captured"
    assert p["total_ms"] >= p["llm_ms"]
    # local + llm + tools reconciles to total (within rounding)
    assert abs(p["total_ms"] - (p["llm_ms"] + p["tools_ms"] + p["local_ms"])) < 1.0
    assert "RUN" in p["states"], "per-state durations present"
