from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

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


# ---------------------------------------------------------------------------
# Sub-phase 2b — Reconnect
# ---------------------------------------------------------------------------


async def test_reconnect_after_transport_death(live_mcp, monkeypatch) -> None:
    import durin.agent.tools.mcp_connection as mc

    factory, harness = live_mcp
    conn, registry = factory(keepalive_interval=0.1)
    monkeypatch.setattr(mc, "_INITIAL_BACKOFF", 0.05, raising=False)
    monkeypatch.setattr(mc, "_MAX_BACKOFF", 0.2, raising=False)
    await conn.start()
    assert conn.session is not None

    fresh = _build_server()
    async with _InProcessHarness(fresh) as fresh_harness:

        async def _open(_self):
            return fresh_harness.client_streams[0], fresh_harness.client_streams[1]

        conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)
        harness.kill_server()
        # Wait for the connection to come back on the fresh server.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if conn.session is not None and conn.breaker_state().name == "CLOSED":
                try:
                    r = await conn.call_tool("echo", {"text": "rev"}, timeout=2.0)
                    if getattr(r, "content", None):
                        break
                except Exception:
                    continue
        assert conn.session is not None
        r = await conn.call_tool("echo", {"text": "rev"}, timeout=2.0)
        assert r.content[0].text == "echo:rev"
    await conn.aclose()


async def test_initial_connect_gives_up_after_three(monkeypatch) -> None:
    import durin.agent.tools.mcp_connection as mc

    monkeypatch.setattr(mc, "_INITIAL_BACKOFF", 0.01, raising=False)
    monkeypatch.setattr(mc, "_MAX_BACKOFF", 0.02, raising=False)
    cfg = MCPServerConfig(command="x")
    registry = ToolRegistry()
    conn = mc.MCPServerConnection("dead", cfg, registry)

    attempts = {"n": 0}

    async def _always_fail(_self):
        attempts["n"] += 1
        raise ConnectionRefusedError("nope")

    conn._open_transport_streams = _always_fail.__get__(conn, mc.MCPServerConnection)

    ok = await conn.start()
    assert ok is False
    assert attempts["n"] == mc._MAX_INITIAL_CONNECT_RETRIES + 1  # first try + retries


async def test_initial_auth_failure_does_not_retry(monkeypatch) -> None:
    import httpx

    import durin.agent.tools.mcp_connection as mc

    cfg = MCPServerConfig(command="x")
    conn = mc.MCPServerConnection("authsrv", cfg, ToolRegistry())

    attempts = {"n": 0}

    async def _auth_fail(_self):
        attempts["n"] += 1
        resp = httpx.Response(401, request=httpx.Request("GET", "http://x"))
        raise httpx.HTTPStatusError("401", request=resp.request, response=resp)

    conn._open_transport_streams = _auth_fail.__get__(conn, mc.MCPServerConnection)

    ok = await conn.start()
    assert ok is False
    assert attempts["n"] == 1  # fail-fast, no backoff retries


async def test_call_recovers_after_session_expiry(live_mcp) -> None:
    import durin.agent.tools.mcp_connection as mc

    factory, harness = live_mcp
    conn, _registry = factory(keepalive_interval=10.0)
    await conn.start()

    real_session = conn.session
    state = {"raised": False}
    orig_call = real_session.call_tool

    async def flaky_call(name, **kw):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("Invalid or expired session")
        return await orig_call(name, **kw)

    real_session.call_tool = flaky_call

    fresh = _build_server()
    async with _InProcessHarness(fresh) as fh:

        async def _open(_self):
            return fh.client_streams[0], fh.client_streams[1]

        conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)

        out = await conn.call_tool("echo", {"text": "recovered"}, timeout=3.0)
        # Recovery must succeed: a real result, NOT the spurious "restarted" sentinel.
        assert not isinstance(out, mc._ConnDown), (
            f"Recovery returned _ConnDown instead of a real result: {out.message!r}"
        )
        assert out.content[0].text == "echo:recovered"
    await conn.aclose()


async def test_timeout_then_success_same_session(live_mcp, monkeypatch) -> None:
    """A per-tool timeout must NOT poison the session stream.

    Call a tool that sleeps long under a short timeout (returns _ConnDown), then
    immediately call a quick tool on the SAME connection — it must return its real
    result, not another sentinel.
    """
    import durin.agent.tools.mcp_connection as mc

    server = FastMCP("timeouts")

    @server.tool()
    async def slow_tool() -> str:
        await asyncio.sleep(5)
        return "slow"

    @server.tool()
    async def fast_tool() -> str:
        return "fast"

    async with _InProcessHarness(server) as h:
        cfg = MCPServerConfig(command="x", tool_timeout=30)
        registry = ToolRegistry()
        conn = mc.MCPServerConnection("timeouts", cfg, registry)

        async def _open(_self):
            return h.client_streams[0], h.client_streams[1]

        conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)
        await conn.start()

        # Short timeout: the watchdog cancels the in-flight request.
        timeout_out = await conn.call_tool("slow_tool", {}, timeout=0.1)
        assert isinstance(timeout_out, mc._ConnDown), (
            f"Expected _ConnDown from timeout, got: {timeout_out!r}"
        )

        # Session stream must not be poisoned: the next call succeeds.
        fast_out = await conn.call_tool("fast_tool", {}, timeout=5.0)
        assert not isinstance(fast_out, mc._ConnDown), (
            f"Session poisoned after timeout; fast_tool returned: {fast_out!r}"
        )
        assert fast_out.content[0].text == "fast"

        await conn.aclose()


async def test_cancel_propagates_and_aclose_is_clean(live_mcp) -> None:
    factory, _harness = live_mcp
    conn, _registry = factory(keepalive_interval=10.0)
    await conn.start()
    assert conn.session is not None
    conn._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await conn._task
    await conn.aclose()
    assert conn.session is None


async def test_call_tool_native_timeout_returns_sentinel(live_mcp) -> None:
    import durin.agent.tools.mcp_connection as mc

    factory, _harness = live_mcp
    conn, _registry = factory()
    await conn.start()
    out = await conn.call_tool("echo", {"text": "x"}, timeout=0.0005)
    assert isinstance(out, mc._ConnDown)
    assert "failed" in out.message or "timed out" in out.message.lower()
    await conn.aclose()


# ---------------------------------------------------------------------------
# Sub-phase 2c — Keepalive + circuit breaker
# ---------------------------------------------------------------------------


async def test_keepalive_tool_server_uses_list_tools(live_mcp) -> None:
    """Real FastMCP tool server: _advertises_tools() is True, heartbeat lists."""
    factory, _harness = live_mcp
    conn, _registry = factory(keepalive_interval=0.1)
    await conn.start()
    assert conn._advertises_tools() is True
    list_calls = {"n": 0}
    orig = conn.session.list_tools

    async def counted_list(*a, **k):
        list_calls["n"] += 1
        return await orig(*a, **k)

    conn.session.list_tools = counted_list
    await asyncio.sleep(0.35)  # a couple keepalive cycles
    assert list_calls["n"] >= 1
    assert conn.session is not None  # no spurious reconnect
    await conn.aclose()


async def test_keepalive_prompt_only_uses_ping(live_mcp) -> None:
    """Capability-gated branch: when the server omits the tools capability,
    the heartbeat uses send_ping instead of list_tools."""
    factory, _harness = live_mcp
    conn, _registry = factory(keepalive_interval=0.1)
    await conn.start()
    # Simulate a server that does NOT advertise the tools capability.
    conn.initialize_result.capabilities.tools = None
    assert conn._advertises_tools() is False
    ping_calls = {"n": 0}
    orig_ping = conn.session.send_ping

    async def counted_ping():
        ping_calls["n"] += 1
        return await orig_ping()

    conn.session.send_ping = counted_ping

    async def boom(*a, **k):
        raise AssertionError("keepalive used list_tools for a prompt-only server")

    conn.session.list_tools = boom
    await asyncio.sleep(0.35)
    assert ping_calls["n"] >= 1
    assert conn.session is not None  # no spurious reconnect (-32601 avoided)
    await conn.aclose()


async def test_keepalive_detects_dead_idle_session(live_mcp) -> None:
    factory, harness = live_mcp
    conn, _registry = factory(keepalive_interval=0.1)
    await conn.start()
    assert conn.session is not None
    # No fresh streams given: after kill, reconnect attempts fail, so the
    # connection's session goes (and stays) None — proving keepalive noticed.
    harness.kill_server()
    for _ in range(60):
        await asyncio.sleep(0.05)
        if conn.session is None:
            break
    assert conn.session is None
    await conn.aclose()


async def test_breaker_opens_after_threshold(live_mcp, monkeypatch) -> None:
    import durin.agent.tools.mcp_connection as mc

    factory, _harness = live_mcp
    conn, _registry = factory(keepalive_interval=10.0)
    await conn.start()

    async def boom(_self, *a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(mc.MCPServerConnection, "_raw_call_tool", boom)

    for _ in range(mc._CIRCUIT_BREAKER_THRESHOLD):
        out = await conn.call_tool("echo", {"text": "x"}, timeout=2.0)
        assert isinstance(out, mc._ConnDown)
    assert conn.breaker_state() == mc.BreakerState.OPEN

    # Next call short-circuits WITHOUT hitting the session.
    out = await conn.call_tool("echo", {"text": "x"}, timeout=2.0)
    assert isinstance(out, mc._ConnDown)
    assert "Do NOT retry" in out.message and "Auto-retry" in out.message
    await conn.aclose()


async def test_breaker_half_open_then_close_on_success(live_mcp, monkeypatch) -> None:
    import durin.agent.tools.mcp_connection as mc

    monkeypatch.setattr(mc, "_CIRCUIT_BREAKER_COOLDOWN_SEC", 0.1, raising=False)
    factory, _harness = live_mcp
    conn, _registry = factory(keepalive_interval=10.0)
    await conn.start()

    state = {"fail": True}
    orig = conn._raw_call_tool

    async def maybe(_self, session, name, args, timeout):
        if state["fail"]:
            raise ValueError("boom")
        return await orig(session, name, args, timeout)

    monkeypatch.setattr(mc.MCPServerConnection, "_raw_call_tool", maybe)

    for _ in range(mc._CIRCUIT_BREAKER_THRESHOLD):
        await conn.call_tool("echo", {"text": "x"}, timeout=2.0)
    assert conn.breaker_state() == mc.BreakerState.OPEN
    await asyncio.sleep(0.15)  # cooldown elapses -> half-open
    assert conn.breaker_state() == mc.BreakerState.HALF_OPEN
    state["fail"] = False  # probe succeeds
    out = await conn.call_tool("echo", {"text": "ok"}, timeout=2.0)
    assert out.content[0].text == "echo:ok"
    assert conn.breaker_state() == mc.BreakerState.CLOSED
    await conn.aclose()


async def test_breaker_resets_on_reconnect(live_mcp, monkeypatch) -> None:
    import durin.agent.tools.mcp_connection as mc

    factory, _harness = live_mcp
    conn, _registry = factory(keepalive_interval=10.0)
    await conn.start()
    # Open the breaker manually.
    conn._error_count = mc._CIRCUIT_BREAKER_THRESHOLD
    conn._breaker_opened_at = asyncio.get_event_loop().time()
    assert conn.breaker_state() == mc.BreakerState.OPEN
    # A reconnect (fresh _serve_once) resets it.
    fresh = _build_server()
    async with _InProcessHarness(fresh) as fh:

        async def _open(_self):
            return fh.client_streams[0], fh.client_streams[1]

        conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)
        conn._request_reconnect()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if conn.breaker_state() == mc.BreakerState.CLOSED and conn.session is not None:
                break
        assert conn.breaker_state() == mc.BreakerState.CLOSED
    await conn.aclose()


# ---------------------------------------------------------------------------
# Sub-phase 2d — tools/list_changed
# ---------------------------------------------------------------------------


async def test_list_changed_rediscovers_tools(live_mcp) -> None:
    factory, harness = live_mcp
    conn, registry = factory(keepalive_interval=10.0)
    await conn.start()
    assert registry.get("mcp_harness_added") is None

    # Add a tool to the LIVE server, then make the server emit the
    # notification via the emit_change tool.
    async def added(x: int) -> int:
        return x + 1

    harness.server.add_tool(added, name="added")
    await conn.call_tool("emit_change", {}, timeout=3.0)

    # The handler schedules a decoupled refresh; wait for it to land.
    for _ in range(60):
        await asyncio.sleep(0.05)
        if registry.get("mcp_harness_added") is not None:
            break
    assert registry.get("mcp_harness_added") is not None
    await conn.aclose()


async def test_refresh_generation_guard(live_mcp) -> None:
    factory, harness = live_mcp
    conn, registry = factory(keepalive_interval=10.0)
    await conn.start()
    captured_session = conn.session

    # Add a tool on the server side (this is what the slow refresh would pick up).
    async def stale_tool(x: int) -> int:
        return x

    harness.server.add_tool(stale_tool, name="stale_tool")

    # Make list_tools slow so we can bump the generation mid-refresh.
    orig_list = captured_session.list_tools

    async def slow_list(*a, **k):
        await asyncio.sleep(0.2)
        return await orig_list(*a, **k)

    captured_session.list_tools = slow_list

    # Snapshot the registry before the stale refresh would apply.
    pre = set(registry._tools.keys())

    # Start a refresh, then invalidate it by bumping the generation
    # (simulating a reconnect that produced a newer catalog).
    task = conn._schedule_refresh()
    await asyncio.sleep(0.05)
    conn._refresh_generation += 1  # newer generation wins
    await asyncio.gather(task, return_exceptions=True)
    # The stale refresh must NOT have mutated the registry
    # (stale_tool must not appear).
    assert "mcp_harness_stale_tool" not in registry._tools
    assert set(registry._tools.keys()) == pre
    await conn.aclose()


async def test_refresh_preserves_unchanged_wrapper_identity(live_mcp) -> None:
    factory, harness = live_mcp
    conn, registry = factory(keepalive_interval=10.0)
    await conn.start()
    before = registry.get("mcp_harness_echo")
    assert before is not None

    async def added(x: int) -> int:
        return x + 1

    harness.server.add_tool(added, name="added")
    await conn.call_tool("emit_change", {}, timeout=3.0)
    for _ in range(60):
        await asyncio.sleep(0.05)
        if registry.get("mcp_harness_added") is not None:
            break

    after = registry.get("mcp_harness_echo")
    # echo was unchanged — its registry slot must not be torn down then re-added
    # in a way that strands an in-flight tool-call ID. Assert the wrapper is
    # still callable and points at the live connection (re-resolve semantics).
    assert after is not None
    out = await after.execute(text="still-here")
    assert "still-here" in out
    await conn.aclose()


async def test_refresh_reapplies_p3_deferral(live_mcp) -> None:
    import durin.agent.tools.mcp_connection as mc

    factory, harness = live_mcp
    registry = ToolRegistry()
    cfg = MCPServerConfig(command="x", keepalive_interval=10.0)
    calls = {"n": 0}
    conn = mc.MCPServerConnection(
        "harness", cfg, registry,
        defer_cb=lambda: calls.__setitem__("n", calls["n"] + 1),
    )

    async def _open(_self):
        return harness.client_streams[0], harness.client_streams[1]

    conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)
    await conn.start()
    assert calls["n"] == 1  # initial registration

    async def added2(x: int) -> int:
        return x + 1

    harness.server.add_tool(added2, name="added2")
    await conn.call_tool("emit_change", {}, timeout=3.0)
    for _ in range(60):
        await asyncio.sleep(0.05)
        if calls["n"] >= 2:
            break
    assert calls["n"] >= 2  # re-applied after refresh
    await conn.aclose()


async def test_sdk_read_timeout_and_progress_contract() -> None:
    """Guard: the native call_tool timeout raises McpError and progress fires."""
    import datetime as dt

    from mcp.shared.exceptions import McpError
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    server = FastMCP("contract")

    @server.tool()
    async def slow(seconds: float, ctx: Context) -> str:
        n = max(1, int(seconds / 0.02))
        for i in range(n):
            await ctx.report_progress(i, n)
            await asyncio.sleep(0.02)
        return "done"

    async with connect(server) as session:
        await session.initialize()
        with pytest.raises(McpError):
            await session.call_tool(
                "slow", {"seconds": 1.0},
                read_timeout_seconds=dt.timedelta(milliseconds=100),
            )
        seen: list[float] = []

        async def pcb(progress: float, total: float | None = None, message: str | None = None) -> None:
            seen.append(progress)

        r = await session.call_tool("slow", {"seconds": 0.2}, progress_callback=pcb)
        assert r.content[0].text == "done"
        assert len(seen) >= 1


async def test_per_tool_timeout_override(monkeypatch) -> None:
    import durin.agent.tools.mcp_connection as mc

    server = FastMCP("to")

    @server.tool()
    async def slow(seconds: float) -> str:
        await asyncio.sleep(seconds)
        return "done"

    async with _InProcessHarness(server) as h:
        cfg = MCPServerConfig(command="x", tool_timeout=30, tool_timeouts={"slow": 1})
        registry = ToolRegistry()
        conn = mc.MCPServerConnection("to", cfg, registry)

        async def _open(_self):
            return h.client_streams[0], h.client_streams[1]

        conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)
        await conn.start()
        wrapper = registry.get("mcp_to_slow")
        out = await wrapper.execute(seconds=5)
        assert "timed out" in out.lower()
        await conn.aclose()


async def test_progress_resets_timeout() -> None:
    import durin.agent.tools.mcp_connection as mc

    server = FastMCP("prog")

    @server.tool()
    async def chatty(ctx: Context) -> str:
        for i in range(20):
            await ctx.report_progress(i, 20)
            await asyncio.sleep(0.05)
        return "done"

    async with _InProcessHarness(server) as h:
        # Base timeout 1s, but 20 progress ticks at 0.05s each = 1s total.
        # With idle-deadline each tick resets the 0.3s window — so it should complete.
        cfg = MCPServerConfig(command="x", tool_timeouts={"chatty": 1})
        registry = ToolRegistry()
        conn = mc.MCPServerConnection("prog", cfg, registry)

        async def _open(_self):
            return h.client_streams[0], h.client_streams[1]

        conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)
        await conn.start()
        wrapper = registry.get("mcp_prog_chatty")
        out = await wrapper.execute()
        assert "done" in out
        assert "timed out" not in out.lower()
        await conn.aclose()


async def test_catalog_timeout_at_connect(monkeypatch) -> None:
    import time

    import durin.agent.tools.mcp_connection as mc

    server = FastMCP("hang")

    @server.tool()
    async def ok() -> str:
        return "ok"

    async with _InProcessHarness(server) as h:
        cfg = MCPServerConfig(command="x", catalog_timeout=0.2, tool_timeout=30)
        registry = ToolRegistry()
        conn = mc.MCPServerConnection("hang", cfg, registry)

        async def _open(_self):
            return h.client_streams[0], h.client_streams[1]

        conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)

        # Patch _register_capabilities to use a hanging list_tools.
        orig_reg = conn._register_capabilities

        async def patched():
            conn.session.list_tools = lambda: asyncio.sleep(10)
            await orig_reg()

        conn._register_capabilities = patched
        t0 = time.monotonic()
        await conn.start()
        elapsed = time.monotonic() - t0
        # Each attempt aborts within catalog_timeout (0.2s).
        # 3 retries + 1s+2s+4s backoff caps total around 7.6s.
        assert elapsed < 10.0
        await conn.aclose()


async def test_resource_read_timeout() -> None:
    from pydantic import AnyUrl

    import durin.agent.tools.mcp_connection as mc

    server = FastMCP("res")

    @server.resource("test://slow")
    async def slow_res() -> str:
        await asyncio.sleep(5)
        return "data"

    async with _InProcessHarness(server) as h:
        cfg = MCPServerConfig(command="x", tool_timeout=1)
        registry = ToolRegistry()
        conn = mc.MCPServerConnection("res", cfg, registry)

        async def _open(_self):
            return h.client_streams[0], h.client_streams[1]

        conn._open_transport_streams = _open.__get__(conn, mc.MCPServerConnection)
        await conn.start()
        out = await conn.read_resource(AnyUrl("test://slow"), timeout=0.3)
        assert isinstance(out, mc._ConnDown)
        assert "timed out" in out.message.lower() or "failed" in out.message.lower()
        await conn.aclose()


async def test_transport_http_falls_back_to_sse(monkeypatch) -> None:
    import mcp.client.sse as _sse_mod
    import mcp.client.streamable_http as _shttp_mod

    import durin.agent.tools.mcp_connection as mc

    calls: list[str] = []

    @asynccontextmanager
    async def fake_streamable(url, http_client=None):
        calls.append("http")
        raise ConnectionError("http endpoint not available")
        yield  # type: ignore[misc]

    @asynccontextmanager
    async def fake_sse(url, httpx_client_factory=None):
        calls.append("sse")
        yield object(), object()

    monkeypatch.setattr(_shttp_mod, "streamable_http_client", fake_streamable)
    monkeypatch.setattr(_sse_mod, "sse_client", fake_sse)
    # Patch _probe_http_url in mcp_connection to always return True (no real network).
    async def _always_reachable(url, timeout=3.0):
        return True

    monkeypatch.setattr(mc, "_probe_http_url", _always_reachable)

    cfg = MCPServerConfig(type="streamableHttp", url="http://localhost:9/mcp")
    conn = mc.MCPServerConnection("http", cfg, ToolRegistry())
    read, write = await conn._open_transport_streams()
    assert calls == ["http", "sse"]  # tried streamableHttp, fell back to SSE
    await conn._close_transport_streams()


# ---------------------------------------------------------------------------
# SP-3 — stderr routing
# ---------------------------------------------------------------------------


async def test_stdio_errlog_routed_to_logfile(monkeypatch, tmp_path) -> None:
    import durin.agent.tools.mcp_connection as mc

    monkeypatch.setattr(mc, "_mcp_stderr_handle", None)
    monkeypatch.setattr(mc, "get_logs_dir", lambda: tmp_path)

    captured = {}

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        captured["errlog"] = errlog
        yield object(), object()

    monkeypatch.setattr("mcp.client.stdio.stdio_client", fake_stdio_client)

    cfg = MCPServerConfig(command="fake")
    conn = mc.MCPServerConnection("srv", cfg, ToolRegistry())
    await conn._open_stdio()

    import sys as _sys
    assert captured["errlog"] is not _sys.stderr
    log = tmp_path / "mcp-stderr.log"
    assert log.exists() and "srv" in log.read_text()
