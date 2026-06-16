import asyncio
import json

import durin.channels.websocket as ws
from durin.service.health import HealthService
from durin.service.principal import Principal
from durin.service.registry import ServiceRegistry


def _channel():
    c = ws.WebSocketChannel.__new__(ws.WebSocketChannel)
    c._resolve_principal = lambda req: Principal.local()
    registry = ServiceRegistry()
    registry.register("health", HealthService())
    c._services = registry
    return c


def test_status_reports_present(monkeypatch):
    import durin.extras as ex
    monkeypatch.setattr(ex, "_module_present", lambda m: True)
    c = _channel()
    resp = asyncio.run(c._handle_extras_status(None, {"feature": ["web_search"]}))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["present"] is True
    assert body["extra"] == "web"
    assert body["needs_restart"] is False


def test_status_unknown_feature_400():
    c = _channel()
    resp = asyncio.run(c._handle_extras_status(None, {"feature": ["nope"]}))
    assert resp.status_code == 400


def test_ensure_invokes_ensure_extra_and_restarts(monkeypatch):
    import durin.extras as ex
    monkeypatch.setattr(
        ex, "ensure_extra",
        lambda feature, *, config: ex.EnsureResult("installed", feature, True, ""),
    )
    monkeypatch.setattr("durin.config.loader.load_config", lambda: object())
    c = _channel()
    called = {"restart": 0}
    c._spawn_gateway_restart = lambda: called.__setitem__("restart", called["restart"] + 1)
    resp = asyncio.run(
        c._handle_extras_ensure(None, {"feature": ["cross_encoder"], "restart": ["true"]})
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["status"] == "installed"
    assert body["needs_restart"] is True
    assert body["restarting"] is True
    assert called["restart"] == 1


def test_ensure_unknown_feature_400(monkeypatch):
    monkeypatch.setattr("durin.config.loader.load_config", lambda: object())
    c = _channel()
    resp = asyncio.run(c._handle_extras_ensure(None, {"feature": ["nope"]}))
    assert resp.status_code == 400
