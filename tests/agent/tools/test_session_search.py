"""Tests for the ``session_search`` tool."""

from __future__ import annotations

import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.session_search import SessionSearchTool
from durin.session.manager import SessionManager


def _tool(sm: SessionManager) -> SessionSearchTool:
    t = SessionSearchTool(sessions=sm)
    rc = RequestContext(
        channel="cli",
        chat_id="d",
        session_key="cli:d",
        metadata={},
    )
    t.set_context(rc)
    return t


def _seed_session(sm: SessionManager, key: str, msgs: list[dict]) -> None:
    sess = sm.get_or_create(key)
    sess.messages.extend(msgs)


@pytest.mark.asyncio
async def test_keyword_search_returns_matches_with_indices(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [
        {"role": "user", "content": "How do I set up auth?"},
        {"role": "assistant", "content": "You can use JWT for auth."},
        {"role": "user", "content": "What about OAuth?"},
    ])

    out = await tool.execute(query="auth")

    assert "3 matches" in out
    # All three msg_index values should appear because all messages
    # contain 'auth' case-insensitively.
    assert "[0]" in out and "[1]" in out and "[2]" in out
    assert "user:" in out and "assistant:" in out


@pytest.mark.asyncio
async def test_case_sensitive_filters_matches(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [
        {"role": "user", "content": "Use React"},
        {"role": "assistant", "content": "react is great"},
    ])

    out = await tool.execute(query="React", case_sensitive=True)
    assert "1 match" in out
    assert "[0]" in out
    assert "[1]" not in out


@pytest.mark.asyncio
async def test_regex_search_with_invalid_pattern_returns_error(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [{"role": "user", "content": "anything"}])

    out = await tool.execute(query="(unclosed", regex=True)
    assert "Error" in out
    assert "invalid regex" in out.lower()


@pytest.mark.asyncio
async def test_regex_pattern_matches_correctly(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [
        {"role": "user", "content": "error 404 happened"},
        {"role": "assistant", "content": "status 200 ok"},
        {"role": "tool", "name": "exec", "content": "exit code 1"},
    ])

    out = await tool.execute(query=r"\d{3}", regex=True)

    # Matches "404" and "200" — three-digit number.
    assert "2 matches" in out


@pytest.mark.asyncio
async def test_role_filter_restricts_results(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [
        {"role": "user", "content": "find this"},
        {"role": "assistant", "content": "find that"},
        {"role": "tool", "name": "grep", "content": "find another"},
    ])

    out = await tool.execute(query="find", role="assistant")
    assert "1 match" in out
    assert "assistant:" in out
    assert "tool" not in out.replace("tool(grep)", "")


@pytest.mark.asyncio
async def test_tool_role_label_includes_tool_name(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [
        {"role": "tool", "name": "read_file", "content": "matching content here"},
    ])

    out = await tool.execute(query="matching")
    assert "tool(read_file)" in out


@pytest.mark.asyncio
async def test_no_matches_returns_clear_message(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [{"role": "user", "content": "hello"}])

    out = await tool.execute(query="zzzz_no_match")
    assert "No matches" in out


@pytest.mark.asyncio
async def test_empty_session_returns_clear_message(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    # No messages seeded — session exists but is empty.
    sm.get_or_create("cli:d")

    out = await tool.execute(query="anything")
    assert "No prior messages" in out


@pytest.mark.asyncio
async def test_max_results_caps_output_to_tail(tmp_path):
    """When there are more matches than max_results, return the latest N."""
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    msgs = [{"role": "user", "content": f"foo {i}"} for i in range(10)]
    _seed_session(sm, "cli:d", msgs)

    out = await tool.execute(query="foo", max_results=3)
    assert "10 matches" in out
    assert "showing last 3" in out
    # The last three indices [7], [8], [9] should appear; not earlier ones.
    assert "[7]" in out and "[8]" in out and "[9]" in out
    assert "[0]" not in out


@pytest.mark.asyncio
async def test_invalid_role_returns_error(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [{"role": "user", "content": "x"}])

    out = await tool.execute(query="x", role="nonexistent")
    assert "Error" in out
    assert "role" in out


@pytest.mark.asyncio
async def test_content_as_blocks_is_searchable(tmp_path):
    """Tool messages with structured content (list of blocks) must be
    searchable too — common shape for multi-modal tool results."""
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [
        {"role": "tool", "name": "image_gen", "content": [
            {"type": "text", "text": "rendered the diagram"},
            {"type": "image_url", "image_url": "..."},
        ]},
    ])

    out = await tool.execute(query="diagram")
    assert "1 match" in out


@pytest.mark.asyncio
async def test_empty_query_returns_error(tmp_path):
    sm = SessionManager(tmp_path)
    tool = _tool(sm)
    _seed_session(sm, "cli:d", [{"role": "user", "content": "hello"}])

    out = await tool.execute(query="   ")
    assert "Error" in out


def test_tool_is_in_plan_mode_allowed_set():
    from durin.agent.agent_mode import PLAN_MODE
    assert PLAN_MODE.is_tool_allowed("session_search")


def test_tool_discovered_by_loader():
    from durin.agent.tools.loader import ToolLoader
    names = [c.__name__ for c in ToolLoader().discover()]
    assert "SessionSearchTool" in names
