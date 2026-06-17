"""SP-6 real-harness end-to-end tests for server→client capabilities.

A real FastMCP server tool calls ctx.session.list_roots(),
ctx.session.send_log_message(), or ctx.session.create_message() over real
in-memory MCP streams. durin's callbacks run inside the client session and
the results/effects are asserted from the server-tool's return value.
"""
from __future__ import annotations

import asyncio
import contextlib

import pytest
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.memory import create_client_server_memory_streams

from durin.agent.tools.mcp_connection import MCPServerConnection
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import MCPSamplingConfig, MCPServerConfig

pytestmark = pytest.mark.asyncio


class _Harness:
    """SP-2 in-process harness — real FastMCP server over in-memory streams."""

    def __init__(self, server: FastMCP) -> None:
        self.server = server
        self._streams_cm = None
        self._task: asyncio.Task | None = None
        self._scope = None
        self.client_streams = None

    async def __aenter__(self) -> "_Harness":
        self._streams_cm = create_client_server_memory_streams()
        client_streams, server_streams = await self._streams_cm.__aenter__()
        self.client_streams = client_streams
        sr, sw = server_streams
        low = self.server._mcp_server
        ready = asyncio.Event()

        async def _run() -> None:
            import anyio
            with anyio.CancelScope() as scope:
                self._scope = scope
                ready.set()
                await low.run(sr, sw, low.create_initialization_options(), raise_exceptions=False)

        self._task = asyncio.create_task(_run())
        await ready.wait()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._scope:
            self._scope.cancel()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self._streams_cm.__aexit__(*exc)


def _wire(conn: MCPServerConnection, harness: _Harness) -> None:
    """Patch the connection's transport-open to return the harness streams."""
    async def _open(_self):
        return harness.client_streams[0], harness.client_streams[1]
    conn._open_transport_streams = _open.__get__(conn, MCPServerConnection)


# ---------------------------------------------------------------------------
# 6e.1 — server tool lists roots; assert it sees durin's workspace
# ---------------------------------------------------------------------------

async def test_server_tool_lists_durin_workspace_root(tmp_path):
    server = FastMCP("roots-probe")

    @server.tool()
    async def whats_my_root(ctx: Context) -> str:
        result = await ctx.session.list_roots()
        return str(result.roots[0].uri)

    async with _Harness(server) as harness:
        conn = MCPServerConnection(
            "roots-probe",
            MCPServerConfig(command="x"),
            ToolRegistry(),
            workspace=str(tmp_path),
        )
        _wire(conn, harness)
        await conn.start()
        out = await conn.call_tool("whats_my_root", {}, timeout=5.0)
        text = out.content[0].text
        assert text.startswith("file://")
        assert tmp_path.name in text
        await conn.aclose()


# ---------------------------------------------------------------------------
# 6e.2 — server tool emits a log; assert durin logs it
# ---------------------------------------------------------------------------

async def test_server_log_reaches_durin_logger(tmp_path):
    server = FastMCP("log-probe")

    @server.tool()
    async def shout(ctx: Context) -> str:
        await ctx.session.send_log_message(level="error", data="disk full", logger="storage")
        return "done"

    logged = []

    async with _Harness(server) as harness:
        conn = MCPServerConnection(
            "log-probe",
            MCPServerConfig(command="x"),
            ToolRegistry(),
            workspace=str(tmp_path),
        )
        _wire(conn, harness)
        await conn.start()

        from loguru import logger
        sink_id = logger.add(lambda msg: logged.append(str(msg)), level="DEBUG", format="{message}")
        try:
            await conn.call_tool("shout", {}, timeout=5.0)
            await asyncio.sleep(0.05)  # let the notification dispatch
        finally:
            logger.remove(sink_id)
        await conn.aclose()

    assert any("storage" in m and "disk full" in m for m in logged), f"log not found: {logged}"


# ---------------------------------------------------------------------------
# 6e.3 — sampling round-trip through fake LLM
# ---------------------------------------------------------------------------

async def test_server_sampling_round_trips_through_fake_llm(tmp_path):
    from durin.agent.tools.mcp_sampling import SamplingGovernance, SamplingRunner
    from durin.providers.base import LLMResponse

    class _FakeProvider:
        async def chat_with_retry(self, **kwargs):
            user = kwargs["messages"][-1]["content"]
            return LLMResponse(content=f"LLM saw: {user}", finish_reason="stop")

    server = FastMCP("sampling-probe")

    @server.tool()
    async def ask_the_model(ctx: Context, q: str) -> str:
        import mcp.types as types
        res = await ctx.session.create_message(
            messages=[types.SamplingMessage(
                role="user", content=types.TextContent(type="text", text=q)
            )],
            max_tokens=64,
        )
        return res.content.text

    runner = SamplingRunner(
        provider=_FakeProvider(),
        default_model="m-default",
        governance=SamplingGovernance(max_tokens_cap=128, requests_per_minute=5),
    )

    async with _Harness(server) as harness:
        conn = MCPServerConnection(
            "sampling-probe",
            MCPServerConfig(command="x", sampling=MCPSamplingConfig(enabled=True)),
            ToolRegistry(),
            sampling_runner=runner,
            workspace=str(tmp_path),
        )
        _wire(conn, harness)
        await conn.start()
        out = await conn.call_tool("ask_the_model", {"q": "hello"}, timeout=5.0)
        assert "LLM saw: hello" in out.content[0].text
        await conn.aclose()


async def test_server_sampling_rpm_limit_trips(tmp_path):
    """After RPM exhausted, the server's create_message call receives an error."""
    from durin.agent.tools.mcp_sampling import SamplingGovernance, SamplingRunner
    from durin.providers.base import LLMResponse

    call_count = {"n": 0}

    class _FakeProvider:
        async def chat_with_retry(self, **kwargs):
            call_count["n"] += 1
            return LLMResponse(content="ok", finish_reason="stop")

    server = FastMCP("rpm-probe")

    @server.tool()
    async def call_twice(ctx: Context) -> str:
        import mcp.types as types

        msg = [types.SamplingMessage(
            role="user", content=types.TextContent(type="text", text="x")
        )]
        results = []
        for _ in range(2):
            try:
                r = await ctx.session.create_message(messages=msg, max_tokens=8)
                results.append(f"ok:{r.content.text}")
            except Exception as e:
                results.append(f"err:{type(e).__name__}")
        return "|".join(results)

    runner = SamplingRunner(
        provider=_FakeProvider(),
        default_model="m-default",
        governance=SamplingGovernance(max_tokens_cap=128, requests_per_minute=1),
    )

    async with _Harness(server) as harness:
        conn = MCPServerConnection(
            "rpm-probe",
            MCPServerConfig(command="x", sampling=MCPSamplingConfig(enabled=True)),
            ToolRegistry(),
            sampling_runner=runner,
            workspace=str(tmp_path),
        )
        _wire(conn, harness)
        await conn.start()
        out = await conn.call_tool("call_twice", {}, timeout=5.0)
        text = out.content[0].text
        # first call succeeds, second is an error (rate limit)
        assert "ok:" in text
        assert "err:" in text
        assert call_count["n"] == 1  # provider only called once
        await conn.aclose()


async def test_server_sampling_rejected_when_runner_absent(tmp_path):
    """No sampling_runner → capability not advertised → server create_message raises."""
    server = FastMCP("no-sampling")

    @server.tool()
    async def ask(ctx: Context) -> str:
        import mcp.types as types
        try:
            await ctx.session.create_message(
                messages=[types.SamplingMessage(
                    role="user", content=types.TextContent(type="text", text="x")
                )],
                max_tokens=8,
            )
            return "unexpected-success"
        except Exception as e:
            return f"refused:{type(e).__name__}"

    async with _Harness(server) as harness:
        conn = MCPServerConnection(
            "no-sampling",
            MCPServerConfig(command="x"),
            ToolRegistry(),
            workspace=str(tmp_path),
        )
        _wire(conn, harness)
        await conn.start()
        out = await conn.call_tool("ask", {}, timeout=5.0)
        assert "refused" in out.content[0].text
        await conn.aclose()
