import json

import durin.providers.provider_catalog as pc
from durin.providers import catalog_refresh


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._payload


def test_refresh_writes_cache_and_overlay_wins(tmp_path, monkeypatch):
    fake = {
        "zai-coding-plan": {
            "models": {
                "glm-9": {
                    "id": "glm-9",
                    "modalities": {"input": ["text"]},
                    "limit": {"context": 99, "output": 9},
                }
            }
        }
    }
    monkeypatch.setattr(
        catalog_refresh.urllib.request, "urlopen",
        lambda *a, **k: _Resp(json.dumps(fake).encode()),
    )
    monkeypatch.setattr(catalog_refresh, "fetch_nvidia_model_ids", lambda: None)
    assert catalog_refresh.refresh_provider_models_cache(tmp_path) is True
    cache = tmp_path / "provider_models_cache.json"
    assert cache.exists()
    written = json.loads(cache.read_text())
    assert written["providers"]["zai_coding_plan"][0]["id"] == "glm-9"

    # Overlay: point the catalog at this cache → it wins over the vendored floor.
    monkeypatch.setattr(pc, "_user_cache_path", lambda: cache)
    pc._load_index.cache_clear()
    assert any(m.id == "glm-9" for m in pc.provider_models("zai_coding_plan"))
    pc._load_index.cache_clear()


def test_refresh_returns_false_on_network_error(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise OSError("offline")

    monkeypatch.setattr(catalog_refresh.urllib.request, "urlopen", _boom)
    assert catalog_refresh.refresh_provider_models_cache(tmp_path) is False
    assert not (tmp_path / "provider_models_cache.json").exists()


_FAKE_MD = {
    "nvidia": {
        "models": {
            "mistralai/mistral-7b-instruct-v03": {
                "id": "mistralai/mistral-7b-instruct-v03",
                "reasoning": True,
                "modalities": {"input": ["text"]},
                "limit": {"context": 32_000, "output": 4_096},
            },
            "z-ai/glm-5.1": {
                "id": "z-ai/glm-5.1",
                "modalities": {"input": ["text"]},
                "limit": {"context": 128_000, "output": 8_192},
            },
        }
    }
}


def test_refresh_nvidia_ids_come_from_live_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        catalog_refresh.urllib.request, "urlopen",
        lambda *a, **k: _Resp(json.dumps(_FAKE_MD).encode()),
    )
    monkeypatch.setattr(
        catalog_refresh, "fetch_nvidia_model_ids",
        lambda: ["mistralai/mistral-7b-instruct-v0.3"],
    )
    assert catalog_refresh.refresh_provider_models_cache(tmp_path) is True
    written = json.loads((tmp_path / "provider_models_cache.json").read_text())
    nv = written["providers"]["nvidia"]
    # Live spelling wins, caps carried over; the model gone from live is dropped.
    assert [e["id"] for e in nv] == ["mistralai/mistral-7b-instruct-v0.3"]
    assert nv[0]["supports_reasoning"] is True


def test_refresh_omits_nvidia_when_live_endpoint_fails(tmp_path, monkeypatch):
    # No nvidia key in the cache → the overlay falls through to the vendored
    # floor instead of resurrecting models.dev's drifted list.
    monkeypatch.setattr(
        catalog_refresh.urllib.request, "urlopen",
        lambda *a, **k: _Resp(json.dumps(_FAKE_MD).encode()),
    )
    monkeypatch.setattr(catalog_refresh, "fetch_nvidia_model_ids", lambda: None)
    assert catalog_refresh.refresh_provider_models_cache(tmp_path) is True
    written = json.loads((tmp_path / "provider_models_cache.json").read_text())
    assert "nvidia" not in written["providers"]
