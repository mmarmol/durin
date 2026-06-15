"""Tests for the note_decision tool."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.note_decision import NoteDecisionTool
from durin.session.decision_log import DECISION_LOG_KEY


class _FakeSessions:
    def __init__(self):
        self.session = SimpleNamespace(metadata={})
        self.saved = 0

    def get_or_create(self, key):
        return self.session

    def save(self, session):
        self.saved += 1


def _ctx(sessions, *, enabled=True, max_entries=10, max_chars=1500):
    defaults = SimpleNamespace(
        decision_log_enabled=enabled,
        decision_log_max_entries=max_entries,
        decision_log_max_chars=max_chars,
    )
    app_config = SimpleNamespace(agents=SimpleNamespace(defaults=defaults))
    return SimpleNamespace(sessions=sessions, app_config=app_config)


def _tool(ctx):
    tool = NoteDecisionTool.create(ctx)
    tool.set_context(
        RequestContext(channel="websocket", chat_id="chat1", session_key="websocket:chat1")
    )
    return tool


@pytest.mark.asyncio
async def test_note_decision_records_to_metadata():
    sessions = _FakeSessions()
    tool = _tool(_ctx(sessions))
    result = await tool.execute(text="chose separate extract call")
    assert "Recorded" in result
    assert sessions.session.metadata[DECISION_LOG_KEY][0]["text"] == "chose separate extract call"
    assert sessions.session.metadata[DECISION_LOG_KEY][0]["source"] == "tool"
    assert sessions.saved == 1


@pytest.mark.asyncio
async def test_note_decision_rejects_blank():
    sessions = _FakeSessions()
    tool = _tool(_ctx(sessions))
    result = await tool.execute(text="   ")
    assert "Error" in result
    assert DECISION_LOG_KEY not in sessions.session.metadata


def test_note_decision_disabled_via_config():
    sessions = _FakeSessions()
    assert NoteDecisionTool.enabled(_ctx(sessions, enabled=False)) is False
    assert NoteDecisionTool.enabled(_ctx(sessions, enabled=True)) is True


def test_note_decision_disabled_when_no_sessions():
    ctx = _ctx(_FakeSessions())
    ctx.sessions = None
    assert NoteDecisionTool.enabled(ctx) is False
