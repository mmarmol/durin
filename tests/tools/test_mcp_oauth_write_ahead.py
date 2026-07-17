"""Write-ahead marker around single-use refresh-token rotation.

If the process dies between the OAuth provider consuming the (single-use,
rotating) refresh token and durin persisting the replacement, the stored
token is silently dead. The marker turns that into a detectable state:
written just before the refresh request is built, cleared when the new
tokens are persisted. An orphaned marker = interrupted refresh.
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from durin.agent.tools import mcp_oauth as mo


@pytest.fixture()
def isolated_secrets(tmp_path, monkeypatch):
    """Point the secret store at a temp DURIN_HOME (mirrors
    test_mcp_oauth_storage.py's ``store`` fixture) and assert isolation by
    round-tripping one secret in the fixture itself."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path", lambda: secrets_file
    )
    import durin.security.secrets as s

    s._STORE = None  # drop the process-wide cache so it rebuilds at tmp path

    # Isolation self-check: a secret written here must not leak across tests.
    from durin.security.secrets import SecretStore

    store = SecretStore().load()
    store.put(
        "MCP_ISOLATION_CHECK",
        value="1",
        service="mcp:isolation-check",
        scope=["mcp:isolation-check"],
    )
    store.save()
    assert secrets_file.exists()

    yield secrets_file
    s._STORE = None


def test_marker_roundtrip(isolated_secrets):
    storage = mo.SecretsTokenStorage("srv", server_url="https://mcp.example.com")
    assert mo.refresh_inflight_marker("srv", "https://mcp.example.com") is None
    storage.write_refresh_marker()
    marker = mo.refresh_inflight_marker("srv", "https://mcp.example.com")
    assert marker is not None and marker["server"] == "srv" and marker["ts"]
    storage.clear_refresh_marker()
    assert mo.refresh_inflight_marker("srv", "https://mcp.example.com") is None


def test_set_tokens_clears_marker(isolated_secrets):
    storage = mo.SecretsTokenStorage("srv", server_url="https://mcp.example.com")
    storage.write_refresh_marker()

    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer")))
    assert mo.refresh_inflight_marker("srv", "https://mcp.example.com") is None
    assert asyncio.run(storage.get_tokens()) is not None


def test_provider_writes_marker_before_building_refresh_request(isolated_secrets):
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url="https://mcp.example.com", oauth=True)
    provider = mo.build_oauth_provider("srv", cfg, headless=True)
    assert isinstance(provider, mo.WriteAheadOAuthProvider)

    # Seed context so _refresh_token() (the SDK's refresh-request builder)
    # can build a request: it needs current_tokens.refresh_token and
    # client_info.client_id (see oauth2.py's OAuthClientProvider._refresh_token).
    provider.context.current_tokens = OAuthToken(
        access_token="old-access", refresh_token="old-refresh", token_type="Bearer"
    )
    provider.context.client_info = OAuthClientInformationFull(
        client_id="cid-123",
        redirect_uris=["http://127.0.0.1:1456/callback"],
    )

    request = asyncio.run(provider._refresh_token())
    assert request is not None
    assert mo.refresh_inflight_marker("srv", "https://mcp.example.com") is not None


def test_auth_failure_message_plain_when_no_marker(isolated_secrets):
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url="https://mcp.example.com", oauth=True)
    msg = mo.auth_failure_message("srv", cfg)
    assert "durin mcp login srv" in msg
    assert "interrupted mid-rotation" not in msg


def test_auth_failure_message_enriched_when_orphan_marker(isolated_secrets):
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url="https://mcp.example.com", oauth=True)
    mo.SecretsTokenStorage("srv", server_url="https://mcp.example.com").write_refresh_marker()

    msg = mo.auth_failure_message("srv", cfg)
    assert "interrupted mid-rotation" in msg
    assert "stored refresh token is likely already consumed" in msg
    assert "durin mcp login srv" in msg


def test_sdk_contract_pin():
    """Fail loudly when an mcp bump changes the hook we wrap."""
    import inspect

    from mcp.client.auth import OAuthClientProvider

    hook = getattr(OAuthClientProvider, "_refresh_token", None)
    assert hook is not None, (
        "mcp SDK no longer has OAuthClientProvider._refresh_token — "
        "WriteAheadOAuthProvider's write-ahead wrap is broken; re-anchor it"
    )
    assert inspect.iscoroutinefunction(hook)
    sig = inspect.signature(hook)
    assert list(sig.parameters) == ["self"], f"unexpected signature: {sig}"
