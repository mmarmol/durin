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
