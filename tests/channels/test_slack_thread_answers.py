"""With ``reply_in_thread`` (the default), a top-level Slack channel mention is
answered in a thread under it, and the person answers a question the turn asks
(or an approval) in that same thread. The turn and every reply or click there
must share one session, or the answer never reaches the waiting turn."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import slack_sdk  # noqa: F401
except ImportError:
    pytest.skip("Slack dependencies not installed (slack-sdk)", allow_module_level=True)

from durin.agent import pending_answers
from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.channels.slack import SlackChannel, SlackConfig
from durin.providers.base import GenerationSettings, LLMResponse


@pytest.fixture(autouse=True)
def _clean_waiters():
    pending_answers.reset()
    yield
    pending_answers.reset()


def _loop(tmp_path, *, unified: bool) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    resp = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=resp)
    provider.chat_stream_with_retry = AsyncMock(return_value=resp)
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     model="test-model", unified_session=unified)


def _channel(bus: MessageBus) -> SlackChannel:
    channel = SlackChannel(SlackConfig(enabled=True, allow_from=["*"]), bus)
    channel._bot_user_id = "UBOT"
    channel._web_client = SimpleNamespace(
        reactions_add=AsyncMock(),
        conversations_replies=AsyncMock(return_value={"messages": []}),
    )
    return channel


def _event(envelope_id: str, **event) -> SimpleNamespace:
    body = {"user": "U1", "channel": "C123", "channel_type": "channel", **event}
    return SimpleNamespace(type="events_api", envelope_id=envelope_id,
                           payload={"event_id": f"Ev-{envelope_id}", "event": body})


def _click(ts: str, thread_ts: str) -> SimpleNamespace:
    return SimpleNamespace(type="interactive", envelope_id="env-click", payload={
        "actions": [{"value": "Yes"}], "user": {"id": "U1"},
        "channel": {"id": "C123"}, "message": {"ts": ts, "thread_ts": thread_ts},
    })


_CLIENT = SimpleNamespace(send_socket_mode_response=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("unified", [False, True])
async def test_a_reply_in_the_thread_answers_the_turn_its_mention_started(tmp_path, unified):
    bus = MessageBus()
    channel = _channel(bus)
    loop = _loop(tmp_path, unified=unified)

    await channel._on_socket_request(
        _CLIENT, _event("m1", type="app_mention", text="<@UBOT> deploy it?", ts="200.000"))
    turn = bus.inbound.get_nowait()
    assert turn.metadata["slack"]["thread_ts"] == "200.000"  # answered in the thread

    waiting = pending_answers.create(loop._effective_session_key(turn))
    await channel._on_socket_request(
        _CLIENT, _event("r1", type="message", text="yes", ts="201.000", thread_ts="200.000"))
    reply = bus.inbound.get_nowait()
    assert loop._maybe_resolve_pending_answer(reply, loop._effective_session_key(reply)) is True
    assert await waiting == "yes"

    waiting = pending_answers.create(loop._effective_session_key(turn))
    await channel._on_socket_request(_CLIENT, _click(ts="202.000", thread_ts="200.000"))
    click = bus.inbound.get_nowait()
    assert loop._maybe_resolve_pending_answer(click, loop._effective_session_key(click)) is True
    assert await waiting == "Yes"


@pytest.mark.asyncio
async def test_the_first_reply_does_not_refetch_history_the_thread_session_holds():
    """The mention and the bot's answers are already in the thread's session;
    fetching the thread again would feed them to the model twice."""
    bus = MessageBus()
    channel = _channel(bus)

    await channel._on_socket_request(
        _CLIENT, _event("m1", type="app_mention", text="<@UBOT> deploy it?", ts="200.000"))
    await channel._on_socket_request(
        _CLIENT, _event("r1", type="message", text="yes", ts="201.000", thread_ts="200.000"))

    channel._web_client.conversations_replies.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_top_level_dm_keeps_the_dm_session():
    bus = MessageBus()
    channel = _channel(bus)

    await channel._on_socket_request(_CLIENT, SimpleNamespace(
        type="events_api", envelope_id="d1",
        payload={"event_id": "Ev-d1", "event": {
            "type": "message", "user": "U1", "channel": "D123", "channel_type": "im",
            "text": "hi", "ts": "300.000"}}))

    msg = bus.inbound.get_nowait()
    assert msg.session_key == "slack:D123"
    assert msg.metadata["slack"]["thread_ts"] is None
