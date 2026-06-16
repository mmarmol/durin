"""Characterization test for /api/models codex discovery.

After SP1 the logic lives in ``durin.service.config`` (ConfigService.models_list)
and ``_handle_models_list`` is an async shim. Build a real channel (so
``_services`` is wired), call the shim, and patch ``list_codex_models`` at its
source module since the service imports it lazily from there.
"""

import json
import types

import pytest

pytest.importorskip("oauth_cli_kit")

from durin.bus.queue import MessageBus
from durin.channels import websocket as ws
from durin.providers import codex_models
from durin.service.principal import Principal


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


async def test_models_list_uses_codex_discovery(monkeypatch):
    inst = _channel()
    monkeypatch.setattr(
        inst, "_resolve_principal", lambda request: Principal.local(), raising=False
    )
    monkeypatch.setattr(
        codex_models, "list_codex_models", lambda access_token: ["gpt-5.5", "gpt-5.4"]
    )
    req = types.SimpleNamespace(
        path="/api/models?provider=openai-codex&token=t", headers={}
    )
    resp = await inst._handle_models_list(req)
    body = json.loads(resp.body.decode("utf-8"))
    assert "gpt-5.5" in body["models"]
