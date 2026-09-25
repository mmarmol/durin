"""Loop interception for blocking ask_user: the user's next plain-text
message resolves the in-turn waiter instead of dispatching a new turn."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent import pending_answers as pa
from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMResponse


@pytest.fixture(autouse=True)
def _clean_registry():
    pa.reset()
    yield
    pa.reset()


def _make_loop(tmp_path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    _resp = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=_resp)
    provider.chat_stream_with_retry = AsyncMock(return_value=_resp)
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )


def _msg(content: str, *, media: list[str] | None = None) -> InboundMessage:
    return InboundMessage(
        channel="websocket",
        sender_id="u",
        chat_id="42",
        content=content,
        media=media or [],
    )


@pytest.mark.asyncio
async def test_plain_text_answer_is_consumed(tmp_path):
    loop = _make_loop(tmp_path)
    key = loop._effective_session_key(_msg("x"))
    fut = pa.create(key)
    consumed = loop._maybe_resolve_pending_answer(_msg("green"), key)
    assert consumed is True
    assert fut.result() == "green"


@pytest.mark.asyncio
async def test_slash_command_is_not_consumed(tmp_path):
    loop = _make_loop(tmp_path)
    key = loop._effective_session_key(_msg("x"))
    fut = pa.create(key)
    consumed = loop._maybe_resolve_pending_answer(_msg("/status"), key)
    assert consumed is False
    assert not fut.done()
    fut.cancel()


@pytest.mark.asyncio
async def test_media_reply_forces_fallback_and_routes_normally(tmp_path):
    loop = _make_loop(tmp_path)
    key = loop._effective_session_key(_msg("x"))
    fut = pa.create(key)
    consumed = loop._maybe_resolve_pending_answer(
        _msg("see attached", media=["/tmp/img.png"]), key,
    )
    # Not consumed: the message must continue through normal routing, but
    # the waiter is told to fall back to yield semantics.
    assert consumed is False
    assert fut.result() is pa.FALLBACK


@pytest.mark.asyncio
async def test_no_waiter_means_no_consumption(tmp_path):
    loop = _make_loop(tmp_path)
    key = loop._effective_session_key(_msg("x"))
    assert loop._maybe_resolve_pending_answer(_msg("hello"), key) is False


@pytest.mark.asyncio
async def test_yes_resolves_an_approval_waiter_as_approve(tmp_path):
    loop = _make_loop(tmp_path)
    key = loop._effective_session_key(_msg("x"))
    fut = pa.create(key, kind="approval", ref="r1")
    assert loop._maybe_resolve_pending_answer(_msg("sí"), key) is True
    assert await fut == "approve"


@pytest.mark.asyncio
async def test_non_verdict_reply_falls_back_and_continues(tmp_path):
    loop = _make_loop(tmp_path)
    key = loop._effective_session_key(_msg("x"))
    fut = pa.create(key, kind="approval", ref="r1")
    # «sí pero cambiá X» is not a verdict: the approval stays pending and the
    # message goes on as a normal message.
    assert loop._maybe_resolve_pending_answer(_msg("sí pero cambiá X"), key) is False
    assert await fut is pa.FALLBACK


@pytest.mark.asyncio
async def test_a_notice_posted_for_the_user_is_not_the_answer(tmp_path):
    """A stored-secret notice rides the chat like a user message but is not
    the user's reply: the question keeps waiting for the real answer."""
    from durin.bus.events import INBOUND_META_NOT_AN_ANSWER

    loop = _make_loop(tmp_path)
    key = loop._effective_session_key(_msg("x"))
    fut = pa.create(key)
    notice = InboundMessage(
        channel="websocket", sender_id="u", chat_id="42",
        content="The user stored the secret 'GH_TOKEN' (service=github, scope=exec). "
                "Please continue the task.",
        metadata={"webui": True, INBOUND_META_NOT_AN_ANSWER: True},
    )

    assert loop._maybe_resolve_pending_answer(notice, key) is False
    assert not fut.done()
    assert loop._maybe_resolve_pending_answer(_msg("green"), key) is True
    assert fut.result() == "green"


# What the three in-process publishers put on the bus under the parent chat's
# session key: a sub-agent's result, a background workflow's result and an
# automation's outcome. Each is posted by durin, never typed by the person.
_SYSTEM_RESULTS = [
    pytest.param("system", {"injected_event": "subagent_result", "subagent_task_id": "t1"},
                 "[Subagent 'x' completed]\n\nyes", id="subagent"),
    pytest.param("system", {"injected_event": "workflow_background_result", "workflow": "w"},
                 "[Background workflow 'w' finished]\n\nyes", id="workflow"),
    pytest.param("system", {"injected_event": "automation_outcome", "automation": "a"},
                 "[Automation 'a' finished]\n\nyes", id="automation"),
    # The injected-event marker alone is enough, whatever channel carries it.
    pytest.param("websocket", {"injected_event": "subagent_result"}, "yes",
                 id="injected-on-a-chat-channel"),
]


def _system_msg(channel: str, metadata: dict, content: str) -> InboundMessage:
    return InboundMessage(
        channel=channel, sender_id="subagent", chat_id="websocket:42", content=content,
        metadata=dict(metadata), session_key_override="websocket:42",
    )


@pytest.mark.parametrize(("channel", "metadata", "content"), _SYSTEM_RESULTS)
@pytest.mark.asyncio
async def test_a_system_result_leaves_an_approval_waiting(tmp_path, channel, metadata, content):
    loop = _make_loop(tmp_path)
    fut = pa.create("websocket:42", kind="approval", ref="r1")
    assert loop._maybe_resolve_pending_answer(
        _system_msg(channel, metadata, content), "websocket:42") is False
    # Neither decided nor told to fall back: the card stays up and the
    # person can still answer it.
    assert not fut.done()
    assert pa.waiting_kind("websocket:42") == "approval"
    assert loop._maybe_resolve_pending_answer(_msg("yes"), "websocket:42") is True
    assert await fut == "approve"


@pytest.mark.parametrize(("channel", "metadata", "content"), _SYSTEM_RESULTS)
@pytest.mark.asyncio
async def test_a_system_result_never_answers_a_question(tmp_path, channel, metadata, content):
    loop = _make_loop(tmp_path)
    fut = pa.create("websocket:42")
    assert loop._maybe_resolve_pending_answer(
        _system_msg(channel, metadata, content), "websocket:42") is False
    assert not fut.done()
    assert loop._maybe_resolve_pending_answer(_msg("green"), "websocket:42") is True
    assert await fut == "green"


@pytest.mark.asyncio
async def test_a_system_result_during_a_wait_routes_into_the_running_turn(tmp_path):
    """The consumer does not take the result as the answer: it goes where a
    system result always goes while a turn runs, the turn's inject queue."""
    import asyncio

    loop = _make_loop(tmp_path)
    waiting = asyncio.Event()
    release = asyncio.Event()
    waiter: list[asyncio.Future] = []

    async def fake_dispatch(msg, *args, **kwargs):
        # A turn blocked on an approval card.
        waiter.append(pa.create("websocket:42", kind="approval", ref="r1"))
        waiting.set()
        await release.wait()

    async def _noop(*_a, **_kw):
        return None

    loop._dispatch = fake_dispatch  # type: ignore[method-assign]
    loop._connect_mcp = _noop  # type: ignore[method-assign]
    loop._warmup_memory_embedding = _noop  # type: ignore[method-assign]
    runner = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(_msg("delete the old logs"))
        await asyncio.wait_for(waiting.wait(), 5)
        await loop.bus.publish_inbound(_system_msg(
            "system", {"injected_event": "subagent_result"}, "[Subagent 'x' completed]"))
        queues = loop._pending_queues["websocket:42"]
        for _ in range(400):
            if queues.inject.qsize():
                break
            await asyncio.sleep(0.005)
        assert queues.inject.qsize() == 1
        routed = queues.inject.get_nowait()
        assert routed.metadata["injected_event"] == "subagent_result"
        assert not waiter[0].done()
    finally:
        loop._running = False
        release.set()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
