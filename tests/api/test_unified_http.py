"""Step 2 of the ASGI unify: the unified Starlette HTTP app
(``build_gateway_http_app``) serves the full gateway HTTP surface from one app —
the `/api/v1` front door + legacy `/api/*` + `/webui/bootstrap` + `/api/media` +
the SPA — reusing the channel's now transport-neutral handlers (parity).
"""

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate the persisted token store (bootstrap mints into it) to a tmp dir.
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    spa = tmp_path / "dist"
    spa.mkdir()
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
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(channel, registry, auth=auth, static_dist_path=spa)
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


def test_media_bad_signature_rejected(client):
    # A forged signature must not return a file (the handler returns 401
    # "invalid signature").
    r = client.get("/api/media/deadbeef/Zm9v")
    assert r.status_code in (401, 403, 404)
