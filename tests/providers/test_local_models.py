"""Tests for local provider live model discovery and provider_catalog integration."""

from __future__ import annotations

import json

import httpx

import durin.providers.provider_catalog as pc
from durin.providers import local_models as lm

# ---------------------------------------------------------------------------
# list_local_models unit tests
# ---------------------------------------------------------------------------


def _mock_client(handler):
    """Return a _client replacement that uses a MockTransport."""
    return lambda timeout: httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)


def test_list_local_models_parses_data(monkeypatch):
    """list_local_models returns ids from {"data":[{"id":...}]}."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    monkeypatch.setattr(lm, "_client", _mock_client(handler))
    assert lm.list_local_models("http://localhost:11434/v1") == ["m1", "m2"]


def test_list_local_models_sends_api_key(monkeypatch):
    """list_local_models sends an Authorization header when api_key is supplied."""
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": [{"id": "x"}]})

    monkeypatch.setattr(lm, "_client", _mock_client(handler))
    lm.list_local_models("http://localhost:1234/v1", api_key="sk-test")
    assert received["auth"] == "Bearer sk-test"


def test_list_local_models_returns_empty_on_connection_error(monkeypatch):
    """list_local_models returns [] when the server is unreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(lm, "_client", _mock_client(handler))
    assert lm.list_local_models("http://localhost:11434/v1") == []


def test_list_local_models_returns_empty_on_http_error(monkeypatch):
    """list_local_models returns [] on a non-200 HTTP status."""
    monkeypatch.setattr(
        lm, "_client", _mock_client(lambda req: httpx.Response(503))
    )
    assert lm.list_local_models("http://localhost:11434/v1") == []


def test_list_local_models_strips_trailing_slash(monkeypatch):
    """Trailing slash on api_base does not produce a double-slash URL."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(lm, "_client", _mock_client(handler))
    lm.list_local_models("http://localhost:11434/v1/")
    assert seen["path"] == "/v1/models"


# ---------------------------------------------------------------------------
# provider_catalog.provider_models integration tests
# ---------------------------------------------------------------------------


def _write_index(tmp_path, entries: list[dict], provider: str = "ollama"):
    idx = tmp_path / "provider_models.json"
    idx.write_text(
        json.dumps({"schema_version": 1, "providers": {provider: entries}}),
        encoding="utf-8",
    )
    return idx


def _mock_cfg(api_base: str | None = "http://localhost:11434/v1", api_key: str | None = None):
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.providers.ollama.api_base = api_base
    cfg.providers.ollama.api_key = api_key
    return cfg


def test_provider_models_ollama_live_wins_over_static(tmp_path, monkeypatch):
    """When live discovery succeeds, provider_models returns those models."""
    idx = _write_index(tmp_path, [{"id": "static-model"}])
    monkeypatch.setattr(pc, "_INDEX_PATH", idx)
    pc._load_index.cache_clear()

    monkeypatch.setattr(lm, "list_local_models", lambda *a, **kw: ["live1", "live2"])
    monkeypatch.setattr(pc, "_load_config_for_local", lambda: _mock_cfg())

    models = pc.provider_models("ollama")
    assert [m.id for m in models] == ["live1", "live2"]
    pc._load_index.cache_clear()


def test_provider_models_ollama_falls_back_to_static(tmp_path, monkeypatch):
    """When live discovery returns [], provider_models falls back to static catalog."""
    idx = _write_index(tmp_path, [{"id": "static-model"}])
    monkeypatch.setattr(pc, "_INDEX_PATH", idx)
    pc._load_index.cache_clear()

    monkeypatch.setattr(lm, "list_local_models", lambda *a, **kw: [])
    monkeypatch.setattr(pc, "_load_config_for_local", lambda: _mock_cfg(api_base=None))

    models = pc.provider_models("ollama")
    assert [m.id for m in models] == ["static-model"]
    pc._load_index.cache_clear()


def test_provider_models_non_local_skips_live_query(monkeypatch):
    """Non-local providers do not trigger live model discovery."""
    called: list = []
    monkeypatch.setattr(lm, "list_local_models", lambda *a, **kw: called.append(1) or [])

    pc.provider_models("anthropic")
    assert called == [], "list_local_models must not be called for non-local providers"
