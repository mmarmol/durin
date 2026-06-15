from __future__ import annotations

import asyncio
import contextlib

import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from durin.agent.tools.mcp_connection import MCPServerConnection
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import MCPServerConfig

pytestmark = pytest.mark.asyncio


def _build_server(name: str = "harness") -> FastMCP:
    server = FastMCP(name)

    @server.tool()
    async def echo(text: str) -> str:
        return f"echo:{text}"

    @server.tool()
    async def emit_change(ctx: Context) -> str:
        await ctx.session.send_tool_list_changed()
        return "emitted"

    return server


class _InProcessHarness:
    """Runs a real FastMCP server in a cancellable task and exposes a
    MCPServerConnection wired to it via in-memory streams.

    The connection's transport-open is monkeypatched to hand back this
    harness's client streams, so MCPServerConnection.run() exercises the
    REAL ClientSession lifecycle (initialize, list_tools, notifications,
    teardown) against a REAL server — only the transport bytes are
    in-process instead of a subprocess/socket.
    """

    def __init__(self, server: FastMCP) -> None:
        self.server = server
        self._streams_cm = None
        self._server_task: asyncio.Task | None = None
        self._server_scope = None
        self.client_streams = None

    async def __aenter__(self) -> "_InProcessHarness":
        self._streams_cm = create_client_server_memory_streams()
        client_streams, server_streams = await self._streams_cm.__aenter__()
        self.client_streams = client_streams
        server_read, server_write = server_streams
        low = self.server._mcp_server
        ready = asyncio.Event()

        async def _run() -> None:
            import anyio

            with anyio.CancelScope() as scope:
                self._server_scope = scope
                ready.set()
                await low.run(
                    server_read, server_write,
                    low.create_initialization_options(),
                    raise_exceptions=False,
                )

        self._server_task = asyncio.create_task(_run())
        await ready.wait()
        return self

    def kill_server(self) -> None:
        """Cancel ONLY the server task; the client session stays alive so
        the next client RPC fails like a real dead/idle connection."""
        if self._server_scope is not None:
            self._server_scope.cancel()

    async def __aexit__(self, *exc) -> None:
        if self._server_scope is not None:
            self._server_scope.cancel()
        if self._server_task is not None:
            with _contextlib_suppress():
                await self._server_task
        await self._streams_cm.__aexit__(*exc)


def _contextlib_suppress():
    return contextlib.suppress(asyncio.CancelledError, Exception)


@pytest.fixture
async def live_mcp():
    """Yields (connection_factory, harness). The factory builds a
    MCPServerConnection whose transport-open returns the harness streams."""
    server = _build_server()
    async with _InProcessHarness(server) as harness:

        def factory(registry: ToolRegistry | None = None, **cfg_kw):
            registry = registry or ToolRegistry()
            cfg = MCPServerConfig(command="unused", **cfg_kw)
            conn = MCPServerConnection("harness", cfg, registry)

            async def _open(_self):
                return harness.client_streams[0], harness.client_streams[1]

            conn._open_transport_streams = _open.__get__(conn, MCPServerConnection)
            return conn, registry

        yield factory, harness


async def test_connection_connects_and_registers_tools(live_mcp) -> None:
    factory, _harness = live_mcp
    conn, registry = factory()
    ok = await conn.start()
    assert ok is True
    assert conn.session is not None
    assert registry.get("mcp_harness_echo") is not None
    await conn.aclose()
    assert conn.session is None


async def test_call_tool_resolves_live_session(live_mcp) -> None:
    factory, _harness = live_mcp
    conn, _registry = factory()
    await conn.start()

    result = await conn.call_tool("echo", {"text": "hi"}, timeout=5.0)
    assert result.content[0].text == "echo:hi"

    # Swap the live session object; the connection must use the NEW one.
    original = conn.session
    conn.session = original  # identity unchanged here, but the call must read the attr
    result2 = await conn.call_tool("echo", {"text": "again"}, timeout=5.0)
    assert result2.content[0].text == "echo:again"
    await conn.aclose()


async def test_call_tool_when_down_returns_sentinel(live_mcp) -> None:
    factory, _harness = live_mcp
    conn, _registry = factory()
    await conn.start()
    conn.session = None  # simulate down
    out = await conn.call_tool("echo", {"text": "x"}, timeout=5.0)
    from durin.agent.tools.mcp_connection import _ConnDown
    assert isinstance(out, _ConnDown)
    assert "not connected" in out.message
    await conn.aclose()


async def test_tool_wrapper_executes_through_connection(live_mcp) -> None:
    factory, _harness = live_mcp
    conn, registry = factory()
    await conn.start()
    wrapper = registry.get("mcp_harness_echo")
    assert wrapper is not None
    out = await wrapper.execute(text="world")
    # FastMCP may append [structuredContent]; the text portion must be present.
    assert "echo:world" in out
    await conn.aclose()


async def test_tool_wrapper_reports_breaker_sentinel(live_mcp) -> None:
    factory, _harness = live_mcp
    conn, registry = factory()
    await conn.start()
    wrapper = registry.get("mcp_harness_echo")
    conn.session = None  # force the down sentinel
    out = await wrapper.execute(text="x")
    assert "not connected" in out
    await conn.aclose()
