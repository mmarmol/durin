"""Gateway-driven OAuth flow orchestrator (McpOauthFlows).

Tests use injected fakes for the provider builder, loopback callback, and the
connection driver — no real browser, MCP SDK, or socket.
"""
from __future__ import annotations

import asyncio

import pytest

from durin.agent.tools.mcp_oauth_web import McpOauthFlows
from durin.config.schema import MCPServerConfig
from durin.service.types import ConflictError, UnavailableError

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


async def test_second_start_while_pending_is_conflict() -> None:
    captured: dict = {}

    async def driver(provider, cfg):
        await captured["redirect"]("url")
        await captured["callback"]()

    flows, _ = _build_flows(captured, driver)
    await flows.start("o", CFG)
    with pytest.raises(ConflictError):
        await flows.start("o", CFG)
    flows.cancel("o")


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


async def test_start_times_out_and_cleans_up_when_no_url() -> None:
    captured: dict = {}

    async def driver(provider, cfg):
        await asyncio.sleep(5)  # never surfaces a URL

    flows, created = _build_flows(captured, driver, url_timeout=0.05)
    with pytest.raises(UnavailableError):
        await flows.start("o", CFG)
    assert not flows.is_pending("o")
    assert created[0].stopped is True
