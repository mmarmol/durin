"""Tests for MCP HTTP probe guard (prevents event-loop crash on unreachable servers)."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from durin.agent.tools.mcp import _probe_http_url, connect_mcp_servers
from durin.agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# _probe_http_url unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_returns_true_for_open_port(tmp_path):
    """Start a trivial TCP server, probe should return True."""
    async def _accept(reader, writer):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await _probe_http_url(f"http://127.0.0.1:{port}/mcp") is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_returns_false_for_closed_port():
    """Port 19999 is almost certainly not listening."""
    assert await _probe_http_url("http://127.0.0.1:19999/mcp") is False


@pytest.mark.asyncio
async def test_probe_uses_default_port_for_http():
    """When no port in URL, should default to 80 (will fail -> False)."""
    assert await _probe_http_url("http://unreachable-host.test/mcp") is False


# ---------------------------------------------------------------------------
# connect_mcp_servers skips unreachable HTTP servers
# ---------------------------------------------------------------------------

def _make_http_cfg(url: str, transport: str = "streamableHttp"):
    # Use a real MCPServerConfig (not MagicMock) so new fields like `oauth`,
    # `malware_check`, `spawn_egress_policy` take their real defaults instead of
    # truthy mocks that wrongly trigger OAuth/security paths.
    from durin.config.schema import MCPServerConfig

    return MCPServerConfig(type=transport, url=url, tool_timeout=30, enabled_tools=["*"])


@pytest.mark.asyncio
async def test_connect_skips_unreachable_streamable_http():
    """Unreachable streamableHttp server should be skipped with a warning, no crash."""
    registry = ToolRegistry()
    servers = {"dead": _make_http_cfg("http://127.0.0.1:19999/mcp")}
    stacks = await connect_mcp_servers(servers, registry)
    assert stacks == {}
    assert len(registry._tools) == 0


@pytest.mark.asyncio
async def test_connect_skips_unreachable_sse():
    """Unreachable SSE server should be skipped with a warning, no crash."""
    registry = ToolRegistry()
    servers = {"dead": _make_http_cfg("http://127.0.0.1:19999/sse", transport="sse")}
    stacks = await connect_mcp_servers(servers, registry)
    assert stacks == {}
    assert len(registry._tools) == 0


@pytest.mark.asyncio
async def test_probe_not_called_for_stdio():
    """stdio transport should not be probed — it spawns a local process."""
    called = False
    original_probe = _probe_http_url

    async def _spy_probe(url, **kw):
        nonlocal called
        called = True
        return await original_probe(url, **kw)

    with patch("durin.agent.tools.mcp._probe_http_url", _spy_probe):
        from durin.config.schema import MCPServerConfig

        cfg = MCPServerConfig(
            type="stdio", command="nonexistent-command-xyz", tool_timeout=30, enabled_tools=["*"]
        )
        registry = ToolRegistry()
        await connect_mcp_servers({"s": cfg}, registry)

    assert not called, "probe should not be called for stdio transport"
