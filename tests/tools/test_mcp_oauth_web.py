"""Gateway-driven OAuth flow orchestrator (McpOauthFlows).

Tests use injected fakes for the provider builder, loopback callback, and the
connection driver — no real browser, MCP SDK, or socket.
"""
from __future__ import annotations

import asyncio

import pytest

from durin.agent.tools.mcp_oauth_web import McpOauthFlows
from durin.config.schema import MCPServerConfig
from durin.service.types import UnavailableError

CFG = MCPServerConfig(url="https://o/mcp", oauth=True)


class _FakeLoopback:
    def __init__(self, port: int) -> None:
        self.port = port
        self.state = "st"
        self.stopped = False
        self._release = asyncio.Event()

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    async def wait(self):
        await self._release.wait()
        return ("code", "st")

    def release(self) -> None:
        self._release.set()


def _build_flows(captured: dict, driver, **kw) -> tuple[McpOauthFlows, list]:
    created: list[_FakeLoopback] = []

    def factory(port):
        lb = _FakeLoopback(port)
        created.append(lb)
        return lb

    def builder(server, cfg, *, headless, redirect_handler, callback_handler):
        captured["redirect"] = redirect_handler
        captured["callback"] = callback_handler
        return "provider-sentinel"

    flows = McpOauthFlows(
        provider_builder=builder, loopback_factory=factory, driver=driver, **kw
    )
    return flows, created


async def test_start_returns_url_then_completes_and_clears_pending() -> None:
    captured: dict = {}

    async def driver(provider, cfg):
        assert provider == "provider-sentinel"
        await captured["redirect"]("https://auth/x?state=st")
        await captured["callback"]()  # blocks until the loopback is released

    flows, created = _build_flows(captured, driver)

    url, state = await flows.start("o", CFG)
    assert url == "https://auth/x?state=st"
    assert state == "st"
    assert flows.is_pending("o")  # the driver is still awaiting the callback

    task = flows._pending["o"].task
    created[0].release()
    await task

    assert not flows.is_pending("o")
    assert created[0].stopped is True


async def test_start_calls_on_success_after_token_stored() -> None:
    captured: dict = {}
    called: list[bool] = []

    async def driver(provider, cfg):
        await captured["redirect"]("https://auth/x")
        await captured["callback"]()  # blocks until released → token stored

    flows, created = _build_flows(captured, driver)

    async def on_success() -> None:
        called.append(True)

    url, _ = await flows.start("o", CFG, on_success=on_success)
    assert url == "https://auth/x"
    assert called == []  # not yet — the callback hasn't fired

    task = flows._pending["o"].task
    created[0].release()
    await task
    assert called == [True]  # fired after the driver completed (token stored)


async def test_second_start_while_pending_cancels_and_restarts() -> None:
    """Retry is idempotent: a new start() aborts the stale pending flow and
    begins a fresh one — an abandoned popup must never lock the server out."""
    captured: dict = {}
    urls = iter(["https://auth/first", "https://auth/second"])

    async def driver(provider, cfg):
        await captured["redirect"](next(urls))
        await captured["callback"]()

    flows, created = _build_flows(captured, driver)

    url1, _ = await flows.start("o", CFG)
    assert url1 == "https://auth/first"
    first_task = flows._pending["o"].task

    url2, _ = await flows.start("o", CFG)
    assert url2 == "https://auth/second"

    # The first flow was aborted: task cancelled, its loopback stopped.
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert created[0].stopped is True

    # The second flow is the live pending one.
    assert flows.is_pending("o")
    flows.cancel("o")


async def test_flow_deadline_clears_wedged_pending() -> None:
    """A driver that hangs (e.g. handshake against an unresponsive server)
    cannot hold the pending slot forever: the flow deadline aborts it."""
    captured: dict = {}

    async def driver(provider, cfg):
        await captured["redirect"]("url")
        await asyncio.sleep(3600)  # wedged — never reaches the callback

    flows, created = _build_flows(captured, driver, flow_deadline=0.05)

    await flows.start("o", CFG)
    task = flows._pending["o"].task
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)

    assert not flows.is_pending("o")
    assert created[0].stopped is True


async def test_cancel_clears_pending_and_stops_loopback() -> None:
    captured: dict = {}

    async def driver(provider, cfg):
        await captured["redirect"]("url")
        await captured["callback"]()

    flows, created = _build_flows(captured, driver)
    await flows.start("o", CFG)
    task = flows._pending["o"].task

    flows.cancel("o")
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not flows.is_pending("o")
    assert created[0].stopped is True


async def test_drive_oauth_handshake_picks_transport_by_cfg(monkeypatch) -> None:
    # The handshake driver must honour cfg.type (and infer from a /sse URL),
    # else an SSE server is driven over streamable-HTTP and fails post-token.
    import durin.agent.tools.mcp_oauth as mo

    calls: list[str] = []

    class _Stop(Exception):
        pass

    def fake_sse(url, **kw):
        calls.append("sse")
        raise _Stop

    def fake_stream(url, **kw):
        calls.append("stream")
        raise _Stop

    monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse, raising=False)
    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client", fake_stream, raising=False
    )

    import httpx

    provider = httpx.BasicAuth("u", "p")  # a real httpx.Auth (never exercised)

    async def drive(cfg):
        with pytest.raises(_Stop):
            await mo.drive_oauth_handshake(provider, cfg)
        return calls[-1]

    assert await drive(MCPServerConfig(type="sse", url="https://x/sse")) == "sse"
    assert await drive(MCPServerConfig(type="streamableHttp", url="https://x/mcp")) == "stream"
    assert await drive(MCPServerConfig(url="https://y/v1/sse")) == "sse"  # inferred
    assert await drive(MCPServerConfig(url="https://y/mcp")) == "stream"  # inferred


async def test_start_times_out_and_cleans_up_when_no_url() -> None:
    captured: dict = {}

    async def driver(provider, cfg):
        await asyncio.sleep(5)  # never surfaces a URL

    flows, created = _build_flows(captured, driver, url_timeout=0.05)
    with pytest.raises(UnavailableError):
        await flows.start("o", CFG)
    assert not flows.is_pending("o")
    assert created[0].stopped is True
