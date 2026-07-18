"""Restart-safe OAuth refresh: cold-loaded tokens must refresh, not re-auth.

The mcp SDK tracks access-token expiry only in memory, computed at token
exchange time; its ``_initialize`` loads persisted tokens WITHOUT recomputing
``token_expiry_time`` (it stays ``None``), and ``is_token_valid()`` treats a
``None`` expiry as valid forever. So after any restart, an already-expired
cold-loaded access token looks valid: the proactive-refresh branch of
``async_auth_flow`` never fires, the dead token is sent, the server 401s, and
the SDK falls through to a FULL re-authorization — which in durin's headless
agent runtime raises ``NeedsInteractiveAuthError`` and loses the connection
even though the stored (single-use, rotating) refresh token is still good.

durin owns this because the SDK cannot restore expiry on its own: the persisted
``OAuthToken`` carries only a relative ``expires_in`` with no issuance anchor.
The fix persists an absolute ``expires_at`` at ``set_tokens`` and restores it
into ``token_expiry_time`` on cold load, so ``is_token_valid()`` is honest and
the refresh path runs. These tests pin that behavior end to end.
"""
from __future__ import annotations

import json
import time

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from durin.agent.tools import mcp_oauth as mo

pytestmark = pytest.mark.asyncio

SRV = "srv"
URL = "https://mcp.example.com"


@pytest.fixture()
def isolated_secrets(tmp_path, monkeypatch):
    """Point the process-wide secret store at a throwaway secrets.json."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path", lambda: secrets_file
    )
    import durin.security.secrets as s

    s._STORE = None  # drop the process-wide cache so it rebuilds at tmp path
    yield secrets_file
    s._STORE = None


def _storage() -> mo.SecretsTokenStorage:
    return mo.SecretsTokenStorage(SRV, server_url=URL)


def _provider():
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url=URL, oauth=True)
    provider = mo.build_oauth_provider(SRV, cfg, headless=True)
    assert isinstance(provider, mo.WriteAheadOAuthProvider)
    return provider


# ---- set_tokens persists an absolute expiry ----


async def test_set_tokens_persists_absolute_expires_at(isolated_secrets):
    storage = _storage()
    before = time.time()
    await storage.set_tokens(
        OAuthToken(access_token="a", refresh_token="r", token_type="Bearer", expires_in=3600)
    )
    exp = storage.read_expires_at()
    assert exp is not None
    # expires_at ≈ now + expires_in (allow a wide slack for slow CI).
    assert before + 3600 - 5 <= exp <= time.time() + 3600 + 5


async def test_set_tokens_without_expires_in_clears_expiry(isolated_secrets):
    storage = _storage()
    # Seed a stale expiry, then store a token that carries no expires_in.
    storage._write(storage._expires_name, json.dumps({"expires_at": time.time() + 999}))
    await storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer"))
    assert storage.read_expires_at() is None


async def test_forget_removes_expires_at(isolated_secrets):
    storage = _storage()
    await storage.set_tokens(
        OAuthToken(access_token="a", refresh_token="r", token_type="Bearer", expires_in=3600)
    )
    assert storage.read_expires_at() is not None
    assert storage.forget() is True
    assert storage.read_expires_at() is None


# ---- cold load restores expiry so is_token_valid() is honest ----


async def _persist_cold_session(*, expires_at: float | None) -> None:
    """Persist a client registration + token as if a prior process signed in.

    ``expires_at`` None simulates a token stored before this fix (no expiry
    companion entry); a past value simulates the access token having expired
    while the process was down.
    """
    storage = _storage()
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="cid-123", redirect_uris=["http://127.0.0.1:1456/callback"]
        )
    )
    await storage.set_tokens(
        OAuthToken(
            access_token="cold-access", refresh_token="cold-refresh",
            token_type="Bearer", expires_in=3600,
        )
    )
    if expires_at is None:
        storage._delete(storage._expires_name)
    else:
        storage._write(storage._expires_name, json.dumps({"expires_at": expires_at}))


async def test_cold_load_expired_access_token_is_invalid(isolated_secrets):
    """The regression: an expired cold-loaded token must read as INVALID so the
    SDK refreshes instead of sending it and re-authorizing on the 401."""
    await _persist_cold_session(expires_at=time.time() - 100)  # expired 100s ago
    provider = _provider()
    await provider._initialize()
    assert provider.context.current_tokens is not None
    assert provider.context.is_token_valid() is False
    assert provider.context.can_refresh_token() is True  # refresh path will run


async def test_cold_load_missing_expiry_forces_refresh(isolated_secrets):
    """A token stored before this fix (no persisted expiry, e.g. the live box)
    must be treated as expired on load so the first request refreshes."""
    await _persist_cold_session(expires_at=None)
    provider = _provider()
    await provider._initialize()
    assert provider.context.current_tokens is not None
    assert provider.context.is_token_valid() is False
    assert provider.context.can_refresh_token() is True


async def test_cold_load_still_valid_token_not_force_refreshed(isolated_secrets):
    """A token whose access token is genuinely still valid (quick restart) must
    stay valid — no gratuitous refresh / refresh-token rotation on every start."""
    await _persist_cold_session(expires_at=time.time() + 3000)
    provider = _provider()
    await provider._initialize()
    assert provider.context.is_token_valid() is True


# ---- behavioral: the flow actually emits a refresh, not a re-auth ----


async def test_cold_load_expired_yields_refresh_request(isolated_secrets):
    """Drive the real SDK async_auth_flow one step: with an expired cold-loaded
    token + valid refresh token, the FIRST request it emits must be a
    refresh_token grant — proof the refresh path fires instead of sending the
    dead access token straight into a 401 → full re-auth."""
    import httpx

    await _persist_cold_session(expires_at=time.time() - 100)
    provider = _provider()

    gen = provider.async_auth_flow(httpx.Request("GET", f"{URL}/sse"))
    first = await gen.asend(None)
    try:
        body = bytes(first.content or b"")
        assert first.method == "POST"
        assert b"grant_type=refresh_token" in body
        # and the write-ahead marker was laid down before the refresh request
        assert mo.refresh_inflight_marker(SRV, URL) is not None
    finally:
        await gen.aclose()


async def test_cold_load_expired_refreshes_end_to_end(isolated_secrets):
    """Drive the FULL SDK async_auth_flow against a mock token endpoint: an
    expired cold-loaded token must recover by refreshing — new rotated token
    persisted, write-ahead marker cleared, original request re-sent with the
    fresh bearer, and NO fall-through to full re-auth."""
    import httpx

    await _persist_cold_session(expires_at=time.time() - 100)
    provider = _provider()
    storage = _storage()

    original = httpx.Request("GET", f"{URL}/sse")
    gen = provider.async_auth_flow(original)

    # 1) The flow first emits a refresh_token grant (not the dead access token).
    refresh_req = await gen.asend(None)
    assert refresh_req.method == "POST"
    assert b"grant_type=refresh_token" in bytes(refresh_req.content or b"")

    # 2) Server rotates the token; the flow then re-emits the original request
    #    carrying the FRESH bearer.
    rotated = httpx.Response(
        200,
        json={
            "access_token": "fresh-access", "refresh_token": "fresh-refresh",
            "token_type": "Bearer", "expires_in": 3600,
        },
        request=refresh_req,
    )
    authed_req = await gen.asend(rotated)
    assert authed_req.headers.get("Authorization") == "Bearer fresh-access"

    # 3) Original request now succeeds → flow ends without a 401 → no re-auth.
    with pytest.raises(StopAsyncIteration):
        await gen.asend(httpx.Response(200, request=authed_req))

    # Recovery is durable: rotated token persisted, marker cleared, expiry ahead.
    reloaded = await storage.get_tokens()
    assert reloaded is not None and reloaded.access_token == "fresh-access"
    assert mo.refresh_inflight_marker(SRV, URL) is None
    exp = storage.read_expires_at()
    assert exp is not None and exp > time.time() + 3000


# ---- SDK contract pins (fail loudly on an mcp bump that moves our seams) ----


async def test_sdk_contract_pin_initialize_and_expiry():
    import inspect

    from mcp.client.auth import OAuthClientProvider

    init = getattr(OAuthClientProvider, "_initialize", None)
    assert init is not None and inspect.iscoroutinefunction(init), (
        "mcp SDK no longer has async OAuthClientProvider._initialize — "
        "WriteAheadOAuthProvider's cold-load expiry restore is broken; re-anchor it"
    )
    src = inspect.getsource(init)
    assert "storage.get_tokens" in src, (
        "_initialize no longer loads tokens from storage — re-anchor the expiry restore"
    )
    # The expiry semantics we depend on: is_token_valid() trusts token_expiry_time,
    # and the context exposes it. If either moves, our restore is a silent no-op.
    ctx_src = inspect.getsource(
        __import__("mcp.client.auth.oauth2", fromlist=["OAuthContext"]).OAuthContext.is_token_valid
    )
    assert "token_expiry_time" in ctx_src, (
        "OAuthContext.is_token_valid no longer keys off token_expiry_time — "
        "re-anchor durin's cold-load expiry restore"
    )
