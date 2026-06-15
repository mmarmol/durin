"""Tests for SP-4b: MCPServerConfig.oauth field + provider builder + headless redirect."""
from __future__ import annotations

import durin.security.secrets as _secrets


def _point_store_at(tmp_path, monkeypatch):
    """Redirect the secret store to a throwaway path."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr("durin.security.secrets._default_secrets_path", lambda: secrets_file)
    _secrets._STORE = None
    return secrets_file


# ---- 4b.1: MCPServerConfig.oauth field ----

def test_oauth_field_defaults_off():
    from durin.config.schema import MCPServerConfig
    cfg = MCPServerConfig(url="https://api.example/mcp")
    assert cfg.oauth is None


def test_oauth_bool_true():
    from durin.config.schema import MCPServerConfig
    cfg = MCPServerConfig.model_validate(
        {"url": "https://api.example/mcp", "oauth": True}
    )
    assert cfg.oauth is True


def test_oauth_dict_with_scope_and_client():
    from durin.config.schema import MCPServerConfig
    cfg = MCPServerConfig.model_validate(
        {
            "url": "https://api.example/mcp",
            "oauth": {"scope": "read write", "clientId": "static-cid"},
        }
    )
    assert cfg.oauth.scope == "read write"
    assert cfg.oauth.client_id == "static-cid"


def test_oauth_config_normalizer_none():
    from durin.config.schema import MCPServerConfig
    cfg = MCPServerConfig(url="https://api.example/mcp")
    assert cfg.oauth_config() is None


def test_oauth_config_normalizer_bool():
    from durin.config.schema import MCPServerConfig, MCPOAuthConfig
    cfg = MCPServerConfig.model_validate({"url": "https://api.example/mcp", "oauth": True})
    oc = cfg.oauth_config()
    assert isinstance(oc, MCPOAuthConfig)
    assert oc.scope is None
    assert oc.callback_port == 1456


def test_oauth_config_normalizer_dict():
    from durin.config.schema import MCPServerConfig, MCPOAuthConfig
    cfg = MCPServerConfig.model_validate(
        {"url": "https://api.example/mcp", "oauth": {"scope": "read", "callbackPort": 1457}}
    )
    oc = cfg.oauth_config()
    assert isinstance(oc, MCPOAuthConfig)
    assert oc.scope == "read"
    assert oc.callback_port == 1457


# ---- 4b.2: provider builder + headless redirect ----

import pytest


def test_build_provider_is_httpx_auth(tmp_path, monkeypatch):
    _point_store_at(tmp_path, monkeypatch)
    import httpx
    from mcp.client.auth import OAuthClientProvider
    from durin.config.schema import MCPServerConfig
    from durin.agent.tools.mcp_oauth import build_oauth_provider

    cfg = MCPServerConfig(url="https://api.example/mcp", oauth=True)
    provider = build_oauth_provider("acme", cfg, headless=True)
    assert isinstance(provider, OAuthClientProvider)
    assert isinstance(provider, httpx.Auth)


def test_build_provider_metadata(tmp_path, monkeypatch):
    _point_store_at(tmp_path, monkeypatch)
    from durin.config.schema import MCPServerConfig
    from durin.agent.tools.mcp_oauth import build_oauth_provider

    cfg = MCPServerConfig.model_validate(
        {"url": "https://api.example/mcp", "oauth": {"scope": "read", "callbackPort": 1457}}
    )
    provider = build_oauth_provider("acme", cfg, headless=True)
    md = provider.context.client_metadata
    assert md.client_name == "durin"
    assert md.scope == "read"
    assert str(md.redirect_uris[0]) == "http://127.0.0.1:1457/callback"


@pytest.mark.asyncio
async def test_headless_redirect_refuses(tmp_path, monkeypatch):
    from durin.agent.tools.mcp_oauth import (
        NeedsInteractiveAuthError,
        make_headless_redirect_handler,
    )

    handler = make_headless_redirect_handler("acme")
    with pytest.raises(NeedsInteractiveAuthError) as ei:
        await handler("https://auth.example/authorize?x=1")
    assert "durin mcp login acme" in str(ei.value)
