from __future__ import annotations

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from durin.agent.tools.mcp_oauth import SecretsTokenStorage

pytestmark = pytest.mark.asyncio


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the process-wide secret store at a throwaway secrets.json."""
    secrets = tmp_path / "secrets.json"
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path", lambda: secrets
    )
    import durin.security.secrets as s
    s._STORE = None  # drop the process-wide cache so it rebuilds at tmp path
    yield secrets
    s._STORE = None


async def test_get_tokens_none_when_absent(store):
    storage = SecretsTokenStorage("acme")
    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None


async def test_token_round_trip(store):
    storage = SecretsTokenStorage("acme")
    tok = OAuthToken(
        access_token="at-123",
        refresh_token="rt-456",
        expires_in=3600,
        scope="read write",
    )
    await storage.set_tokens(tok)

    reloaded = await storage.get_tokens()
    assert reloaded is not None
    assert reloaded.access_token == "at-123"
    assert reloaded.refresh_token == "rt-456"
    assert reloaded.expires_in == 3600
    assert reloaded.scope == "read write"


async def test_client_info_round_trip(store):
    storage = SecretsTokenStorage("acme")
    info = OAuthClientInformationFull(
        client_id="cid-789",
        client_secret="csecret",
        redirect_uris=["http://127.0.0.1:1456/callback"],
    )
    await storage.set_client_info(info)

    reloaded = await storage.get_client_info()
    assert reloaded is not None
    assert reloaded.client_id == "cid-789"
    assert reloaded.client_secret == "csecret"
    assert str(reloaded.redirect_uris[0]) == "http://127.0.0.1:1456/callback"


async def test_keyed_per_server(store):
    a = SecretsTokenStorage("acme")
    b = SecretsTokenStorage("globex")
    await a.set_tokens(OAuthToken(access_token="a-tok"))
    assert await b.get_tokens() is None  # b's slot is independent
    assert (await a.get_tokens()).access_token == "a-tok"


async def test_url_change_invalidates(store):
    """A server whose URL changed must not reuse the old creds (opencode)."""
    a = SecretsTokenStorage("acme", server_url="https://old.example/mcp")
    await a.set_tokens(OAuthToken(access_token="old-tok"))
    moved = SecretsTokenStorage("acme", server_url="https://new.example/mcp")
    assert await moved.get_tokens() is None


async def test_corrupt_blob_returns_none(store):
    """A non-JSON / schema-broken secret value must not raise."""
    from durin.security.secrets import store_secret
    store_secret(
        "MCP_ACME_OAUTH_TOKENS",
        "not-json{",
        service="mcp:acme",
        scope=["mcp:acme"],
        origin="oauth",
    )
    storage = SecretsTokenStorage("acme")
    assert await storage.get_tokens() is None
