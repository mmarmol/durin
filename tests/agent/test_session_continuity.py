"""A fresh session on a single-user channel starts with the previous
session's summary in the archived-context slot, for its first turns only."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.config.schema import MemoryContinuityConfig
from durin.memory.session_summary_store import write_session_summary
from durin.providers.base import LLMResponse, ToolCallRequest


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


def test_fresh_session_gets_previous_summary(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:old", "- decided to use X", last_active=date(2026, 9, 1))

    block = loop._format_pending_summary(loop.sessions.get_or_create("websocket:new"))

    assert block is not None
    assert "=== PREVIOUS SESSION SUMMARY (websocket_old, last active 2026-09-01) ===" in block
    assert "decided to use X" in block


def test_own_summary_wins_over_previous(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:old", "- decided to use X", last_active=date(2026, 9, 1))
    write_session_summary(tmp_path, "websocket:new", "- my own span", last_active=date(2026, 9, 6))

    block = loop._format_pending_summary(loop.sessions.get_or_create("websocket:new"))

    assert "my own span" in block
    assert "PREVIOUS SESSION SUMMARY" not in block


def test_previous_summary_stops_after_max_turns_and_off_single_user_channels(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:old", "- decided to use X", last_active=date(2026, 9, 1))
    write_session_summary(tmp_path, "slack:c1", "- slack", last_active=date(2026, 9, 5))

    grown = loop.sessions.get_or_create("websocket:new")
    for i in range(4):
        grown.add_message("user", f"q{i}")
        grown.add_message("assistant", f"a{i}")
    assert loop._format_pending_summary(grown) is None
    assert loop._format_pending_summary(loop.sessions.get_or_create("slack:c2")) is None


def test_previous_summary_can_be_disabled(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:old", "- decided to use X", last_active=date(2026, 9, 1))
    loop.app_config = SimpleNamespace(memory=SimpleNamespace(continuity=MemoryContinuityConfig(enabled=False)))

    assert loop._format_pending_summary(loop.sessions.get_or_create("websocket:new")) is None


@pytest.mark.asyncio
async def test_previous_summary_reaches_the_system_prompt(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:old", "- decided to use X", last_active=date(2026, 9, 1))
    captured: list[list[dict]] = []

    async def _chat(*args, **kwargs):
        captured.append(kwargs.get("messages") or (args[0] if args else []))
        return LLMResponse(content="ok", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)

    await loop._process_message(InboundMessage(channel="websocket", sender_id="u", chat_id="new", content="hi"))

    system = captured[0][0]["content"]
    assert "[Archived Context Summary]" in system
    assert "decided to use X" in system


@pytest.mark.asyncio
async def test_previous_summary_appears_only_for_first_max_turns_turns(tmp_path: Path) -> None:
    """The previous-session block lasts exactly max_turns turns.
    With max_turns=2, it appears on turns 1 and 2, but not turn 3 —
    even when an agentic turn persists far more than two messages of its
    own (tool results land in session.messages too)."""
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:old", "- old summary", last_active=date(2026, 9, 1))
    loop.app_config = SimpleNamespace(memory=SimpleNamespace(continuity=MemoryContinuityConfig(max_turns=2)))
    captured: list[list[dict]] = []
    responses = iter([
        # Turn 1 is agentic: one stub tool, three results inside the turn.
        LLMResponse(content="", tool_calls=[
            ToolCallRequest(id=f"call{i}", name="read_file", arguments={"path": f"f{i}.txt"})
            for i in range(3)
        ]),
        LLMResponse(content="response-1", tool_calls=[]),
        LLMResponse(content="response-2", tool_calls=[]),
        LLMResponse(content="response-3", tool_calls=[]),
    ])

    async def _chat(*args, **kwargs):
        captured.append(kwargs.get("messages") or (args[0] if args else []))
        return next(responses)

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)
    loop.tools.prepare_call = MagicMock(return_value=(None, {"path": "foo.txt"}, None))
    loop.tools.execute = AsyncMock(return_value="ok")

    async def _turn(text: str) -> str:
        """Run one turn; return the system prompt it opened with."""
        at = len(captured)
        await loop._process_message(
            InboundMessage(channel="websocket", sender_id="u", chat_id="new", content=text)
        )
        return captured[at][0]["content"]

    assert "PREVIOUS SESSION SUMMARY" in await _turn("q1")
    # One completed turn, but well past 2 * max_turns persisted messages.
    assert len(loop.sessions.get_or_create("websocket:new").messages) > 4
    assert "PREVIOUS SESSION SUMMARY" in await _turn("q2")
    assert "PREVIOUS SESSION SUMMARY" not in await _turn("q3")


@pytest.mark.asyncio
async def test_closed_record_feeds_the_fresh_session(tmp_path: Path) -> None:
    """`/new` files the conversation it closes as its own record; the fresh
    session on the same key then picks that record as its previous session."""
    loop = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "I prefer terse answers")
    session.add_message("assistant", "Noted.")
    loop.sessions.save(session)

    async def _fake_archive(_messages):
        return "- user prefers terse answers", {"entities": [], "topics": []}

    loop.consolidator.archive = _fake_archive  # type: ignore[method-assign]

    await loop._process_message(
        InboundMessage(channel="cli", sender_id="u", chat_id="test", content="/new")
    )
    await loop.close_mcp()  # drains the background closed-record write

    block = loop._format_pending_summary(loop.sessions.get_or_create("cli:test"))

    assert block is not None
    assert "PREVIOUS SESSION SUMMARY" in block
    assert "user prefers terse answers" in block


@pytest.mark.asyncio
async def test_a_slash_command_turn_does_not_consume_a_continuity_turn(tmp_path: Path) -> None:
    """Continuity is measured in conversation turns. A slash command persists a
    user message like any other turn, but it never reached the model, so it
    must not spend one of the turns the previous summary is shown for."""
    loop = _make_loop(tmp_path)
    write_session_summary(tmp_path, "websocket:old", "- old summary", last_active=date(2026, 9, 1))
    loop.app_config = SimpleNamespace(memory=SimpleNamespace(continuity=MemoryContinuityConfig(max_turns=1)))
    captured: list[list[dict]] = []

    async def _chat(*args, **kwargs):
        captured.append(kwargs.get("messages") or (args[0] if args else []))
        return LLMResponse(content="ok", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="new", content="/help")
    )
    session = loop.sessions.get_or_create("websocket:new")
    # The command turn is persisted (and marked), so a plain message count
    # would already be at the limit here.
    assert [m.get("_command") for m in session.messages] == [True, True]
    assert captured == []                                  # it never reached the model

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="new", content="q1")
    )
    assert "PREVIOUS SESSION SUMMARY" in captured[0][0]["content"]

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="new", content="q2")
    )
    assert "PREVIOUS SESSION SUMMARY" not in captured[1][0]["content"]
