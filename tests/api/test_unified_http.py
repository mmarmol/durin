"""The unified Starlette HTTP app (``build_gateway_http_app``) serves the full
gateway HTTP surface from one app — the `/api/v1` front door + `/webui/bootstrap`
+ `/api/media` + the SPA — building the responses from the channel's
``bootstrap`` / ``media_fetch`` methods.

Also pins the bootstrap localhost gate (SEC-1): unauthenticated ADMIN-token
minting is allowed only from a loopback peer, or with the configured secret.
"""

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus


def _build_app(tmp_path, monkeypatch, **cfg_extra):
    # Isolate the persisted token store (bootstrap mints into it) to a tmp dir.
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    spa = tmp_path / "dist"
    spa.mkdir(exist_ok=True)
    (spa / "index.html").write_text(
        "<!doctype html><title>durin</title><div id=root></div>", encoding="utf-8"
    )

    from durin.api.asgi import build_gateway_http_app
    from durin.channels.websocket import WebSocketChannel

    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
        **cfg_extra,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    auth = registry.get("auth")
    return build_gateway_http_app(channel, registry, auth=auth, static_dist_path=spa)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    return _build_app(tmp_path, monkeypatch)


@pytest.fixture()
def client(app):
    return TestClient(app)


def _token(client: TestClient) -> str:
    r = client.get("/webui/bootstrap")
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_bootstrap_mints_a_token(client):
    r = client.get("/webui/bootstrap")
    assert r.status_code == 200
    assert r.json().get("token")


def test_front_door_v1_secrets_with_token(client):
    tok = _token(client)
    r = client.get("/api/v1/secrets", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert "secrets" in r.json()


def test_spa_index_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "durin" in r.text


def test_spa_history_fallback_to_index(client):
    # A client-side route (not an asset, not /api) falls back to index.html.
    r = client.get("/settings/general")
    assert r.status_code == 200
    assert "durin" in r.text


def test_spa_index_served_no_cache(client):
    # The SPA shell must be no-cache so a redeploy's new hashed-asset references
    # are picked up on the next load (stale index.html → stale JS otherwise).
    for path in ("/", "/settings/general"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache", (path, dict(r.headers))


def test_provider_models_endpoint_serves_catalog(client):
    # E2E through the real gateway HTTP app: the new Providers-settings endpoint
    # serves the per-provider catalog (vendored provider_models.json).
    tok = _token(client)
    r = client.get(
        "/api/v1/providers/models?provider=zai_coding_plan",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["provider"] == "zai_coding_plan"
    ids = [m["id"] for m in data["models"]]
    assert "glm-5.2" in ids  # the model the old shortlist missed


def test_model_picker_endpoint_serves_entries(client, monkeypatch):
    # E2E: the picker endpoint resolves through the catalog. Keep codex
    # undetected so the build stays network-free regardless of the host's store.
    import durin.providers.codex_device_auth as _cda

    monkeypatch.setattr(_cda, "codex_token_present", lambda: False)
    tok = _token(client)
    r = client.get("/api/v1/model/picker", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert "entries" in r.json()


def test_media_bad_signature_rejected(client):
    # A forged signature must not return a file (the handler returns 401
    # "invalid signature").
    r = client.get("/api/media/deadbeef/Zm9v")
    assert r.status_code in (401, 403, 404)


# -- SEC-1: bootstrap localhost gate -----------------------------------------


def test_bootstrap_allowed_from_localhost_no_secret(app):
    # A loopback peer with no token_issue_secret configured may mint a token.
    c = TestClient(app, client=("127.0.0.1", 0))
    assert c.get("/webui/bootstrap").status_code == 200


def test_bootstrap_rejected_from_remote_when_no_secret(app):
    # Regression guard (SEC-1): a NON-loopback peer must NOT mint an ADMIN token
    # when no token_issue_secret is configured. Before the fix the ASGI adapter
    # handed bootstrap a hardcoded localhost peer, so any remote caller
    # got 200 — a privilege-escalation on a non-loopback bind.
    c = TestClient(app, client=("203.0.113.7", 0))  # TEST-NET-3, definitely remote
    r = c.get("/webui/bootstrap")
    assert r.status_code == 403, r.text


def test_bootstrap_remote_allowed_only_with_secret(tmp_path, monkeypatch):
    # With a token_issue_secret configured, a remote caller is gated on the
    # secret (the reverse-proxy deployment path), not on the peer IP.
    app = _build_app(tmp_path, monkeypatch, tokenIssueSecret="s3cr3t")
    c = TestClient(app, client=("203.0.113.7", 0))
    assert c.get("/webui/bootstrap").status_code == 401  # missing secret
    r = c.get("/webui/bootstrap", headers={"Authorization": "Bearer s3cr3t"})
    assert r.status_code == 200, r.text
