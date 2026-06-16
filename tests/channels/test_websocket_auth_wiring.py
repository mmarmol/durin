"""Channel-level tests for SP2 auth wiring.

Covers:
- _resolve_principal resolution order (store, static, legacy in-memory)
- Restart-survival: a token minted via the store resolves after the in-memory
  dicts are cleared (simulates a process restart)
- Bootstrap token is persisted so it survives a restart
- Media secret is loaded from the store (stable across instances)
"""

from __future__ import annotations

import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.channels.websocket import WebSocketChannel
from durin.security.api_tokens import ApiTokenStore
from durin.service.principal import Scope


def _channel(tmp_path: Path, **extra) -> WebSocketChannel:
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 0,
        "path": "/",
        "websocketRequiresToken": False,
        **extra,
    }
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    return WebSocketChannel(cfg, bus)


def _req(path: str = "/api/x?token=dummy", headers: dict | None = None):
    return types.SimpleNamespace(path=path, headers=headers or {})


# ---------------------------------------------------------------------------
# _resolve_principal — resolution order
# ---------------------------------------------------------------------------


def test_resolve_principal_returns_none_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path)
    req = _req("/api/x", headers={})
    assert ch._resolve_principal(req) is None


def test_resolve_principal_accepts_persisted_store_token(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    store = ApiTokenStore(path=tmp_path / "api_tokens.json")
    _, plaintext = store.issue(["secrets:read"], label="test")
    ch = _channel(tmp_path)
    # Wire the same store path so the channel's AuthService finds the token.
    ch._services.get("auth")._store._path = tmp_path / "api_tokens.json"
    req = _req(headers={"Authorization": f"Bearer {plaintext}"})
    principal = ch._resolve_principal(req)
    assert principal is not None
    assert principal.kind == "remote"
    assert principal.has_scope(Scope.SECRETS_READ)
    assert not principal.has_scope(Scope.ADMIN)


def test_resolve_principal_accepts_static_config_token(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path, token="mysecrettoken")
    req = _req(headers={"Authorization": "Bearer mysecrettoken"})
    principal = ch._resolve_principal(req)
    assert principal is not None
    assert principal.has_scope(Scope.ADMIN)
    assert principal.subject == "static"


def test_resolve_principal_accepts_legacy_in_memory_token(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path)
    token = "nbwt_legacytoken"
    ch._api_tokens[token] = time.monotonic() + 300
    req = _req(headers={"Authorization": f"Bearer {token}"})
    principal = ch._resolve_principal(req)
    assert principal is not None
    assert principal.has_scope(Scope.ADMIN)
    assert principal.subject == "legacy"


def test_resolve_principal_rejects_expired_in_memory_token(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path)
    token = "nbwt_expiredtoken"
    ch._api_tokens[token] = time.monotonic() - 1  # already expired
    req = _req(headers={"Authorization": f"Bearer {token}"})
    assert ch._resolve_principal(req) is None


def test_resolve_principal_accepts_token_query_param(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path, token="qtok")
    req = _req("/api/x?token=qtok")
    principal = ch._resolve_principal(req)
    assert principal is not None
    assert principal.has_scope(Scope.ADMIN)


# ---------------------------------------------------------------------------
# Restart-survival: token in store survives clearing in-memory dicts
# ---------------------------------------------------------------------------


def test_restart_survival_persisted_token(tmp_path, monkeypatch):
    """Simulate a restart: mint token via store, clear in-memory dicts,
    confirm _resolve_principal STILL accepts the token (store path is stable)."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path)
    # Point channel's auth store to the test tmp_path.
    store_path = tmp_path / "api_tokens.json"
    ch._services.get("auth")._store._path = store_path

    # Mint via store.
    store = ApiTokenStore(path=store_path)
    _, plaintext = store.issue([Scope.ADMIN.value], label="restart-test")

    # Clear both in-memory dicts to simulate a restart (no legacy token).
    ch._api_tokens.clear()
    ch._issued_tokens.clear()

    # Token must still resolve via the persisted store.
    req = _req(headers={"Authorization": f"Bearer {plaintext}"})
    principal = ch._resolve_principal(req)
    assert principal is not None, "persisted token must resolve after in-memory dicts are cleared"
    assert principal.has_scope(Scope.ADMIN)


# ---------------------------------------------------------------------------
# Bootstrap persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_token_resolves_via_store(tmp_path, monkeypatch):
    """Bootstrap mints a token into the store; clearing in-memory dicts still
    leaves the token resolvable (restart-survival at the channel level)."""
    import asyncio
    import functools

    import httpx

    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path)
    # Point the channel's store to tmp_path so the test is isolated.
    store_path = tmp_path / "api_tokens.json"
    ch._services.get("auth")._store._path = store_path

    # Start the server.
    port = 29970
    ch.config.port = port
    ch.config.host = "127.0.0.1"
    server_task = asyncio.create_task(ch.start())
    await asyncio.sleep(0.3)
    try:
        boot = await asyncio.to_thread(
            functools.partial(httpx.get, f"http://127.0.0.1:{port}/webui/bootstrap", timeout=5.0)
        )
        assert boot.status_code == 200
        token = boot.json()["token"]

        # Clear in-memory dicts to simulate a restart.
        ch._api_tokens.clear()
        ch._issued_tokens.clear()

        # Token must still resolve via the persisted store.
        req = _req(headers={"Authorization": f"Bearer {token}"})
        principal = ch._resolve_principal(req)
        assert principal is not None, "bootstrap token must resolve after in-memory dicts are cleared"
        assert principal.has_scope(Scope.ADMIN)
    finally:
        await ch.stop()
        await server_task


# ---------------------------------------------------------------------------
# Media secret stability (loaded from store, same across instances)
# ---------------------------------------------------------------------------


def test_media_secret_stable_across_channel_instances(tmp_path, monkeypatch):
    """Two channels sharing the same store path must produce the same media secret."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch1 = _channel(tmp_path)
    ch2 = _channel(tmp_path)
    assert ch1._media_secret == ch2._media_secret
