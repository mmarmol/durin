import json
import types

import pytest

pytest.importorskip("oauth_cli_kit")

from durin.channels import websocket as ws
from durin.providers.codex_device_auth import CodexSessionInfo, DeviceCodeChallenge


def _handler_instance():
    return ws.WebSocketChannel.__new__(ws.WebSocketChannel)


def _ok_token(monkeypatch, inst):
    monkeypatch.setattr(inst, "_check_api_token", lambda request: True, raising=False)


def _req(path):
    return types.SimpleNamespace(path=path, headers={})


def test_settings_payload_lists_codex_as_oauth(monkeypatch):
    from durin.utils import oauth as oauth_utils

    inst = _handler_instance()
    # _settings_payload imports any_token_present from this module at call time.
    monkeypatch.setattr(oauth_utils, "any_token_present", lambda name: False)
    payload = inst._settings_payload()
    codex = [p for p in payload["providers"] if p["name"] == "openai_codex"]
    assert len(codex) == 1
    assert codex[0]["oauth"] is True
    assert codex[0]["configured"] is False
    # OAuth rows carry no api_key fields.
    assert "api_key_hint" not in codex[0]


def test_status_reports_connected(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    info = CodexSessionInfo(email="u@x.com", plan="pro", source="durin")
    monkeypatch.setattr(ws, "existing_codex_session", lambda: info)
    resp = inst._handle_codex_oauth_status(_req("/api/oauth/codex/status?token=t"))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["connected"] is True and body["email"] == "u@x.com"


def test_start_returns_challenge(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    ch = DeviceCodeChallenge(
        user_code="WXYZ-1",
        verification_uri="https://auth.openai.com/codex/device",
        device_auth_id="dev_1",
        interval=5,
        expires_in=900,
    )
    monkeypatch.setattr(ws, "request_device_code", lambda: ch)
    resp = inst._handle_codex_oauth_start(_req("/api/oauth/codex/start?token=t"))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["user_code"] == "WXYZ-1" and body["device_auth_id"] == "dev_1"


def test_disconnect(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    monkeypatch.setattr(ws, "codex_disconnect", lambda: True)
    monkeypatch.setattr(ws, "existing_codex_session", lambda: None)
    resp = inst._handle_codex_oauth_disconnect(_req("/api/oauth/codex/disconnect?token=t"))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["connected"] is False
