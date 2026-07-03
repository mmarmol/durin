"""``is_local`` is server-derived from the transport peer, never client-claimed.

The OAuth loopback flows gate on ``is_local``; the ASGI glue injects it from
``request.client`` for any request model that declares the field, discarding
whatever the client sent in the query or body.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_api_app
from durin.providers import openrouter_oauth as oro
from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import AuthService
from durin.service.oauth import OAuthService
from durin.service.registry import ServiceRegistry

STATIC_TOKEN = "test-static-token"
AUTH = {"Authorization": f"Bearer {STATIC_TOKEN}"}


@pytest.fixture()
def app(tmp_path):
    auth = AuthService(store=ApiTokenStore(path=tmp_path / "tokens.json"))
    reg = ServiceRegistry()
    reg.register("oauth", OAuthService())
    reg.register("auth", auth)
    return build_api_app(reg, auth=auth, static_token=STATIC_TOKEN)


@pytest.fixture(autouse=True)
def _stub_openrouter(monkeypatch):
    monkeypatch.setattr(
        oro, "key_status", lambda: oro.OpenRouterKeyStatus(connected=False)
    )
    monkeypatch.setattr(
        oro, "start_loopback_login", lambda **_kw: "https://openrouter.ai/auth?x=1"
    )


def test_loopback_peer_gets_can_loopback(app):
    client = TestClient(app, client=("127.0.0.1", 40001))
    resp = client.get("/api/v1/oauth/openrouter/status", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["can_loopback"] is True


def test_remote_peer_does_not(app):
    client = TestClient(app, client=("203.0.113.7", 40001))
    resp = client.get("/api/v1/oauth/openrouter/status", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["can_loopback"] is False


def test_client_claim_is_discarded_on_writes(app):
    # A remote client claiming isLocal=true must still be Forbidden.
    client = TestClient(app, client=("203.0.113.7", 40001))
    resp = client.post(
        "/api/v1/oauth/openrouter/start-loopback",
        headers=AUTH,
        json={"isLocal": True},
    )
    assert resp.status_code == 403


def test_loopback_peer_can_start(app):
    client = TestClient(app, client=("::1", 40001))
    resp = client.post(
        "/api/v1/oauth/openrouter/start-loopback",
        headers=AUTH,
        json={"isLocal": False},  # stale client field — overridden by the peer
    )
    assert resp.status_code == 200
    assert resp.json()["authorize_url"].startswith("https://openrouter.ai/auth")
