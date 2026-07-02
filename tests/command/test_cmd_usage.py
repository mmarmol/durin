"""/usage: cumulative per-session token accounting."""

from types import SimpleNamespace

import pytest

from durin.command.builtin import accumulate_session_usage, cmd_usage
from durin.command.router import CommandContext


class _FakeSession:
    def __init__(self):
        self.metadata = {}


def test_accumulate_session_usage_sums_turns():
    session = _FakeSession()
    accumulate_session_usage(session, {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
    accumulate_session_usage(session, {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230})
    usage = session.metadata["token_usage"]
    assert usage == {
        "prompt_tokens": 300,
        "completion_tokens": 50,
        "total_tokens": 350,
        "turns": 2,
    }


def test_accumulate_ignores_empty_usage():
    session = _FakeSession()
    accumulate_session_usage(session, {})
    assert "token_usage" not in session.metadata


@pytest.mark.asyncio
async def test_cmd_usage_reports_cumulative_and_last_turn():
    session = _FakeSession()
    session.metadata["token_usage"] = {
        "prompt_tokens": 300, "completion_tokens": 50, "total_tokens": 350, "turns": 2,
    }
    loop = SimpleNamespace(_last_usage={"prompt_tokens": 200, "completion_tokens": 30})
    msg = SimpleNamespace(channel="websocket", chat_id="c1", metadata={})
    ctx = CommandContext(msg=msg, session=session, key="k", raw="/usage", loop=loop)
    out = await cmd_usage(ctx)
    assert "350" in out.content
    assert "2" in out.content


@pytest.mark.asyncio
async def test_cmd_usage_without_history():
    msg = SimpleNamespace(channel="websocket", chat_id="c1", metadata={})
    loop = SimpleNamespace(_last_usage={}, sessions=SimpleNamespace(get_or_create=lambda k: _FakeSession()))
    ctx = CommandContext(msg=msg, session=None, key="k", raw="/usage", loop=loop)
    out = await cmd_usage(ctx)
    assert "No token usage" in out.content
