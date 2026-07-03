"""Queue-by-default + explicit-steer semantics for messages arriving mid-turn.

Contract under test:
- A plain user message sent while a turn runs is DEFERRED: it is not
  injected at mid-turn checkpoints, only after the turn's final response.
- A steer (``metadata["steer"]`` or a literal ``[steer]`` prefix) and
  system-origin results (subagent / workflow completions) inject mid-turn.
- Steers are framed so the model knows they are mid-work guidance.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


def _make_loop(tmp_path):
    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager"), \
         patch("durin.agent.loop.SubagentManager") as MockSubMgr:
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    return loop


# ---------------------------------------------------------------------------
# Runner: which checkpoints drain with steer_only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mid_turn_checkpoint_drains_steer_only():
    """Checkpoint 1 (after tools) passes steer_only=True; the final drain doesn't."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    call_count = {"n": 0}

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="using tool",
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "x"})],
                usage={},
            )
        return LLMResponse(content="final answer", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="file content")

    seen_flags: list[bool] = []

    async def inject_cb(*, limit: int, steer_only: bool = False):
        seen_flags.append(steer_only)
        return []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=5,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        injection_callback=inject_cb,
    ))

    assert result.final_content == "final answer"
    # One mid-turn drain (steer_only) after the tool batch, one final drain.
    assert seen_flags == [True, False]


@pytest.mark.asyncio
async def test_deferred_user_message_waits_for_final_response():
    """A deferred message must not enter the turn mid-work, only after the answer."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    provider = MagicMock()
    call_count = {"n": 0}
    captured_messages: list[list[dict]] = []

    async def chat_with_retry(*, messages, **kwargs):
        call_count["n"] += 1
        captured_messages.append([dict(m) for m in messages])
        if call_count["n"] == 1:
            return LLMResponse(
                content="using tool",
                tool_calls=[ToolCallRequest(id="c1", name="read_file", arguments={"path": "x"})],
                usage={},
            )
        return LLMResponse(content=f"answer-{call_count['n']}", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="file content")

    async def inject_cb(*, limit: int, steer_only: bool = False):
        # Simulates the loop's two-queue drain: the user follow-up is only
        # released once the mid-turn restriction is lifted.
        if steer_only:
            return []
        if not inject_cb.delivered:
            inject_cb.delivered = True
            return [{"role": "user", "content": "queued follow-up"}]
        return []
    inject_cb.delivered = False

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        model="test-model",
        max_iterations=6,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        injection_callback=inject_cb,
    ))

    assert result.had_injections is True
    assert call_count["n"] == 3
    # Call 2 runs mid-turn (after tools): the queued follow-up must NOT be there.
    mid_turn_users = [
        m for m in captured_messages[1]
        if m.get("role") == "user" and m.get("content") == "queued follow-up"
    ]
    assert not mid_turn_users
    # Call 3 runs after the final response: now the follow-up is present.
    final_users = [
        m for m in captured_messages[2]
        if m.get("role") == "user" and m.get("content") == "queued follow-up"
    ]
    assert len(final_users) == 1


# ---------------------------------------------------------------------------
# Loop: _drain_pending two-queue semantics and steer framing
# ---------------------------------------------------------------------------

async def _capture_injection_callback(loop, session=None):
    """Run _run_agent_loop with a stubbed runner and capture its drain closure."""
    from types import SimpleNamespace

    injection_callback = None

    async def fake_runner_run(spec):
        nonlocal injection_callback
        injection_callback = spec.injection_callback
        return SimpleNamespace(
            stop_reason="done", final_content="done", error=None,
            tool_events=[], messages=[], usage={},
            had_injections=False, tools_used=[],
        )

    loop.runner.run = AsyncMock(side_effect=fake_runner_run)
    from durin.agent.loop import PendingQueues

    pending = PendingQueues.create()
    await loop._run_agent_loop(
        [{"role": "user", "content": "test"}],
        session=session,
        channel="test",
        chat_id="c1",
        pending_queues=pending,
    )
    assert injection_callback is not None
    return injection_callback, pending


@pytest.mark.asyncio
async def test_drain_pending_steer_only_leaves_deferred_queued(tmp_path):
    from durin.agent.loop import AgentLoop
    from durin.bus.events import InboundMessage
    from durin.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    callback, pending = await _capture_injection_callback(loop)

    await pending.inject.put(InboundMessage(
        channel="websocket", sender_id="u", chat_id="c1",
        content="go deeper on X", metadata={"steer": True},
    ))
    await pending.inject.put(InboundMessage(
        channel="system", sender_id="subagent", chat_id="c1",
        content="Sub-agent result",
    ))
    await pending.deferred.put(InboundMessage(
        channel="websocket", sender_id="u", chat_id="c1",
        content="also, unrelated question",
    ))

    mid_turn = await callback(steer_only=True)
    texts = [str(m["content"]) for m in mid_turn]
    assert len(mid_turn) == 2
    assert any("go deeper on X" in t for t in texts)
    assert any("Sub-agent result" in t for t in texts)
    assert all("unrelated question" not in t for t in texts)
    # The steer carries mid-work framing; the system result does not.
    from durin.agent.loop import _STEER_FRAMING
    steer_text = next(t for t in texts if "go deeper on X" in t)
    assert _STEER_FRAMING in steer_text
    system_text = next(t for t in texts if "Sub-agent result" in t)
    assert _STEER_FRAMING not in system_text
    # The deferred user message is still queued for the final drain.
    assert pending.deferred.qsize() == 1

    final = await callback(steer_only=False)
    assert len(final) == 1
    assert "unrelated question" in str(final[0]["content"])


@pytest.mark.asyncio
async def test_drain_pending_final_takes_inject_before_deferred(tmp_path):
    from durin.agent.loop import AgentLoop
    from durin.bus.events import InboundMessage
    from durin.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=tmp_path, model="test-model")

    callback, pending = await _capture_injection_callback(loop)

    await pending.deferred.put(InboundMessage(
        channel="websocket", sender_id="u", chat_id="c1", content="user question",
    ))
    await pending.inject.put(InboundMessage(
        channel="system", sender_id="workflow_background", chat_id="c1",
        content="Workflow finished",
    ))

    final = await callback()
    assert len(final) == 2
    assert "Workflow finished" in str(final[0]["content"])
    assert "user question" in str(final[1]["content"])


# ---------------------------------------------------------------------------
# Consumer: routing into inject vs deferred
# ---------------------------------------------------------------------------

async def _route_through_consumer(tmp_path, msg):
    """Publish *msg* while a session has active queues; return those queues."""
    from durin.agent.loop import PendingQueues

    loop = _make_loop(tmp_path)
    loop._dispatch = AsyncMock()  # type: ignore[method-assign]

    pending = PendingQueues.create()
    loop._pending_queues[msg.session_key] = pending

    run_task = asyncio.create_task(loop.run())
    await loop.bus.publish_inbound(msg)

    deadline = time.time() + 2
    while (pending.inject.empty() and pending.deferred.empty()
           and time.time() < deadline):
        await asyncio.sleep(0.01)

    loop.stop()
    await asyncio.wait_for(run_task, timeout=2)
    assert loop._dispatch.await_count == 0
    return pending


@pytest.mark.asyncio
async def test_consumer_routes_steer_metadata_to_inject(tmp_path):
    from durin.bus.events import InboundMessage

    pending = await _route_through_consumer(tmp_path, InboundMessage(
        channel="websocket", sender_id="u", chat_id="c",
        content="steer this", metadata={"steer": True},
    ))
    assert pending.deferred.empty()
    routed = pending.inject.get_nowait()
    assert routed.content == "steer this"


@pytest.mark.asyncio
async def test_consumer_normalizes_steer_prefix(tmp_path):
    """A literal [steer] prefix becomes the metadata flag, content stripped."""
    from durin.bus.events import InboundMessage

    pending = await _route_through_consumer(tmp_path, InboundMessage(
        channel="cli", sender_id="u", chat_id="c",
        content="[steer] focus on the tests",
    ))
    assert pending.deferred.empty()
    routed = pending.inject.get_nowait()
    assert routed.content == "focus on the tests"
    assert routed.metadata.get("steer") is True


@pytest.mark.asyncio
async def test_consumer_routes_system_result_to_inject(tmp_path):
    from durin.bus.events import InboundMessage

    pending = await _route_through_consumer(tmp_path, InboundMessage(
        channel="system", sender_id="workflow_background", chat_id="websocket:c",
        content="[Background workflow 'x' finished]",
        session_key_override="websocket:c",
    ))
    assert pending.deferred.empty()
    assert pending.inject.qsize() == 1


@pytest.mark.asyncio
async def test_consumer_emits_queued_ack_for_deferred_websocket_message(tmp_path):
    """Deferring a webui message publishes a _message_queued outbound ack."""
    from durin.bus.events import InboundMessage

    loop = _make_loop(tmp_path)
    loop._dispatch = AsyncMock()  # type: ignore[method-assign]

    from durin.agent.loop import PendingQueues
    msg = InboundMessage(
        channel="websocket", sender_id="u", chat_id="c",
        content="plain follow-up", metadata={"client_msg_id": "cm-1"},
    )
    pending = PendingQueues.create()
    loop._pending_queues[msg.session_key] = pending

    run_task = asyncio.create_task(loop.run())
    await loop.bus.publish_inbound(msg)

    deadline = time.time() + 2
    while pending.deferred.empty() and time.time() < deadline:
        await asyncio.sleep(0.01)

    loop.stop()
    await asyncio.wait_for(run_task, timeout=2)

    acks = []
    while not loop.bus.outbound.empty():
        out = await loop.bus.consume_outbound()
        if out.metadata.get("_message_queued"):
            acks.append(out)
    assert len(acks) == 1
    assert acks[0].metadata.get("client_msg_id") == "cm-1"
    assert acks[0].chat_id == "c"
