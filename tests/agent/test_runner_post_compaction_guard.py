"""Runner integration with post-compaction loop guard (Tier 2 C2).

Unit tests for the guard itself live in ``tests/utils/test_post_compaction_guard.py``.
This file verifies the wiring: AgentRunner observes each tool call
through ``spec.post_compaction_guard.observe`` and terminates the turn
with ``stop_reason="post_compaction_loop"`` when the guard trips.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest
from durin.utils.post_compaction_guard import PostCompactionLoopGuard

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    from durin.agent import runner as runner_mod
    monkeypatch.setattr(runner_mod, "current_telemetry", lambda: sink)


@pytest.mark.asyncio
async def test_runner_aborts_when_guard_trips(monkeypatch):
    """3 identical (name, args, result) triples post-compaction →
    runner returns ``stop_reason="post_compaction_loop"`` and the
    matching telemetry event fires."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    guard = PostCompactionLoopGuard(window_size=3)
    guard.arm("sess-1")

    provider = MagicMock()
    # The model keeps calling the same tool with same args every iteration.
    same_tool_call = ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"})
    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{i}", name="read_file", arguments={"path": "a.py"})],
            finish_reason="tool_calls",
            usage={},
        )
        for i in range(3)
    ]
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["read_file"]
    # Tool returns the SAME content every time — that's the loop.
    tools.execute = AsyncMock(return_value="same content")

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "read it"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        session_key="sess-1",
        post_compaction_guard=guard,
    ))

    assert result.stop_reason == "post_compaction_loop"
    assert "post-compaction" in result.error.lower()
    assert "read_file" in result.error

    events = [e for e in telemetry.events if e[0] == "post_compaction_loop.tripped"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["tool_name"] == "read_file"
    assert payload["repeat_count"] == 3
    assert payload["session_key"] == "sess-1"


@pytest.mark.asyncio
async def test_unarmed_guard_does_not_interfere(monkeypatch):
    """When the guard exists but was never armed, the runner ignores it."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    guard = PostCompactionLoopGuard(window_size=3)
    # Do NOT arm — simulate a session that hasn't compacted yet.

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "a.py"})],
            finish_reason="tool_calls",
            usage={},
        ),
        LLMResponse(content="done", tool_calls=[], usage={}),
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["read_file"]
    tools.execute = AsyncMock(return_value="content")

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        session_key="sess-2",
        post_compaction_guard=guard,
    ))

    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_guard_failure_does_not_break_turn(monkeypatch):
    """If ``observe`` raises (defensive — shouldn't happen with the real
    guard), the runner must log and continue, not abort the turn."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    broken = MagicMock()
    broken.observe = MagicMock(side_effect=RuntimeError("guard exploded"))

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=[
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={})],
            finish_reason="tool_calls",
            usage={},
        ),
        LLMResponse(content="done", tool_calls=[], usage={}),
    ])
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.tool_names = ["read_file"]
    tools.execute = AsyncMock(return_value="content")

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="test-model",
        max_iterations=10,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        session_key="sess-3",
        post_compaction_guard=broken,
    ))

    # Turn completes normally despite the guard raising.
    assert result.stop_reason == "completed"
    assert result.final_content == "done"


@pytest.mark.asyncio
async def test_consolidator_arms_guard_after_successful_compaction(tmp_path):
    """End-to-end: when ``Consolidator.maybe_consolidate_by_tokens``
    completes with at least one summary, the post-compaction guard for
    that session is armed."""
    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus
    from durin.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="A summary.", tool_calls=[],
    ))
    provider.chat_stream_with_retry = AsyncMock(return_value=LLMResponse(content="A summary."))

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=200,
        preemptive_compact_ratio=1.0,  # legacy semantics for this fixture
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator._SAFETY_BUFFER = 0

    session = loop.sessions.get_or_create("sess-arm")
    session.messages = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]
    loop.sessions.save(session)

    # Force estimator above budget so consolidation runs.
    loop.consolidator.estimate_session_prompt_tokens = lambda _s, **_: (500, "test")
    from unittest.mock import patch

    import durin.agent.memory as memory_module
    with patch.object(memory_module, "estimate_message_tokens", lambda _m: 100):
        await loop.consolidator.maybe_consolidate_by_tokens(session)

    # Guard armed for sess-arm.
    assert loop.consolidator.post_compaction_guard.is_armed("sess-arm")
