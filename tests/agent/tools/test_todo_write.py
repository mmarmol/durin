"""Tests for the ``todo_write`` tool."""

from __future__ import annotations

import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.todos import TodoWriteTool
from durin.session.manager import SessionManager
from durin.session.todo_state import TODOS_KEY


def _tool(sm: SessionManager) -> TodoWriteTool:
    t = TodoWriteTool(sessions=sm)
    rc = RequestContext(
        channel="cli",
        chat_id="d",
        session_key="cli:d",
        metadata={},
    )
    t.set_context(rc)
    return t


@pytest.mark.asyncio
async def test_todo_write_stores_full_list_in_session_metadata(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    items = [
        {"content": "Read code", "status": "completed", "activeForm": "Reading code"},
        {"content": "Apply fix", "status": "in_progress", "activeForm": "Applying the fix"},
        {"content": "Run tests", "status": "pending", "activeForm": "Running tests"},
    ]
    out = await tool.execute(todos=items)

    assert "Todo list updated" in out
    # The result echoes the markdown so the model can re-present it.
    assert "Applying the fix" in out
    assert "[x] Read code" in out

    sess = sm.get_or_create("cli:d")
    stored = sess.metadata.get(TODOS_KEY)
    assert isinstance(stored, list)
    assert len(stored) == 3
    assert stored[1]["status"] == "in_progress"


@pytest.mark.asyncio
async def test_todo_write_replaces_prior_list(tmp_path):
    """Each call replaces — old items not in the new list disappear."""
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    await tool.execute(todos=[
        {"content": "old", "status": "pending", "activeForm": "old-ing"},
    ])
    await tool.execute(todos=[
        {"content": "new", "status": "completed", "activeForm": "new-ing"},
    ])

    sess = sm.get_or_create("cli:d")
    stored = sess.metadata[TODOS_KEY]
    assert len(stored) == 1
    assert stored[0]["content"] == "new"


@pytest.mark.asyncio
async def test_todo_write_coerces_multiple_in_progress_to_single(tmp_path):
    """Soft contract: only the first in_progress is kept; the rest move
    to pending. The tool flags this in its result so the model notices."""
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    out = await tool.execute(todos=[
        {"content": "first", "status": "in_progress", "activeForm": "first-ing"},
        {"content": "second", "status": "in_progress", "activeForm": "second-ing"},
        {"content": "third", "status": "in_progress", "activeForm": "third-ing"},
    ])

    assert "only the first kept" in out

    sess = sm.get_or_create("cli:d")
    statuses = [t["status"] for t in sess.metadata[TODOS_KEY]]
    assert statuses == ["in_progress", "pending", "pending"]


@pytest.mark.asyncio
async def test_todo_write_rejects_malformed_input(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    out = await tool.execute(todos="not a list")
    assert "Error" in out


@pytest.mark.asyncio
async def test_todo_write_no_session_errors_gracefully(tmp_path):
    sm = SessionManager(tmp_path)
    tool = TodoWriteTool(sessions=sm)
    # No request context set ⇒ no session resolution possible.
    out = await tool.execute(todos=[
        {"content": "x", "status": "pending", "activeForm": "xing"},
    ])
    assert "active chat session" in out


@pytest.mark.asyncio
async def test_todo_write_empty_list_clears_the_state(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)

    await tool.execute(todos=[
        {"content": "x", "status": "pending", "activeForm": "xing"},
    ])
    await tool.execute(todos=[])

    sess = sm.get_or_create("cli:d")
    assert sess.metadata[TODOS_KEY] == []
