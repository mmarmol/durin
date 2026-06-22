"""Channel-level tests for SP2 auth wiring.

The bearer-token resolution order (store / static / unknown) is covered by
``tests/api/test_principal_resolver.py`` against the live front-door resolver
``resolve_principal_from_headers``. The legacy in-memory ``_api_tokens`` pool
and the HTTP query-param token path were removed in SP8 — the only credential
surfaces left are the persisted store (HTTP) and the single-use issued-token
pool (WS handshake).

What remains channel-specific and worth asserting here:
- Restart-survival: a token minted via the store resolves after the in-memory
  pool is cleared (simulates a process restart).
- Bootstrap persists its token to the store so it survives a restart.
- Media secret is loaded from the store (stable across instances).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from durin.api.asgi import resolve_principal_from_headers
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


# ---------------------------------------------------------------------------
# Restart-survival: token in store survives clearing the in-memory pool
# ---------------------------------------------------------------------------


def test_restart_survival_persisted_token(tmp_path, monkeypatch):
    """Mint a token via the store, clear the in-memory pool (simulating a
    restart), and confirm the front-door resolver STILL accepts it."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path)
    # Point the channel's auth store to the test tmp_path.
    store_path = tmp_path / "api_tokens.json"
    auth = ch._services.get("auth")
    auth._store._path = store_path

    # Mint via store.
    store = ApiTokenStore(path=store_path)
    _, plaintext = store.issue([Scope.ADMIN.value], label="restart-test")

    # Clear the single-use issued-token pool to simulate a restart.
    ch._issued_tokens.clear()

    # Token must still resolve via the persisted store.
    principal = resolve_principal_from_headers(
        {"authorization": f"Bearer {plaintext}"}, auth=auth
    )
    assert principal is not None, "persisted token must resolve after a restart"
    assert principal.has_scope(Scope.ADMIN)


# ---------------------------------------------------------------------------
# Bootstrap persistence
# ---------------------------------------------------------------------------


def test_bootstrap_token_resolves_via_store(tmp_path, monkeypatch):
    """Bootstrap mints a token into the store; clearing the in-memory pool still
    leaves the token resolvable (restart-survival at the channel level)."""
    from starlette.testclient import TestClient

    from durin.api.asgi import build_gateway_http_app

    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path)
    # Point the channel's store to tmp_path so the test is isolated.
    store_path = tmp_path / "api_tokens.json"
    auth = ch._services.get("auth")
    auth._store._path = store_path

    app = build_gateway_http_app(ch, ch._services, auth=auth)
    client = TestClient(app)

    boot = client.get("/webui/bootstrap")
    assert boot.status_code == 200
    token = boot.json()["token"]

    # Clear the in-memory issued-token pool to simulate a restart.
    ch._issued_tokens.clear()

    # Token must still resolve via the persisted store.
    principal = resolve_principal_from_headers(
        {"authorization": f"Bearer {token}"}, auth=auth
    )
    assert principal is not None, "bootstrap token must resolve after a restart"
    assert principal.has_scope(Scope.ADMIN)


# ---------------------------------------------------------------------------
# webui session cookie (httpOnly, opaque, revocable)
# ---------------------------------------------------------------------------


def _secret_gated_app(tmp_path, monkeypatch, secret: str = "hunter2"):
    from durin.api.asgi import build_gateway_http_app

    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch = _channel(tmp_path, tokenIssueSecret=secret)
    auth = ch._services.get("auth")
    auth._store._path = tmp_path / "api_tokens.json"
    return build_gateway_http_app(ch, ch._services, auth=auth), ch


def test_bootstrap_secret_login_sets_httponly_session_cookie(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    app, _ch = _secret_gated_app(tmp_path, monkeypatch)
    client = TestClient(app)

    boot = client.get("/webui/bootstrap", headers={"X-Durin-Auth": "hunter2"})
    assert boot.status_code == 200
    set_cookie = boot.headers.get("set-cookie", "")
    assert "durin_session=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    # The secret is NOT echoed back in the JSON body.
    assert "hunter2" not in boot.text
    # The session token is opaque, not the secret.
    assert "hunter2" not in set_cookie


def test_bootstrap_reauthorizes_via_session_cookie_without_secret(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    app, _ch = _secret_gated_app(tmp_path, monkeypatch)
    client = TestClient(app)

    # Initial sign-in with the secret sets the cookie (kept by the client jar).
    first = client.get("/webui/bootstrap", headers={"X-Durin-Auth": "hunter2"})
    assert first.status_code == 200

    # Reload: no secret header, but the durin_session cookie re-authorizes.
    again = client.get("/webui/bootstrap")
    assert again.status_code == 200
    assert again.json()["token"]


def test_bootstrap_rejects_without_secret_or_cookie(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    app, _ch = _secret_gated_app(tmp_path, monkeypatch)
    client = TestClient(app)  # fresh jar — no cookie

    res = client.get("/webui/bootstrap")
    assert res.status_code == 401


def test_signout_revokes_session_and_clears_cookie(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    app, _ch = _secret_gated_app(tmp_path, monkeypatch)
    client = TestClient(app)

    client.get("/webui/bootstrap", headers={"X-Durin-Auth": "hunter2"})
    out = client.post("/webui/signout")
    assert out.status_code == 200
    # Cookie cleared client-side (max-age=0) AND token revoked server-side, so a
    # subsequent secret-less bootstrap is rejected.
    after = client.get("/webui/bootstrap")
    assert after.status_code == 401


# ---------------------------------------------------------------------------
# Media secret stability (loaded from store, same across instances)
# ---------------------------------------------------------------------------


def test_media_secret_stable_across_channel_instances(tmp_path, monkeypatch):
    """Two channels sharing the same store path must produce the same media secret."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    ch1 = _channel(tmp_path)
    ch2 = _channel(tmp_path)
    assert ch1._media_secret == ch2._media_secret
