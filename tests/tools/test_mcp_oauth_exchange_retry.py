"""Transient-invalid_grant retry around the OAuth authorization-code exchange.

Some OAuth providers (notably Cloudflare workers-oauth-provider deployments
such as mcp.atlassian.com) store the grant in an eventually-consistent KV at
consent time; a token exchange arriving seconds later at a different edge can
miss the read and answer 400 invalid_grant ("Grant not found or authorization
code expired") even though the code is fresh and unconsumed. durin's
gateway-callback sign-in makes that window systematic: the browser (consent
write) and the gateway (exchange read) are on different networks, and consent
auto-approval keeps every attempt inside the propagation window.

A failed grant *lookup* does not consume the single-use code, so re-sending
the identical exchange request is safe and succeeds once the store catches up.
These tests drive the httpx-auth-flow wrapper that does exactly that.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from durin.agent.tools import mcp_oauth as mo


def _exchange_request() -> httpx.Request:
    return httpx.Request(
        "POST",
        "https://mcp.example.com/v1/token",
        data={
            "grant_type": "authorization_code",
            "code": "user:grant:secret",
            "redirect_uri": "https://gw.example.com/api/v1/mcp/oauth/callback",
            "client_id": "cid",
            "code_verifier": "ver",
        },
    )


def _invalid_grant_response(request: httpx.Request, description: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={"error": "invalid_grant", "error_description": description},
        request=request,
    )


def _make_inner(requests: list[httpx.Request], received: list[httpx.Response]):
    """Scripted stand-in for the SDK's async_auth_flow generator."""

    async def _inner():
        for req in requests:
            received.append((yield req))

    return _inner()


class _SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def test_transient_invalid_grant_retried_then_success_forwarded():
    """400 invalid_grant on the code exchange re-yields the SAME request; the
    eventual 200 is what the inner (SDK) flow receives."""

    async def scenario():
        received: list[httpx.Response] = []
        req = _exchange_request()
        sleep = _SleepRecorder()
        flow = mo.retry_transient_exchange_flow(
            _make_inner([req], received), delays=(0.01, 0.02), sleep=sleep
        )

        first = await flow.__anext__()
        assert first is req

        retry = await flow.asend(
            _invalid_grant_response(req, "Grant not found or authorization code expired")
        )
        assert retry is req  # identical request re-sent, code not regenerated

        ok = httpx.Response(200, json={"access_token": "t"}, request=req)
        with pytest.raises(StopAsyncIteration):
            await flow.asend(ok)

        assert sleep.delays == [0.01]
        assert received == [ok]

    asyncio.run(scenario())


def test_retries_exhausted_forward_last_failure_to_inner():
    async def scenario():
        received: list[httpx.Response] = []
        req = _exchange_request()
        sleep = _SleepRecorder()
        flow = mo.retry_transient_exchange_flow(
            _make_inner([req], received), delays=(0.01, 0.02), sleep=sleep
        )

        await flow.__anext__()
        fail = _invalid_grant_response(req, "Grant not found or authorization code expired")
        assert (await flow.asend(fail)) is req
        assert (await flow.asend(fail)) is req
        with pytest.raises(StopAsyncIteration):
            await flow.asend(fail)  # delays exhausted: forwarded inward

        assert sleep.delays == [0.01, 0.02]
        assert received == [fail]

    asyncio.run(scenario())


def test_inner_exception_propagates_after_forward():
    """When the forwarded failure makes the SDK raise (OAuthTokenError in real
    life), the wrapper must surface that exception, not swallow it."""

    async def scenario():
        req = _exchange_request()

        async def _raising_inner():
            yield req
            raise ValueError("token exchange failed")

        flow = mo.retry_transient_exchange_flow(
            _raising_inner(), delays=(), sleep=_SleepRecorder()
        )
        await flow.__anext__()
        with pytest.raises(ValueError, match="token exchange failed"):
            await flow.asend(
                _invalid_grant_response(req, "Grant not found or authorization code expired")
            )

    asyncio.run(scenario())


def test_non_exchange_request_never_retried():
    async def scenario():
        received: list[httpx.Response] = []
        req = httpx.Request("GET", "https://mcp.example.com/.well-known/oauth-authorization-server")
        sleep = _SleepRecorder()
        flow = mo.retry_transient_exchange_flow(
            _make_inner([req], received), delays=(0.01,), sleep=sleep
        )

        await flow.__anext__()
        fail = httpx.Response(400, json={"error": "invalid_grant"}, request=req)
        with pytest.raises(StopAsyncIteration):
            await flow.asend(fail)

        assert sleep.delays == []
        assert received == [fail]

    asyncio.run(scenario())


def test_refresh_grant_never_retried():
    """A dead refresh token is definitive (rotation semantics own that case);
    only the authorization_code exchange gets the consistency retry."""

    async def scenario():
        received: list[httpx.Response] = []
        req = httpx.Request(
            "POST",
            "https://mcp.example.com/v1/token",
            data={"grant_type": "refresh_token", "refresh_token": "r", "client_id": "cid"},
        )
        sleep = _SleepRecorder()
        flow = mo.retry_transient_exchange_flow(
            _make_inner([req], received), delays=(0.01,), sleep=sleep
        )

        await flow.__anext__()
        fail = httpx.Response(400, json={"error": "invalid_grant"}, request=req)
        with pytest.raises(StopAsyncIteration):
            await flow.asend(fail)

        assert sleep.delays == []
        assert received == [fail]

    asyncio.run(scenario())


def test_other_400_errors_never_retried():
    async def scenario():
        received: list[httpx.Response] = []
        req = _exchange_request()
        sleep = _SleepRecorder()
        flow = mo.retry_transient_exchange_flow(
            _make_inner([req], received), delays=(0.01,), sleep=sleep
        )

        await flow.__anext__()
        fail = httpx.Response(400, json={"error": "invalid_client"}, request=req)
        with pytest.raises(StopAsyncIteration):
            await flow.asend(fail)

        assert sleep.delays == []
        assert received == [fail]

    asyncio.run(scenario())


def test_non_json_400_never_retried():
    async def scenario():
        received: list[httpx.Response] = []
        req = _exchange_request()
        sleep = _SleepRecorder()
        flow = mo.retry_transient_exchange_flow(
            _make_inner([req], received), delays=(0.01,), sleep=sleep
        )

        await flow.__anext__()
        fail = httpx.Response(400, text="<html>bad gateway</html>", request=req)
        with pytest.raises(StopAsyncIteration):
            await flow.asend(fail)

        assert sleep.delays == []
        assert received == [fail]

    asyncio.run(scenario())


def test_success_path_untouched():
    async def scenario():
        received: list[httpx.Response] = []
        req = _exchange_request()
        sleep = _SleepRecorder()
        flow = mo.retry_transient_exchange_flow(
            _make_inner([req], received), delays=(0.01,), sleep=sleep
        )

        await flow.__anext__()
        ok = httpx.Response(200, json={"access_token": "t"}, request=req)
        with pytest.raises(StopAsyncIteration):
            await flow.asend(ok)

        assert sleep.delays == []
        assert received == [ok]

    asyncio.run(scenario())


def test_multi_request_inner_flow_passthrough():
    """Discovery GETs and the DCR POST flow through untouched; only the
    exchange leg is inspected."""

    async def scenario():
        received: list[httpx.Response] = []
        discovery = httpx.Request("GET", "https://mcp.example.com/.well-known/x")
        register = httpx.Request(
            "POST", "https://mcp.example.com/v1/register", json={"client_name": "durin"}
        )
        exchange = _exchange_request()
        sleep = _SleepRecorder()
        flow = mo.retry_transient_exchange_flow(
            _make_inner([discovery, register, exchange], received),
            delays=(0.01,),
            sleep=sleep,
        )

        assert (await flow.__anext__()) is discovery
        assert (await flow.asend(httpx.Response(200, request=discovery))) is register
        assert (
            await flow.asend(httpx.Response(201, json={"client_id": "c"}, request=register))
        ) is exchange
        retry = await flow.asend(
            _invalid_grant_response(exchange, "Grant not found or authorization code expired")
        )
        assert retry is exchange
        with pytest.raises(StopAsyncIteration):
            await flow.asend(httpx.Response(200, json={"access_token": "t"}, request=exchange))

        assert sleep.delays == [0.01]
        assert [r.status_code for r in received] == [200, 201, 200]

    asyncio.run(scenario())


def test_write_ahead_provider_overrides_auth_flow():
    """The provider must route httpx through the retry wrapper — an SDK bump
    that renames async_auth_flow would silently drop the retry otherwise."""
    from mcp.client.auth import OAuthClientProvider

    assert "async_auth_flow" in mo.WriteAheadOAuthProvider.__dict__
    assert (
        mo.WriteAheadOAuthProvider.async_auth_flow
        is not OAuthClientProvider.async_auth_flow
    )
    # The wrapper relies on httpx pre-reading response bodies (aread() inside
    # the flow); the SDK opts in for us — pin it.
    assert OAuthClientProvider.requires_response_body is True


def test_leaf_errors_flattens_nested_exception_groups():
    """The sign-in failure log must name the real cause, not
    'ExceptionGroup: unhandled errors in a TaskGroup'."""
    from durin.agent.tools.mcp_oauth_web import leaf_errors

    inner = ValueError("boom")
    nested = ExceptionGroup("outer", [ExceptionGroup("mid", [inner]), KeyError("k")])
    leaves = leaf_errors(nested)
    assert inner in leaves
    assert any(isinstance(e, KeyError) for e in leaves)
    assert len(leaves) == 2

    plain = RuntimeError("plain")
    assert leaf_errors(plain) == [plain]


def test_exchange_detection_requires_form_encoding():
    """A JSON MCP payload that merely CONTAINS the literal string must not be
    mistaken for the token exchange."""
    req = httpx.Request(
        "POST",
        "https://mcp.example.com/mcp",
        json={"note": "grant_type=authorization_code"},
    )
    assert mo._is_code_exchange_request(req) is False
    assert mo._is_code_exchange_request(_exchange_request()) is True
