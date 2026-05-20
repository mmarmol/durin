"""Compaction lock aggregate timeout (OpenClaw-inspired Tier 2 A3).

Per-session compaction lock prevents two compactions running concurrently
on the same session. But if a compaction hung (e.g. the LLM provider got
stuck mid-summarize), the next user message's call to
``maybe_consolidate_by_tokens`` would wait on the lock forever — the
session lane silently dies.

A3 bounds the wait with ``DURIN_COMPACTION_LOCK_TIMEOUT_S`` (default 180s).
When the timeout expires, the call abandons the acquisition and returns
without consolidating. The next call may succeed if the original holder
finishes; meanwhile the user gets responses (with a potentially oversized
prompt) instead of an unbounded hang.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import durin.agent.memory as memory_module
from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMResponse


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    monkeypatch.setattr(memory_module, "current_telemetry", lambda: sink)


def _make_loop(tmp_path, *, context_window_tokens: int = 1000) -> AgentLoop:
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
        preemptive_compact_ratio=1.0,  # legacy trigger for these tests
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator._SAFETY_BUFFER = 0
    return loop


def test_lock_timeout_reader_default(monkeypatch):
    from durin.agent.memory import Consolidator
    monkeypatch.delenv("DURIN_COMPACTION_LOCK_TIMEOUT_S", raising=False)
    assert Consolidator._lock_timeout_s() == 180.0


def test_lock_timeout_reader_override(monkeypatch):
    from durin.agent.memory import Consolidator
    monkeypatch.setenv("DURIN_COMPACTION_LOCK_TIMEOUT_S", "30")
    assert Consolidator._lock_timeout_s() == 30.0


def test_lock_timeout_reader_garbage_falls_back(monkeypatch):
    from durin.agent.memory import Consolidator
    monkeypatch.setenv("DURIN_COMPACTION_LOCK_TIMEOUT_S", "not-a-number")
    assert Consolidator._lock_timeout_s() == 180.0


def test_lock_timeout_reader_zero_means_unbounded(monkeypatch):
    """Backwards-compat escape hatch: ``DURIN_COMPACTION_LOCK_TIMEOUT_S=0``
    disables the timeout — restores the original ``async with lock`` behaviour."""
    from durin.agent.memory import Consolidator
    monkeypatch.setenv("DURIN_COMPACTION_LOCK_TIMEOUT_S", "0")
    assert Consolidator._lock_timeout_s() == 0.0


@pytest.mark.asyncio
async def test_held_lock_times_out_and_skips_consolidation(tmp_path, monkeypatch):
    """When a prior consolidator holds the lock and never releases, the
    next call must time out within the configured window and return
    cleanly — no archive call, no exception."""
    monkeypatch.setenv("DURIN_COMPACTION_LOCK_TIMEOUT_S", "0.1")

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    loop = _make_loop(tmp_path, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))

    session = loop.sessions.get_or_create("cli:hung")
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    loop.sessions.save(session)

    # Hold the lock from a separate task.
    lock = loop.consolidator.get_lock(session.key)
    holder_ready = asyncio.Event()
    holder_release = asyncio.Event()

    async def _hold_lock():
        async with lock:
            holder_ready.set()
            await holder_release.wait()

    holder = asyncio.create_task(_hold_lock())
    await holder_ready.wait()

    try:
        # Force estimator to a value above trigger so the function would
        # have tried to compact if it had the lock.
        loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_: (500, "test")

        # This call must NOT hang — it should time out and return.
        await asyncio.wait_for(
            loop.consolidator.maybe_consolidate_by_tokens(session),
            timeout=2.0,
        )

        assert loop.consolidator.archive.await_count == 0
        timeouts = [e for e in telemetry.events if e[0] == "compaction.lock_timeout"]
        assert len(timeouts) == 1
        payload = timeouts[0][1]
        assert payload["session_key"] == session.key
        assert payload["timeout_s"] == 0.1
    finally:
        holder_release.set()
        await holder


@pytest.mark.asyncio
async def test_lock_released_after_normal_compaction(tmp_path, monkeypatch):
    """The replaced ``async with`` body must still release the lock on
    happy path — otherwise the FIRST compaction would block all future ones."""
    monkeypatch.setenv("DURIN_COMPACTION_LOCK_TIMEOUT_S", "5")

    loop = _make_loop(tmp_path, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=("summary", {"entities": [], "topics": []}))
    session = loop.sessions.get_or_create("cli:happy")
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]
    loop.sessions.save(session)

    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_: (10, "test")
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 1)

    # Two consecutive calls — second one must acquire freely.
    await loop.consolidator.maybe_consolidate_by_tokens(session)
    lock = loop.consolidator.get_lock(session.key)
    assert not lock.locked()

    await loop.consolidator.maybe_consolidate_by_tokens(session)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_lock_released_even_when_compaction_raises(tmp_path, monkeypatch):
    """Lock must be released even if the consolidation body raises an
    unexpected error — otherwise one buggy compaction would deadlock the
    session forever."""
    monkeypatch.setenv("DURIN_COMPACTION_LOCK_TIMEOUT_S", "5")

    loop = _make_loop(tmp_path, context_window_tokens=200)
    session = loop.sessions.get_or_create("cli:raises")
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
    ]
    loop.sessions.save(session)

    # Estimator above trigger so we enter the consolidation body.
    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_: (500, "test")
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    def _explode(*_a, **_kw):
        raise RuntimeError("compaction body went wrong")

    # Make ``archive`` raise — body raises during the for-loop.
    loop.consolidator.archive = AsyncMock(side_effect=_explode)

    with pytest.raises(RuntimeError):
        await loop.consolidator.maybe_consolidate_by_tokens(session)

    # Despite the raise, the lock is free for the next caller.
    lock = loop.consolidator.get_lock(session.key)
    assert not lock.locked()
