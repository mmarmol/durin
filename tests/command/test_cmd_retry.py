"""/retry: rewrite into a resend of the last user message."""

from types import SimpleNamespace

import pytest

from durin.command.builtin import cmd_retry
from durin.command.router import CommandContext


class _FakeSession:
    def __init__(self, messages):
        self.messages = messages
        self.metadata = {}


def _ctx(messages, content="/retry"):
    msg = SimpleNamespace(channel="telegram", chat_id="c1", metadata={}, content=content)
    session = _FakeSession(messages)
    loop = SimpleNamespace(sessions=SimpleNamespace(get_or_create=lambda k: session))
    return CommandContext(msg=msg, session=session, key="k", raw=content, loop=loop), msg


@pytest.mark.asyncio
async def test_retry_rewrites_last_user_text():
    ctx, msg = _ctx([
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "flaky answer"},
    ])
    result = await cmd_retry(ctx)
    assert result is None
    assert msg.content == "second question"
    assert msg.metadata["original_command"] == "/retry"


@pytest.mark.asyncio
async def test_retry_skips_slash_commands_and_block_content():
    ctx, msg = _ctx([
        {"role": "user", "content": [{"type": "text", "text": "block question"}]},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "/status"},
    ])
    result = await cmd_retry(ctx)
    assert result is None
    assert msg.content == "block question"


@pytest.mark.asyncio
async def test_retry_with_no_user_history_returns_notice():
    ctx, _msg = _ctx([{"role": "assistant", "content": "hello"}])
    result = await cmd_retry(ctx)
    assert result is not None
    assert "Nothing to retry" in result.content
