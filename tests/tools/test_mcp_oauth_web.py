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

    def builder(server, cfg, *, headless, redirect_handler, callback_handler, redirect_uri=None):
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


async def test_gateway_callback_resolves_once_by_state() -> None:
    from durin.agent.tools.mcp_oauth_web import (
        GatewayCallback,
        resolve_gateway_oauth_callback,
    )

    cb = GatewayCallback()
    cb.start()
    assert cb.state  # unguessable, non-empty

    assert resolve_gateway_oauth_callback(cb.state, code="c0de") is True
    assert await asyncio.wait_for(cb.wait(), timeout=1) == ("c0de", cb.state)
    # Single use: the same state no longer resolves.
    assert resolve_gateway_oauth_callback(cb.state, code="again") is False


async def test_gateway_callback_unknown_state_rejected() -> None:
    from durin.agent.tools.mcp_oauth_web import resolve_gateway_oauth_callback

    assert resolve_gateway_oauth_callback("nope", code="x") is False


async def test_gateway_callback_provider_error_fails_wait() -> None:
    from durin.agent.tools.mcp_oauth_web import (
        GatewayCallback,
        resolve_gateway_oauth_callback,
    )

    cb = GatewayCallback()
    cb.start()
    assert resolve_gateway_oauth_callback(cb.state, error="access_denied") is True
    with pytest.raises(RuntimeError, match="access_denied"):
        await asyncio.wait_for(cb.wait(), timeout=1)


async def test_gateway_callback_stop_deregisters() -> None:
    from durin.agent.tools.mcp_oauth_web import (
        GatewayCallback,
        resolve_gateway_oauth_callback,
    )

    cb = GatewayCallback()
    cb.start()
    cb.stop()
    assert resolve_gateway_oauth_callback(cb.state, code="x") is False


async def test_start_with_redirect_base_uses_gateway_callback() -> None:
    captured: dict = {}

    def builder(server, cfg, *, headless, redirect_handler, callback_handler, redirect_uri=None):
        captured["redirect_uri"] = redirect_uri
        captured["redirect"] = redirect_handler
        captured["callback"] = callback_handler
        return "provider-sentinel"

    async def driver(provider, cfg):
        await captured["redirect"]("https://auth/x")
        await captured["callback"]()

    flows = McpOauthFlows(provider_builder=builder, driver=driver)
    url, state = await flows.start("o", CFG, redirect_base="https://durin.tail9e5f5d.ts.net")
    assert captured["redirect_uri"] == "https://durin.tail9e5f5d.ts.net/api/v1/mcp/oauth/callback"
    assert state  # GatewayCallback state
    # Resolving via the registry completes the flow.
    from durin.agent.tools.mcp_oauth_web import resolve_gateway_oauth_callback

    assert resolve_gateway_oauth_callback(state, code="c0de") is True
    await flows._pending["o"].task
    assert not flows.is_pending("o")


async def test_start_gateway_registration_failure_does_not_leak_callback_state(
    monkeypatch,
) -> None:
    """GatewayCallback.start() registers the state in the module registry
    before ensure_registration_covers runs. If that check raises, the entry
    must not leak in _gateway_callbacks for the rest of the process."""
    import durin.agent.tools.mcp_oauth as mo
    from durin.agent.tools.mcp_oauth_web import _gateway_callbacks

    async def boom(storage, oc, redirect_uri):
        raise RuntimeError("registration check failed")

    monkeypatch.setattr(mo, "ensure_registration_covers", boom)

    async def driver(provider, cfg):
        raise AssertionError("driver must not run; registration check failed first")

    flows = McpOauthFlows(driver=driver)

    before = len(_gateway_callbacks)
    with pytest.raises(RuntimeError, match="registration check failed"):
        await flows.start("o", CFG, redirect_base="https://durin.tail9e5f5d.ts.net")

    assert len(_gateway_callbacks) == before
    assert not flows.is_pending("o")


async def test_gateway_flow_rekeys_to_sdk_own_state() -> None:
    """The mcp SDK's OAuthClientProvider generates its OWN ``state``, embeds
    it in the authorization URL, and its callback_handler must return that
    same state (the SDK verifies with ``secrets.compare_digest``). This
    driver mimics the SDK faithfully — it never reuses our GatewayCallback's
    state — to prove the real redirect (carrying the SDK's state) resolves
    instead of hanging until the 300s callback timeout."""
    import secrets as stdlib_secrets

    from durin.agent.tools.mcp_oauth_web import (
        _gateway_callbacks,
        resolve_gateway_oauth_callback,
    )

    captured: dict = {}

    def builder(server, cfg, *, headless, redirect_handler, callback_handler, redirect_uri=None):
        captured["redirect"] = redirect_handler
        captured["callback"] = callback_handler
        return "provider-sentinel"

    async def driver(provider, cfg):
        sdk_state = stdlib_secrets.token_urlsafe(32)
        captured["sdk_state"] = sdk_state
        await captured["redirect"](f"https://auth/x?client_id=c&state={sdk_state}")
        code, returned_state = await captured["callback"]()
        # Exactly the SDK's own check (mcp.client.auth.oauth2).
        assert stdlib_secrets.compare_digest(returned_state or "", sdk_state)

    flows = McpOauthFlows(provider_builder=builder, driver=driver)
    url, our_state = await flows.start(
        "o", CFG, redirect_base="https://durin.tail9e5f5d.ts.net"
    )

    sdk_state = captured["sdk_state"]
    assert sdk_state in url
    # start() reads callback.state after the redirect handler ran, so the
    # returned state already reflects the rekey — it equals the SDK's state.
    assert our_state == sdk_state

    # The real provider redirect carries the SDK's state, not ours.
    assert resolve_gateway_oauth_callback(sdk_state, code="c0de") is True

    await flows._pending["o"].task
    assert not flows.is_pending("o")
    assert sdk_state not in _gateway_callbacks


async def test_start_reports_flow_failure_reason_when_url_never_arrives() -> None:
    """When the flow task fails BEFORE the redirect handler runs (e.g. DCR
    refused), the generic "no authorization URL" error must surface the
    provider's reason instead of swallowing it."""

    async def driver(provider, cfg):
        raise RuntimeError("DCR refused: redirect_uri not allowed")

    flows, created = _build_flows({}, driver, url_timeout=0.1)
    with pytest.raises(UnavailableError, match="DCR refused"):
        await flows.start("o", CFG)
    assert not flows.is_pending("o")


async def test_start_gateway_builder_failure_does_not_leak_callback_state() -> None:
    """The provider builder call also sits between GatewayCallback.start() and
    task creation (e.g. pydantic validation of a malformed redirect_uri inside
    build_oauth_provider). If it raises, the registered state must not leak in
    _gateway_callbacks for the rest of the process."""
    from durin.agent.tools.mcp_oauth_web import _gateway_callbacks

    def builder(server, cfg, *, headless, redirect_handler, callback_handler, redirect_uri=None):
        raise ValueError("malformed redirect_uri")

    async def driver(provider, cfg):
        raise AssertionError("driver must not run; builder failed first")

    flows = McpOauthFlows(provider_builder=builder, driver=driver)

    before = len(_gateway_callbacks)
    with pytest.raises(ValueError, match="malformed redirect_uri"):
        await flows.start("o", CFG, redirect_base="https://durin.tail9e5f5d.ts.net")

    assert len(_gateway_callbacks) == before
    assert not flows.is_pending("o")
