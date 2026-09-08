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
from durin.providers.base import LLMResponse


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
