"""End-to-end fault-injection tests for the Tier 1 + Tier 2 defensive guards.

The unit tests in ``tests/agent/`` cover each guard's *behavior* by mocking
the provider and recording telemetry events via an in-memory sink. What
they DON'T verify is the full pipeline from "guard trips" to "structured
JSON event lands on disk":

1. Guard logic decides to trip (unit-tested already).
2. ``current_telemetry()`` resolves the bound logger.
3. ``logger.log(event_type, data)`` serialises to JSON.
4. JSON line appended to the session's ``.jsonl`` file.
5. The on-disk file is the format dashboards / dream / external
   consumers actually read.

These E2E tests close that loop. Each test:

- Binds a real ``TelemetryLogger`` against a tmp file.
- Constructs an ``AgentRunner`` with a deterministic mock provider
  shaped to inject the specific failure.
- Runs the agent loop end-to-end.
- Reads the JSONL file BACK from disk and asserts the event is present
  with the documented payload schema.

The smoke test report flagged "some defensive triggers can't be smoke-tested
without fault injection" as a limitation. This module is that injection
harness. Unlike smoke (which exercises healthy load), every test here
ARMS the failure path explicitly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e_telemetry(tmp_path):
    """Bind a real ``TelemetryLogger`` writing to a tmp file and yield a
    helper that reads the events back. This is the harness signature
    every test in this module uses.

    Why a real logger (vs. a recording mock): the goal is to verify the
    JSON serialisation, file-handle flushing, and ContextVar binding
    work together — NOT just the guard's logic (which is unit-tested).
    A regression in any of those layers wouldn't be caught by a
    recording-sink unit test."""
    from durin.telemetry.logger import (
        TelemetryLogger,
        bind_telemetry,
        reset_telemetry,
    )

    path = tmp_path / "guard_events.jsonl"
    logger = TelemetryLogger(path)
    token = bind_telemetry(logger)

    def read_events() -> list[dict]:
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    yield read_events
    reset_telemetry(token)


def _events_of(read_fn: Callable[[], list[dict]], event_type: str) -> list[dict]:
    return [e for e in read_fn() if e["type"] == event_type]


def _make_runner_spec(**overrides):
    """Common AgentRunSpec scaffold. Subtests override fields per case."""
    from durin.agent.runner import AgentRunSpec
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = []
    tools.execute = AsyncMock(return_value="ok")
    defaults = dict(
        initial_messages=[{"role": "user", "content": "test"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        session_key="e2e-test",
    )
    defaults.update(overrides)
    return AgentRunSpec(**defaults)


def _timeout_response() -> LLMResponse:
    return LLMResponse(
        content="Error calling LLM: timed out",
        finish_reason="error",
        error_kind="timeout",
        usage={},
    )


# ---------------------------------------------------------------------------
# 1. Idle-timeout circuit breaker (Tier 1 2C)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_timeout_circuit_breaker_writes_event(e2e_telemetry):
    """Two consecutive provider timeouts with an injection bridging them
    trips the breaker. Event ``circuit_breaker.idle_timeout`` lands on
    disk with the documented payload."""
    from durin.agent.runner import AgentRunner

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        _timeout_response(),
        _timeout_response(),
    ])
    # One injection keeps the run alive past the first timeout so the
    # second one can trip the breaker.
    queue = [[{"role": "user", "content": "retry"}]]
    async def cb(**_):
        return queue.pop(0) if queue else []

    runner = AgentRunner(provider)
    spec = _make_runner_spec(injection_callback=cb)
    result = await runner.run(spec)

    assert result.stop_reason == "circuit_breaker_idle_timeout"
    events = _events_of(e2e_telemetry, "circuit_breaker.idle_timeout")
    assert len(events) == 1
    data = events[0]["data"]
    assert data["consecutive_timeouts"] == 2
    assert data["threshold"] == 1
    assert data["session_key"] == "e2e-test"


# ---------------------------------------------------------------------------
# 2. Mid-turn precheck overflow (Tier 2 A2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_turn_precheck_writes_overflow_event(e2e_telemetry, monkeypatch):
    """Force the estimator to report we'd exceed budget → runner aborts
    PRE-LLM-call with ``mid_turn_precheck_overflow`` stop_reason +
    ``mid_turn_precheck.overflow`` event."""
    from durin.agent.runner import AgentRunner
    from durin.agent import runner as runner_mod

    monkeypatch.setattr(
        runner_mod, "estimate_prompt_tokens_chain",
        lambda *_a, **_kw: (500_000, "test"),
    )

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="never"))
    runner = AgentRunner(provider)
    spec = _make_runner_spec(
        context_window_tokens=10_000,
        max_tokens=2_000,
    )
    result = await runner.run(spec)

    assert result.stop_reason == "mid_turn_precheck_overflow"
    provider.chat_with_retry.assert_not_awaited()  # blocked PRE-call
    events = _events_of(e2e_telemetry, "mid_turn_precheck.overflow")
    assert len(events) == 1
    data = events[0]["data"]
    assert data["estimated_tokens"] == 500_000
    assert data["budget_tokens"] > 0
    assert data["iteration"] == 0


# ---------------------------------------------------------------------------
# 3. Unknown-tool loop guard (Tier 2 B2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_loop_guard_writes_event(e2e_telemetry):
    """Three calls to a hallucinated tool name (over the default
    threshold of 2) → guard trips, event lands on disk."""
    from durin.agent.runner import AgentRunner

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="", tool_calls=[ToolCallRequest(id=f"c{i}", name="search_web", arguments={"q": f"v{i}"})], finish_reason="tool_calls", usage={})
        for i in range(3)
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["web_search"]  # real tool is web_search, not search_web

    runner = AgentRunner(provider)
    spec = _make_runner_spec(tools=tools)
    result = await runner.run(spec)

    assert result.stop_reason == "unknown_tool_loop_guard"
    events = _events_of(e2e_telemetry, "unknown_tool.loop_guard")
    assert len(events) == 1
    data = events[0]["data"]
    assert data["tool_name"] == "search_web"
    assert data["attempts"] == 3
    assert data["threshold"] == 2


# ---------------------------------------------------------------------------
# 4. Turn-budget enforced (Tier 1 2H)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_budget_enforced_writes_event(e2e_telemetry, monkeypatch, tmp_path):
    """Four tool results, each well under per-tool cap but aggregating
    over 100 KB → runner spills the largest to disk + emits
    ``turn_budget.enforced``."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "100000")

    provider = MagicMock()
    tool_calls = [ToolCallRequest(id=f"c{i}", name="big", arguments={}) for i in range(4)]
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="", tool_calls=tool_calls, finish_reason="tool_calls", usage={}),
        LLMResponse(content="done", tool_calls=[], usage={}),
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["big"]
    tools.execute = AsyncMock(side_effect=["X" * 60_000 for _ in range(4)])

    runner = AgentRunner(provider)
    spec = _make_runner_spec(
        tools=tools,
        # Per-tool cap high enough that the AGGREGATE is what catches us.
        max_tool_result_chars=500_000,
        workspace=tmp_path,
    )
    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    events = _events_of(e2e_telemetry, "turn_budget.enforced")
    assert len(events) == 1
    data = events[0]["data"]
    assert data["budget_chars"] == 100_000
    assert data["before_chars"] > data["budget_chars"]
    assert data["after_chars"] <= data["before_chars"]
    assert data["spilled_count"] >= 1
    assert data["tool_count"] == 4


# ---------------------------------------------------------------------------
# 5. Post-compaction loop guard (Tier 2 C2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_compaction_loop_writes_event(e2e_telemetry):
    """Arm the guard, then run a session where the model repeatedly
    invokes the same (name, args, result) — the guard trips on the
    3rd identical observation."""
    from durin.agent.runner import AgentRunner
    from durin.utils.post_compaction_guard import PostCompactionLoopGuard

    guard = PostCompactionLoopGuard(window_size=3)
    guard.arm("e2e-test")

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(content="", tool_calls=[ToolCallRequest(id=f"c{i}", name="read", arguments={"path": "x.py"})], finish_reason="tool_calls", usage={})
        for i in range(3)
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["read"]
    tools.execute = AsyncMock(return_value="same content")  # identical result

    runner = AgentRunner(provider)
    spec = _make_runner_spec(tools=tools, post_compaction_guard=guard)
    result = await runner.run(spec)

    assert result.stop_reason == "post_compaction_loop"
    events = _events_of(e2e_telemetry, "post_compaction_loop.tripped")
    assert len(events) == 1
    data = events[0]["data"]
    assert data["tool_name"] == "read"
    assert data["repeat_count"] == 3


# ---------------------------------------------------------------------------
# 6. History media prune (Tier 2 B3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_media_prune_writes_event(e2e_telemetry):
    """A session pre-populated with images in old turns → the runner's
    sanitize pipeline removes them and emits ``history_media.pruned``."""
    from durin.agent.runner import AgentRunner

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[], usage={}))
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = []

    # 4 completed turns where the first has an image — preserve_turns=3
    # default → the oldest image is prune-eligible.
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVB..."}}
    initial = []
    for i in range(4):
        initial.append({"role": "user", "content": [image, {"type": "text", "text": f"u{i}"}]})
        initial.append({"role": "assistant", "content": f"a{i}"})
    initial.append({"role": "user", "content": "now go"})

    runner = AgentRunner(provider)
    spec = _make_runner_spec(initial_messages=initial, tools=tools)
    result = await runner.run(spec)

    assert result.stop_reason == "completed"
    events = _events_of(e2e_telemetry, "history_media.pruned")
    assert len(events) >= 1
    data = events[0]["data"]
    assert data["image_blocks_removed"] >= 1
    assert data["audio_blocks_removed"] == 0
    assert data["preserve_turns"] == 3
    assert data["session_key"] == "e2e-test"


# ---------------------------------------------------------------------------
# 7. Tool-call argument repair (Tier 2 B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_argument_repair_writes_event(e2e_telemetry):
    """An HTML-entity-encoded JSON string for tool args triggers the
    repair pre-processor. The event records the repair tokens.

    This test verifies the *helper* layer (utils/tool_argument_repair.py)
    end-to-end with the real telemetry logger, since it's the cleanest
    way to provoke the trip without a full provider streaming setup."""
    from durin.utils.tool_argument_repair import parse_tool_call_arguments

    result = parse_tool_call_arguments(
        '{&quot;name&quot;:&quot;list_dir&quot;,&quot;path&quot;:&quot;.&quot;}'
    )
    assert result == {"name": "list_dir", "path": "."}

    events = _events_of(e2e_telemetry, "tool_call.argument_repair")
    assert len(events) == 1
    data = events[0]["data"]
    assert "html_unescape" in data["repairs"]
    assert data["parsed_ok"] is True


# ---------------------------------------------------------------------------
# 8. Compaction lock timeout (Tier 2 A3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compaction_lock_timeout_writes_event(e2e_telemetry, tmp_path, monkeypatch):
    """When the per-session compaction lock is held by another task
    longer than ``DURIN_COMPACTION_LOCK_TIMEOUT_S``, the next call
    aborts acquisition with a ``compaction.lock_timeout`` event."""
    monkeypatch.setenv("DURIN_COMPACTION_LOCK_TIMEOUT_S", "0.1")

    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus
    from durin.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    _resp = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=_resp)
    provider.chat_stream_with_retry = AsyncMock(return_value=_resp)

    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path,
        model="test-model", context_window_tokens=200,
        preemptive_compact_ratio=1.0,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator._SAFETY_BUFFER = 0

    session = loop.sessions.get_or_create("e2e-test")
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    loop.sessions.save(session)

    # Hold the lock from a separate task so the consolidator call below
    # has to time out trying to acquire.
    lock = loop.consolidator.get_lock(session.key)
    holder_ready = asyncio.Event()
    holder_release = asyncio.Event()

    async def _hold():
        async with lock:
            holder_ready.set()
            await holder_release.wait()

    holder = asyncio.create_task(_hold())
    await holder_ready.wait()
    try:
        # Estimator says above trigger so it WOULD try to compact if it
        # could grab the lock.
        loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_: (500, "test")
        await asyncio.wait_for(
            loop.consolidator.maybe_consolidate_by_tokens(session),
            timeout=2.0,
        )
    finally:
        holder_release.set()
        await holder

    events = _events_of(e2e_telemetry, "compaction.lock_timeout")
    assert len(events) == 1
    assert events[0]["data"]["session_key"] == session.key
    assert events[0]["data"]["timeout_s"] == 0.1


# ---------------------------------------------------------------------------
# 9. Pre-emptive compaction trigger (Tier 2 A1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preemptive_compaction_writes_event(e2e_telemetry, tmp_path, monkeypatch):
    """When ``estimated > preemptive_compact_ratio * window`` AND
    ``estimated < input_budget`` (the pre-emptive trigger fires below
    the hard wall), the consolidator emits
    ``compaction.preemptive_trigger`` for visibility."""
    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus
    from durin.providers.base import GenerationSettings
    import durin.agent.memory as memory_module

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="summary"))
    provider.chat_stream_with_retry = AsyncMock(return_value=LLMResponse(content="summary"))

    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path,
        model="test-model", context_window_tokens=1000,
        # ratio 0.4 → trigger at 400 tokens
        preemptive_compact_ratio=0.4,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    # max_completion=0 + safety=50 → budget = 950; trigger = 400.
    loop.consolidator._SAFETY_BUFFER = 50

    session = loop.sessions.get_or_create("e2e-test")
    session.messages = [
        {"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"}, {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
    ]
    loop.sessions.save(session)

    # First estimate ABOVE trigger (500 > 400) but BELOW budget (500 < 950).
    # After one archive it drops below trigger.
    estimates = iter([500, 100])
    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_: (next(estimates), "test")
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)
    loop.consolidator.archive = AsyncMock(return_value="A summary.")

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    events = _events_of(e2e_telemetry, "compaction.preemptive_trigger")
    assert len(events) == 1
    data = events[0]["data"]
    assert data["session_key"] == session.key
    assert data["estimated_tokens"] == 500
    assert data["trigger_tokens"] == 400
    assert data["ratio"] == 0.4


# ---------------------------------------------------------------------------
# Aggregate sanity check
# ---------------------------------------------------------------------------


def test_all_guards_register_a_typeddict_in_schema():
    """Every event this module asserts on must also be in the catalog.
    Catches the case where a guard is wired but its TypedDict registration
    was forgotten — the schema-catalog meta-test would catch it too, but
    surfacing the failure here gives a clearer 'this E2E test won't
    correlate to a documented event' diagnostic."""
    from durin.telemetry.schema import EVENTS

    required = {
        "circuit_breaker.idle_timeout",
        "mid_turn_precheck.overflow",
        "unknown_tool.loop_guard",
        "turn_budget.enforced",
        "post_compaction_loop.tripped",
        "history_media.pruned",
        "tool_call.argument_repair",
        "compaction.lock_timeout",
        "compaction.preemptive_trigger",
    }
    missing = required - set(EVENTS)
    assert not missing, f"E2E tests assert on events missing from EVENTS: {sorted(missing)}"
