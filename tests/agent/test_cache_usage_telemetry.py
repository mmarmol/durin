"""Tests for the ``cache.usage`` telemetry event emitted per turn.

The event surfaces prompt-cache savings — invisible by default at the
loguru-debug level. We log it as a structured telemetry event so users
can answer "how many tokens am I saving via provider-side caching?" by
grepping their telemetry file. Inspired by pi's perf review.
"""

from __future__ import annotations

import pytest

from durin.agent.hook import AgentHookContext
from durin.agent.progress_hook import AgentProgressHook


class _RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _make_context(usage: dict) -> AgentHookContext:
    ctx = AgentHookContext(iteration=3, messages=[])
    ctx.usage = usage
    return ctx


def _bind_logger(monkeypatch, logger):
    """Stub current_telemetry() in the progress_hook module."""
    from durin.agent import progress_hook
    monkeypatch.setattr(progress_hook, "current_telemetry", lambda: logger)


@pytest.mark.asyncio
async def test_emits_cache_usage_event_with_ratio(monkeypatch):
    logger = _RecordingLogger()
    _bind_logger(monkeypatch, logger)
    hook = AgentProgressHook()

    ctx = _make_context({
        "prompt_tokens": 5000,
        "cached_tokens": 4500,
        "completion_tokens": 300,
    })
    await hook.after_iteration(ctx)

    cache_events = [e for e in logger.events if e[0] == "cache.usage"]
    assert len(cache_events) == 1
    event_type, data = cache_events[0]
    assert data["iteration"] == 3
    assert data["prompt_tokens"] == 5000
    assert data["cached_tokens"] == 4500
    assert data["completion_tokens"] == 300
    # 4500/5000 = 90%
    assert data["cache_ratio_pct"] == 90.0


@pytest.mark.asyncio
async def test_emits_zero_ratio_when_provider_does_not_cache(monkeypatch):
    """For providers without caching (or cold cache), we still emit
    the event with ratio=0% — it's signal, not noise."""
    logger = _RecordingLogger()
    _bind_logger(monkeypatch, logger)
    hook = AgentProgressHook()

    ctx = _make_context({"prompt_tokens": 2000, "completion_tokens": 100})
    await hook.after_iteration(ctx)

    events = [e for e in logger.events if e[0] == "cache.usage"]
    assert len(events) == 1
    assert events[0][1]["cached_tokens"] == 0
    assert events[0][1]["cache_ratio_pct"] == 0.0


@pytest.mark.asyncio
async def test_skips_event_when_no_prompt_tokens(monkeypatch):
    """Empty/zero usage (e.g. an iteration that ended in an error
    before the LLM call) shouldn't emit a meaningless event."""
    logger = _RecordingLogger()
    _bind_logger(monkeypatch, logger)
    hook = AgentProgressHook()

    ctx = _make_context({})  # no fields
    await hook.after_iteration(ctx)

    cache_events = [e for e in logger.events if e[0] == "cache.usage"]
    assert cache_events == []


@pytest.mark.asyncio
async def test_no_emission_when_telemetry_unbound(monkeypatch):
    """When current_telemetry() returns None (no session-bound logger),
    we silently skip — never crash the hook on a missing dependency."""
    from durin.agent import progress_hook
    monkeypatch.setattr(progress_hook, "current_telemetry", lambda: None)
    hook = AgentProgressHook()

    # Just shouldn't raise.
    ctx = _make_context({"prompt_tokens": 100, "cached_tokens": 50})
    await hook.after_iteration(ctx)


@pytest.mark.asyncio
async def test_ratio_rounds_to_one_decimal(monkeypatch):
    logger = _RecordingLogger()
    _bind_logger(monkeypatch, logger)
    hook = AgentProgressHook()

    # 1234 / 5000 = 24.68 → rounds to 24.7
    ctx = _make_context({
        "prompt_tokens": 5000,
        "cached_tokens": 1234,
        "completion_tokens": 50,
    })
    await hook.after_iteration(ctx)

    events = [e for e in logger.events if e[0] == "cache.usage"]
    assert events[0][1]["cache_ratio_pct"] == 24.7
