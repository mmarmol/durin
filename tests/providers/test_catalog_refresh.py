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
