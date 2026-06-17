"""Tests for MCP _is_transient helper and wrapper delegation."""

import asyncio
from types import SimpleNamespace

import pytest

# The `mcp` package is an optional extra (`durin-agent[mcp]`). Skip the whole
# file when it isn't installed so CI without the extra doesn't blow up at
# collection time.
mcp_types = pytest.importorskip("mcp.types")

from durin.agent.tools.mcp import (
    MCPToolWrapper,
    _is_transient,
)


# ---------------------------------------------------------------------------
# _is_transient helper
# ---------------------------------------------------------------------------


class _FakeClosedResourceError(Exception):
    pass


_FakeClosedResourceError.__name__ = "ClosedResourceError"


class _FakeEndOfStreamError(Exception):
    pass


_FakeEndOfStreamError.__name__ = "EndOfStream"


def test_is_transient_recognizes_closed_resource():
    assert _is_transient(_FakeClosedResourceError("gone"))


def test_is_transient_recognizes_broken_pipe():
    assert _is_transient(BrokenPipeError("pipe"))


def test_is_transient_recognizes_connection_reset():
    assert _is_transient(ConnectionResetError("reset"))


def test_is_transient_recognizes_connection_refused():
    assert _is_transient(ConnectionRefusedError("refused"))


def test_is_transient_recognizes_end_of_stream():
    assert _is_transient(_FakeEndOfStreamError("eof"))


def test_is_transient_rejects_value_error():
    assert not _is_transient(ValueError("nope"))


def test_is_transient_rejects_runtime_error():
    assert not _is_transient(RuntimeError("nope"))


def test_is_transient_rejects_timeout():
    assert not _is_transient(TimeoutError("timeout"))


# ---------------------------------------------------------------------------
# Wrapper delegation smoke test (retry/timeout/cancel behavior moved to
# test_mcp_connection.py as SP-2 2b/2e harness-backed tests)
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal stand-in connection for wrapper unit tests."""

    def __init__(self, session):
        self.session = session
        self.name = "test_server"

    async def call_tool(self, name, arguments, timeout):
        from durin.agent.tools.mcp_connection import _ConnDown
        if self.session is None:
            return _ConnDown("not connected")
        try:
            return await self.session.call_tool(name, arguments=arguments)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _ConnDown(f"failed: {type(exc).__name__}")


def _make_tool_def(name="test_tool"):
    return SimpleNamespace(
        name=name,
        description="A test tool",
        inputSchema={"type": "object", "properties": {}},
    )


@pytest.mark.asyncio
async def test_tool_success_on_first_try_no_retry():
    """Normal success path — wrapper delegates to connection."""
    from unittest.mock import AsyncMock

    session = AsyncMock()
    result = SimpleNamespace(content=[mcp_types.TextContent(type="text", text="hello")])
    session.call_tool = AsyncMock(return_value=result)

    wrapper = MCPToolWrapper(_FakeConn(session), "test_server", _make_tool_def(), tool_timeout=5)
    output = await wrapper.execute()

    assert output == "hello"
    assert session.call_tool.call_count == 1
