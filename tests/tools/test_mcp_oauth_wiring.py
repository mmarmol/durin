"""Tests for SP-4b.3: OAuthClientProvider attached to the HTTP/SSE transport.

These are pure unit tests — no real OAuth flow, no network.
"""
from __future__ import annotations

import pytest

import durin.security.secrets as _secrets


def _point_store_at(tmp_path, monkeypatch):
    """Redirect the secret store to a throwaway path."""
    secrets_file = tmp_path / "secrets.json"
    monkeypatch.setattr("durin.security.secrets._default_secrets_path", lambda: secrets_file)
    _secrets._STORE = None
    return secrets_file


pytestmark = pytest.mark.asyncio


async def test_no_provider_when_oauth_unset(tmp_path, monkeypatch):
    """A server without oauth configured must not build a provider."""
    _point_store_at(tmp_path, monkeypatch)
    from durin.agent.tools.mcp_connection import MCPServerConnection
    from durin.agent.tools.registry import ToolRegistry
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url="https://api.example/mcp")
    conn = MCPServerConnection("plain", cfg, ToolRegistry())
    assert conn._oauth_provider is None


async def test_provider_built_when_oauth_true(tmp_path, monkeypatch):
    """A server with oauth=True must have a provider built in __init__."""
    _point_store_at(tmp_path, monkeypatch)
    import httpx
    from mcp.client.auth import OAuthClientProvider

    from durin.agent.tools.mcp_connection import MCPServerConnection
    from durin.agent.tools.registry import ToolRegistry
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url="https://api.example/mcp", oauth=True)
    conn = MCPServerConnection("acme", cfg, ToolRegistry())
    assert conn._oauth_provider is not None
    assert isinstance(conn._oauth_provider, OAuthClientProvider)
    assert isinstance(conn._oauth_provider, httpx.Auth)


async def test_streamable_http_attaches_provider_as_auth(monkeypatch, tmp_path):
    """In _open_streamable_http, the httpx.AsyncClient must receive auth=<provider>."""
    _point_store_at(tmp_path, monkeypatch)
    from durin.agent.tools.mcp_connection import MCPServerConnection
    from durin.agent.tools.registry import ToolRegistry
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url="https://api.example/mcp", oauth=True)
    conn = MCPServerConnection("acme", cfg, ToolRegistry())
    assert conn._oauth_provider is not None

    captured: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            captured["auth"] = kw.get("auth")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

    async def _ok(_url):
        return True

    monkeypatch.setattr("durin.agent.tools.mcp_connection._probe_http_url", _ok)

    class _FakeTransportCM:
        async def __aenter__(self):
            return ("r", "w", lambda: None)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client",
        lambda url, http_client=None, **kw: _FakeTransportCM(),
    )

    read, write = await conn._open_streamable_http()
    assert captured["auth"] is conn._oauth_provider


async def test_streamable_http_no_auth_when_oauth_unset(monkeypatch, tmp_path):
    """Without oauth, _open_streamable_http must not pass auth= to the httpx client."""
    _point_store_at(tmp_path, monkeypatch)
    from durin.agent.tools.mcp_connection import MCPServerConnection
    from durin.agent.tools.registry import ToolRegistry
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url="https://api.example/mcp")
    conn = MCPServerConnection("plain", cfg, ToolRegistry())
    assert conn._oauth_provider is None

    captured: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            captured["auth"] = kw.get("auth")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

    async def _ok(_url):
        return True

    monkeypatch.setattr("durin.agent.tools.mcp_connection._probe_http_url", _ok)

    class _FakeTransportCM:
        async def __aenter__(self):
            return ("r", "w", lambda: None)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client",
        lambda url, http_client=None, **kw: _FakeTransportCM(),
    )

    await conn._open_streamable_http()
    # auth should be None (not passed) when no oauth configured
    assert captured.get("auth") is None


async def test_sse_factory_uses_provider_auth(tmp_path, monkeypatch):
    """In _open_sse, the factory passed to sse_client must use auth=provider."""
    _point_store_at(tmp_path, monkeypatch)
    from durin.agent.tools.mcp_connection import MCPServerConnection
    from durin.agent.tools.registry import ToolRegistry
    from durin.config.schema import MCPServerConfig

    cfg = MCPServerConfig(url="https://api.example/sse", type="sse", oauth=True)
    conn = MCPServerConnection("acme", cfg, ToolRegistry())

    captured: dict = {}

    class _FakeClient:
        def __init__(self, *a, **kw):
            captured["auth"] = kw.get("auth")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

    async def _ok(_url):
        return True

    monkeypatch.setattr("durin.agent.tools.mcp_connection._probe_http_url", _ok)

    captured_factory: dict = {}

    class _FakeSSECM:
        async def __aenter__(self):
            return ("r", "w")

        async def __aexit__(self, *exc):
            return False

    def _fake_sse(url, httpx_client_factory=None, **kw):
        captured_factory["factory"] = httpx_client_factory
        return _FakeSSECM()

    monkeypatch.setattr("mcp.client.sse.sse_client", _fake_sse)
    await conn._open_sse()

    # Emulate the SDK calling the factory with auth=<provider>
    captured_factory["factory"](auth=conn._oauth_provider)
    assert captured["auth"] is conn._oauth_provider
