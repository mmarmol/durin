import json
import types

import pytest

pytest.importorskip("oauth_cli_kit")

from durin.channels import websocket as ws


def test_models_list_uses_codex_discovery(monkeypatch):
    inst = ws.WebSocketChannel.__new__(ws.WebSocketChannel)
    monkeypatch.setattr(inst, "_check_api_token", lambda request: True, raising=False)
    monkeypatch.setattr(ws, "list_codex_models", lambda access_token: ["gpt-5.5", "gpt-5.4"])
    req = types.SimpleNamespace(
        path="/api/models?provider=openai-codex&token=t", headers={}
    )
    resp = inst._handle_models_list(req)
    body = json.loads(resp.body.decode("utf-8"))
    assert "gpt-5.5" in body["models"]
