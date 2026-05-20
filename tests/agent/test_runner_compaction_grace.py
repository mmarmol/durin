"""Compaction grace window (OpenClaw-inspired Tier 1).

When the outer LLM wall-clock timeout would have fired during active
consolidation, extend the deadline by one grace window. The LLM call is
likely slow *because* the context still needs to be reshaped; killing it
just to retry the same oversized prompt is wasted work.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

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


def _make_runner_spec(*, llm_timeout_s, is_compacting):
    from durin.agent.runner import AgentRunSpec
    provider = MagicMock()
    return provider, AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=MagicMock(),
        model="m",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        llm_timeout_s=llm_timeout_s,
        is_compacting=is_compacting,
        session_key="sess-1",
    )


@pytest.mark.asyncio
async def test_grace_extends_deadline_when_compacting(monkeypatch):
    """Base timeout fires; is_compacting() True → grace activates and the
    LLM call completes inside the extended window."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "5")

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    async def slow_llm():
        # Slower than base (0.05) but faster than base+grace (5+).
        await asyncio.sleep(0.15)
        return LLMResponse(content="ok", usage={})

    provider, spec = _make_runner_spec(
        llm_timeout_s=0.05,
        is_compacting=lambda: True,
    )
    runner = AgentRunner(provider)

    response = await runner._await_with_compaction_grace(
        slow_llm(), base_timeout=0.05, spec=spec,
    )
    assert response.content == "ok"

    grace_events = [e for e in telemetry.events if e[0] == "compaction.grace_extended"]
    assert len(grace_events) == 1
    payload = grace_events[0][1]
    assert payload["base_timeout_s"] == pytest.approx(0.05)
    assert payload["grace_s"] == 5.0
    assert payload["session_key"] == "sess-1"


@pytest.mark.asyncio
async def test_grace_not_applied_when_not_compacting(monkeypatch):
    """is_compacting() False at the moment of timeout → fail with the
    regular timeout; no grace event emitted."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "5")

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    async def slow_llm():
        await asyncio.sleep(2.0)  # would succeed if we waited grace
        return LLMResponse(content="ok", usage={})

    provider, spec = _make_runner_spec(
        llm_timeout_s=0.05,
        is_compacting=lambda: False,
    )
    runner = AgentRunner(provider)

    with pytest.raises(asyncio.TimeoutError):
        await runner._await_with_compaction_grace(
            slow_llm(), base_timeout=0.05, spec=spec,
        )

    grace_events = [e for e in telemetry.events if e[0] == "compaction.grace_extended"]
    assert grace_events == []


@pytest.mark.asyncio
async def test_grace_used_once_then_aborts(monkeypatch):
    """Even with compaction in flight, if the call still hasn't returned
    after grace expires, raise TimeoutError. Grace is one-shot."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "0.1")

    async def too_slow():
        await asyncio.sleep(5.0)
        return LLMResponse(content="never", usage={})

    provider, spec = _make_runner_spec(
        llm_timeout_s=0.05,
        is_compacting=lambda: True,
    )
    runner = AgentRunner(provider)

    with pytest.raises(asyncio.TimeoutError):
        await runner._await_with_compaction_grace(
            too_slow(), base_timeout=0.05, spec=spec,
        )


@pytest.mark.asyncio
async def test_is_compacting_callback_exception_does_not_break(monkeypatch):
    """A bug in the compaction-detection callback must not turn a timeout
    into a hang. Treat as 'not compacting' and fail normally."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "5")

    async def slow_llm():
        await asyncio.sleep(2.0)
        return LLMResponse(content="never", usage={})

    def boom():
        raise RuntimeError("compaction-detection went wrong")

    provider, spec = _make_runner_spec(
        llm_timeout_s=0.05,
        is_compacting=boom,
    )
    runner = AgentRunner(provider)

    with pytest.raises(asyncio.TimeoutError):
        await runner._await_with_compaction_grace(
            slow_llm(), base_timeout=0.05, spec=spec,
        )


@pytest.mark.asyncio
async def test_no_is_compacting_callback_no_grace(monkeypatch):
    """When the caller doesn't supply ``is_compacting``, no grace path is
    taken — preserve the old behavior."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "5")

    async def slow_llm():
        await asyncio.sleep(2.0)
        return LLMResponse(content="never", usage={})

    provider, spec = _make_runner_spec(llm_timeout_s=0.05, is_compacting=None)
    runner = AgentRunner(provider)

    with pytest.raises(asyncio.TimeoutError):
        await runner._await_with_compaction_grace(
            slow_llm(), base_timeout=0.05, spec=spec,
        )


@pytest.mark.asyncio
async def test_grace_disabled_when_env_set_to_zero(monkeypatch):
    """``DURIN_COMPACTION_GRACE_S=0`` disables the grace path even when
    compaction is in flight."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "0")

    async def slow_llm():
        await asyncio.sleep(2.0)
        return LLMResponse(content="never", usage={})

    provider, spec = _make_runner_spec(
        llm_timeout_s=0.05,
        is_compacting=lambda: True,
    )
    runner = AgentRunner(provider)

    with pytest.raises(asyncio.TimeoutError):
        await runner._await_with_compaction_grace(
            slow_llm(), base_timeout=0.05, spec=spec,
        )


def test_compaction_grace_seconds_reader(monkeypatch):
    """Default + override + garbage handling."""
    from durin.agent.runner import _compaction_grace_seconds

    monkeypatch.delenv("DURIN_COMPACTION_GRACE_S", raising=False)
    assert _compaction_grace_seconds() == 30.0

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "12.5")
    assert _compaction_grace_seconds() == 12.5

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "not-a-number")
    assert _compaction_grace_seconds() == 30.0

    monkeypatch.setenv("DURIN_COMPACTION_GRACE_S", "-5")
    assert _compaction_grace_seconds() == 0.0
