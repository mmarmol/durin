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
