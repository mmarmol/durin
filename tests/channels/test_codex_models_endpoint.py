"""Characterization test for GET /api/v1/models codex discovery.

The logic lives in ``durin.service.config`` (ConfigService.models_list); this
drives it through the unified v1 front door and patches ``list_codex_models_async`` at
its source module since the service imports it lazily from there.
"""

import pytest
from starlette.testclient import TestClient

pytest.importorskip("oauth_cli_kit")

from durin.api.asgi import build_gateway_http_app
from durin.bus.queue import MessageBus
from durin.channels import websocket as ws
from durin.providers import codex_models


def _channel():
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    return ws.WebSocketChannel(cfg, MessageBus())


def test_models_list_uses_codex_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)

    async def _fake_discovery(access_token):
        return ["codex-discovery-sentinel", "gpt-5.4"]

    monkeypatch.setattr(codex_models, "list_codex_models_async", _fake_discovery)
    inst = _channel()
    app = build_gateway_http_app(inst, inst._services, auth=inst._services.get("auth"))
    client = TestClient(app)
    tok = client.get("/webui/bootstrap").json()["token"]
    resp = client.get(
        "/api/v1/models?provider=openai-codex",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 200
    assert "codex-discovery-sentinel" in resp.json()["models"]
