"""Turn-end fallback serialization for channels that don't render tool payloads.

Rich channels (websocket, cli) render interactive payloads from structured
``tool_events``; everything else gets a plain-text message published by
``AgentLoop._maybe_publish_interaction_fallback`` (durin/agent/user_payloads.py).
The user's next inbound message clears answered payloads so fallbacks and
badges don't fire again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.agent.user_payloads import PENDING_SECRET_KEY
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMResponse


def _make_loop(tmp_path) -> AgentLoop:
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
    )
    loop.bus.publish_outbound = AsyncMock()
    return loop


@pytest.mark.asyncio
async def test_fallback_published_for_dumb_channel(tmp_path):
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("telegram:42")
    session.metadata["pending_question"] = {
        "question_id": "q1", "question": "Which color?", "options": ["red"],
    }
    await loop._maybe_publish_interaction_fallback(
        channel="telegram", chat_id="42", session_key="telegram:42",
    )
    sent = [
        call.args[0]
        for call in loop.bus.publish_outbound.call_args_list
        if "Which color?" in (call.args[0].content or "")
    ]
    assert len(sent) == 1
    assert "1. red" in sent[0].content
    assert sent[0].channel == "telegram"
    assert sent[0].chat_id == "42"


@pytest.mark.asyncio
async def test_no_fallback_for_rich_channel(tmp_path):
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("websocket:42")
    session.metadata["pending_question"] = {
        "question_id": "q1", "question": "Which color?", "options": [],
    }
    await loop._maybe_publish_interaction_fallback(
        channel="websocket", chat_id="42", session_key="websocket:42",
    )
    loop.bus.publish_outbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_fallback_without_pending_payloads(tmp_path):
    loop = _make_loop(tmp_path)
    loop.sessions.get_or_create("telegram:42")
    await loop._maybe_publish_interaction_fallback(
        channel="telegram", chat_id="42", session_key="telegram:42",
    )
    loop.bus.publish_outbound.assert_not_awaited()


def test_user_message_clears_pending_payloads(tmp_path):
    """Appending the user's next message clears answered interaction payloads
    (but never the pending plan — /build owns that)."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("websocket:42")
    session.metadata["pending_question"] = {
        "question_id": "q", "question": "?", "options": [],
    }
    session.metadata[PENDING_SECRET_KEY] = {"name": "N", "service": "s"}
    session.metadata["pending_plan_review"] = {"path": "p.md", "plan": "# P"}
    msg = InboundMessage(
        channel="websocket", sender_id="u", chat_id="42", content="my answer",
    )
    assert loop._persist_user_message_early(msg, session) is True
    assert "pending_question" not in session.metadata
    assert PENDING_SECRET_KEY not in session.metadata
    assert "pending_plan_review" in session.metadata


@pytest.mark.asyncio
async def test_pending_plan_is_delivered_once_not_every_turn(tmp_path):
    """A plan outlives the turn that produced it (only /build closes it), so
    without a delivered-marker every later turn re-sent the whole plan to the
    channel. Measured before the fix: 3 turns → 3 full plan messages."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("telegram:42")
    session.metadata["pending_plan_review"] = {
        "path": "p.md", "plan": "# Plan\n1. step one", "verification": "tests pass",
    }

    for _ in range(3):
        await loop._maybe_publish_interaction_fallback(
            channel="telegram", chat_id="42", session_key="telegram:42",
        )

    sent = [
        call.args[0]
        for call in loop.bus.publish_outbound.call_args_list
        if "step one" in (call.args[0].content or "")
    ]
    assert len(sent) == 1
    # The payload itself survives — /build still owns its lifecycle.
    assert "pending_plan_review" in session.metadata


@pytest.mark.asyncio
async def test_revised_plan_is_delivered_again(tmp_path):
    """Delivery is keyed on the text, so a refined plan reaches the user."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("telegram:42")
    session.metadata["pending_plan_review"] = {"path": "p.md", "plan": "# Plan v1"}
    await loop._maybe_publish_interaction_fallback(
        channel="telegram", chat_id="42", session_key="telegram:42",
    )
    session.metadata["pending_plan_review"] = {"path": "p.md", "plan": "# Plan v2"}
    await loop._maybe_publish_interaction_fallback(
        channel="telegram", chat_id="42", session_key="telegram:42",
    )

    bodies = [c.args[0].content or "" for c in loop.bus.publish_outbound.call_args_list]
    assert sum("Plan v1" in b for b in bodies) == 1
    assert sum("Plan v2" in b for b in bodies) == 1


@pytest.mark.asyncio
async def test_question_still_delivered_once_per_ask(tmp_path):
    """The same guard must not swallow a re-asked question."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("telegram:42")
    session.metadata["pending_question"] = {
        "question_id": "q1", "question": "Which color?", "options": ["red"],
    }
    await loop._maybe_publish_interaction_fallback(
        channel="telegram", chat_id="42", session_key="telegram:42",
    )
    await loop._maybe_publish_interaction_fallback(
        channel="telegram", chat_id="42", session_key="telegram:42",
    )
    session.metadata["pending_question"] = {
        "question_id": "q2", "question": "Which size?", "options": ["L"],
    }
    await loop._maybe_publish_interaction_fallback(
        channel="telegram", chat_id="42", session_key="telegram:42",
    )

    bodies = [c.args[0].content or "" for c in loop.bus.publish_outbound.call_args_list]
    assert sum("Which color?" in b for b in bodies) == 1
    assert sum("Which size?" in b for b in bodies) == 1


class _Outbox:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def publish_outbound(self, msg) -> None:
        self.sent.append(msg.content)


def _blocking_ask_on_slack(loop: AgentLoop, outbox: _Outbox):
    from durin.agent import pending_answers
    from durin.agent.tools.ask_user import AskUserQuestionTool
    from durin.agent.tools.context import RequestContext

    pending_answers.reset()
    pending_answers.set_consumer_active(True)
    tool = AskUserQuestionTool(
        sessions=loop.sessions, bus=outbox, blocking=True, answer_timeout_s=0.05,
    )
    tool.set_context(RequestContext(
        channel="slack", chat_id="C1", session_key="slack:C1", metadata={},
    ))
    return tool


@pytest.mark.asyncio
async def test_a_timed_out_question_reaches_a_text_channel_once(tmp_path):
    """A text channel gets the blocking question before the wait. When the
    wait times out the turn ends, and the turn-end fallback must not send the
    same question a second time."""
    from durin.agent import pending_answers

    loop = _make_loop(tmp_path)
    outbox = _Outbox()
    tool = _blocking_ask_on_slack(loop, outbox)
    try:
        await tool.execute(question="Which color?")
    finally:
        pending_answers.reset()
    await loop._maybe_publish_interaction_fallback(
        channel="slack", chat_id="C1", session_key="slack:C1",
    )

    assert outbox.sent == ["❓ Which color?"]
    assert loop.bus.publish_outbound.call_args_list == []


@pytest.mark.asyncio
async def test_the_same_question_asked_again_is_sent_again(tmp_path):
    """Delivery is remembered per text, so a new ask worded like an answered
    one must still reach the channel."""
    from durin.agent import pending_answers
    from durin.agent.tools.ask_user import PENDING_QUESTION_KEY

    loop = _make_loop(tmp_path)
    outbox = _Outbox()
    tool = _blocking_ask_on_slack(loop, outbox)
    try:
        await tool.execute(question="Which color?")
        # The user answered; the loop clears the answered question.
        loop.sessions.get_or_create("slack:C1").metadata.pop(PENDING_QUESTION_KEY)
        await tool.execute(question="Which color?")
    finally:
        pending_answers.reset()

    assert outbox.sent == ["❓ Which color?", "❓ Which color?"]
