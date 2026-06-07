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
    from durin.providers import codex_device_auth

    inst = _handler_instance()
    # _settings_payload imports codex_token_present from this module at call
    # time to decide the codex row's `configured` flag — mock it so the test is
    # hermetic regardless of the dev machine's real ~/.durin codex token.
    monkeypatch.setattr(codex_device_auth, "codex_token_present", lambda: False)
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


def _req_host(host):
    return types.SimpleNamespace(path="/api/oauth/codex/x?token=t", headers={"Host": host})


def test_status_reports_can_loopback_for_localhost(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    monkeypatch.setattr(ws, "existing_codex_session", lambda: None)
    local = json.loads(inst._handle_codex_oauth_status(_req_host("localhost:8765")).body)
    remote = json.loads(inst._handle_codex_oauth_status(_req_host("example.com")).body)
    assert local["can_loopback"] is True
    assert remote["can_loopback"] is False


def test_start_loopback_returns_url_for_local(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    monkeypatch.setattr(
        ws, "start_loopback_login", lambda: "https://auth.openai.com/oauth/authorize?x=1"
    )
    body = json.loads(inst._handle_codex_oauth_start_loopback(_req_host("127.0.0.1:8765")).body)
    assert body["authorize_url"].startswith("https://auth.openai.com/oauth/authorize")


def test_start_loopback_rejected_when_remote(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    resp = inst._handle_codex_oauth_start_loopback(_req_host("example.com"))
    assert resp.status_code == 400


def test_settings_update_accepts_oauth_provider_with_token(monkeypatch):
    from durin.config import loader as cfgloader
    from durin.providers import codex_device_auth as cda

    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    monkeypatch.setattr(inst, "_settings_payload", lambda **k: {"ok": True}, raising=False)
    cfg = types.SimpleNamespace(
        agents=types.SimpleNamespace(
            defaults=types.SimpleNamespace(provider="auto", model="")
        ),
        providers=types.SimpleNamespace(
            openai_codex=types.SimpleNamespace(api_key=None, api_base=None)
        ),
    )
    monkeypatch.setattr(cfgloader, "load_config", lambda: cfg)
    saved = []
    monkeypatch.setattr(cfgloader, "save_config", lambda c: saved.append(c))
    monkeypatch.setattr(cda, "codex_token_present", lambda: True)
    req = types.SimpleNamespace(
        path="/api/settings/update?model=gpt-5.5&provider=openai_codex&token=t", headers={}
    )
    resp = inst._handle_settings_update(req)
    assert resp.status_code == 200, resp.body
    assert cfg.agents.defaults.provider == "openai_codex"
    assert saved
