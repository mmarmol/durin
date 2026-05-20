"""Pre-emptive compaction trigger (OpenClaw-inspired Tier 2 A1).

Old behaviour: consolidator only fires when estimated_tokens exceeded the
input budget (``context_window - max_completion - safety``) — i.e. we
waited for the context wall before doing anything. Many turns shipped
huge prompts up to ~93% of the window before the first compaction.

New behaviour: consolidator fires when estimated_tokens exceeds
``preemptive_compact_ratio * context_window`` (default 0.5). Per-preset:
a 1M-window model wants ~0.15 (compact at 150K — paying for every token
shipped, you don't want to wait until 500K). Configured in
``ModelPresetConfig.preemptive_compact_ratio`` (per-model) or
``AgentDefaults.preemptive_compact_ratio`` (fallback). The
``consolidation_ratio`` now means "fraction of trigger threshold to
keep after compaction" so each round does substantial work.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import durin.agent.memory as memory_module
from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.config.schema import AgentDefaults, ModelPresetConfig
from durin.providers.base import GenerationSettings, LLMResponse


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    monkeypatch.setattr(memory_module, "current_telemetry", lambda: sink)


def _make_loop(
    tmp_path,
    *,
    context_window_tokens: int,
    consolidation_ratio: float = 0.5,
    preemptive_compact_ratio: float = 0.5,
) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    _resp = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=_resp)
    provider.chat_stream_with_retry = AsyncMock(return_value=_resp)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=context_window_tokens,
        consolidation_ratio=consolidation_ratio,
        preemptive_compact_ratio=preemptive_compact_ratio,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator._SAFETY_BUFFER = 0
    return loop


def _session_with_messages(loop: AgentLoop, count: int):
    session = loop.sessions.get_or_create("cli:test")
    session.messages = []
    for i in range(count):
        session.messages.append({"role": "user", "content": f"u{i}"})
        session.messages.append({"role": "assistant", "content": f"a{i}"})
    loop.sessions.save(session)
    return session


def _stub_consolidator(*, window: int, max_completion: int, safety: int, ratio: float):
    """Build a barebones Consolidator without the full LLM provider plumbing."""
    from durin.agent.memory import Consolidator
    c = Consolidator.__new__(Consolidator)
    c.context_window_tokens = window
    c.max_completion_tokens = max_completion
    c.preemptive_compact_ratio = ratio
    # ``_SAFETY_BUFFER`` is a class constant on the real class; set as
    # instance attribute here for direct override.
    c._SAFETY_BUFFER = safety
    # Patch the property's class attribute by assigning to a fresh class.
    return c


def test_threshold_property_uses_ratio(monkeypatch):
    """``_preemptive_trigger_tokens`` = ``window * ratio``, clamped by
    the input token budget so a misconfigured 0.99 ratio doesn't disable
    the hard ceiling."""
    from durin.agent.memory import Consolidator
    c = _stub_consolidator(window=1_000_000, max_completion=8192, safety=1024, ratio=0.15)
    monkeypatch.setattr(Consolidator, "_SAFETY_BUFFER", 1024, raising=False)
    assert c._preemptive_trigger_tokens == 150_000


def test_threshold_clamped_by_input_budget(monkeypatch):
    """A 0.99 ratio on a small window still respects the budget ceiling."""
    from durin.agent.memory import Consolidator
    monkeypatch.setattr(Consolidator, "_SAFETY_BUFFER", 100, raising=False)
    c = _stub_consolidator(window=1000, max_completion=200, safety=100, ratio=0.99)
    # window * 0.99 = 990, but budget = 1000 - 200 - 100 = 700, so capped at 700.
    assert c._preemptive_trigger_tokens == 700


def test_threshold_falls_back_to_budget_on_invalid_ratio(monkeypatch):
    """Garbage ratio (0, negative, non-numeric) → legacy budget-only trigger."""
    from durin.agent.memory import Consolidator
    monkeypatch.setattr(Consolidator, "_SAFETY_BUFFER", 100, raising=False)

    c = _stub_consolidator(window=1000, max_completion=200, safety=100, ratio=0)
    assert c._preemptive_trigger_tokens == 700

    c.preemptive_compact_ratio = -0.5
    assert c._preemptive_trigger_tokens == 700


@pytest.mark.asyncio
async def test_preemptive_trigger_fires_below_input_budget(tmp_path, monkeypatch):
    """With ratio=0.5 on a 200-token window and budget=200 (no safety/completion),
    the trigger is 100. estimated=150 fires consolidation — old behaviour would
    have waited for >200."""
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    loop = _make_loop(
        tmp_path,
        context_window_tokens=200,
        preemptive_compact_ratio=0.5,
        consolidation_ratio=0.5,
    )
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    session = _session_with_messages(loop, count=10)

    estimates = [150, 40]  # 150 > trigger(100); after archive, 40 <= target(50)
    def mock_estimate(_session, *, session_summary=None):
        return (estimates.pop(0), "test")
    loop.consolidator.estimate_session_prompt_tokens = mock_estimate
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert loop.consolidator.archive.await_count == 1

    preempt_events = [e for e in telemetry.events if e[0] == "compaction.preemptive_trigger"]
    assert len(preempt_events) == 1
    payload = preempt_events[0][1]
    assert payload["trigger_tokens"] == 100
    assert payload["estimated_tokens"] == 150
    assert payload["ratio"] == 0.5


@pytest.mark.asyncio
async def test_below_trigger_skips_consolidation(tmp_path, monkeypatch):
    """estimated < trigger → no archive, no telemetry."""
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    loop = _make_loop(
        tmp_path,
        context_window_tokens=200,
        preemptive_compact_ratio=0.5,  # trigger = 100
    )
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    session = _session_with_messages(loop, count=5)

    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_: (80, "test")
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert loop.consolidator.archive.await_count == 0
    assert [e for e in telemetry.events if e[0] == "compaction.preemptive_trigger"] == []


def test_per_preset_ratio_field_default_is_none():
    """ModelPresetConfig.preemptive_compact_ratio defaults to None — preset
    inherits from AgentDefaults.preemptive_compact_ratio when unset."""
    preset = ModelPresetConfig(model="some-model")
    assert preset.preemptive_compact_ratio is None


def test_per_preset_ratio_field_accepts_override():
    preset = ModelPresetConfig(model="big", preemptiveCompactRatio=0.15)
    assert preset.preemptive_compact_ratio == 0.15
    preset2 = ModelPresetConfig(model="big", preemptive_compact_ratio=0.2)
    assert preset2.preemptive_compact_ratio == 0.2


def test_agent_defaults_ratio_default_is_half():
    defaults = AgentDefaults()
    assert defaults.preemptive_compact_ratio == 0.5


def test_agent_defaults_ratio_validation_range():
    """Below 0.05 or above 0.99 is rejected — protects against accidental
    values like 0 (would disable trigger) or 1.0 (==budget = old broken
    behaviour)."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AgentDefaults(preemptive_compact_ratio=0.0)
    with pytest.raises(ValidationError):
        AgentDefaults(preemptive_compact_ratio=1.0)


def test_set_provider_applies_per_preset_ratio(tmp_path):
    """Switching presets through set_provider with a new ratio updates the
    consolidator's threshold for future turns."""
    loop = _make_loop(tmp_path, context_window_tokens=200, preemptive_compact_ratio=0.5)
    assert loop.consolidator.preemptive_compact_ratio == 0.5

    loop.consolidator.set_provider(
        loop.provider, "new-model", 1_000_000,
        preemptive_compact_ratio=0.15,
    )
    assert loop.consolidator.preemptive_compact_ratio == 0.15
    assert loop.consolidator.context_window_tokens == 1_000_000


def test_set_provider_without_explicit_ratio_preserves_current(tmp_path):
    """set_provider with preemptive_compact_ratio=None leaves the current
    value untouched (the global default → preset case where preset doesn't
    override)."""
    loop = _make_loop(tmp_path, context_window_tokens=200, preemptive_compact_ratio=0.3)
    loop.consolidator.set_provider(loop.provider, "x", 500)
    assert loop.consolidator.preemptive_compact_ratio == 0.3
