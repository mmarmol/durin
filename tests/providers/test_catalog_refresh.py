import json

import durin.providers.provider_catalog as pc
from durin.providers import catalog_refresh


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
        catalog_refresh, "_default_fetch", lambda url: json.dumps(fake).encode()
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
    def _boom(url):
        raise OSError("offline")

    monkeypatch.setattr(catalog_refresh, "_default_fetch", _boom)
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
        catalog_refresh, "_default_fetch", lambda url: json.dumps(_FAKE_MD).encode()
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
        catalog_refresh, "_default_fetch", lambda url: json.dumps(_FAKE_MD).encode()
    )
    monkeypatch.setattr(catalog_refresh, "fetch_nvidia_model_ids", lambda: None)
    assert catalog_refresh.refresh_provider_models_cache(tmp_path) is True
    written = json.loads((tmp_path / "provider_models_cache.json").read_text())
    assert "nvidia" not in written["providers"]


# ---------------------------------------------------------------------------
# CatalogRefreshScheduler: due time persists across restarts (cache mtime)
# ---------------------------------------------------------------------------


def test_initial_wait_zero_when_cache_missing(tmp_path):
    """No cache file → the catalog is overdue: first fetch happens immediately."""
    sched = catalog_refresh.CatalogRefreshScheduler(tmp_path, interval_hours=24)
    assert sched._initial_wait() == 0.0


def test_initial_wait_zero_when_cache_overdue(tmp_path):
    """Cache older than the interval → first fetch happens immediately."""
    import os
    import time

    cache = tmp_path / "provider_models_cache.json"
    cache.write_text("{}", encoding="utf-8")
    stale = time.time() - 25 * 3600
    os.utime(cache, (stale, stale))

    sched = catalog_refresh.CatalogRefreshScheduler(tmp_path, interval_hours=24)
    assert sched._initial_wait() == 0.0


def test_initial_wait_remaining_when_cache_fresh(tmp_path):
    """Fresh cache → first wait is the REMAINING time, not a full interval
    restarting from zero (the pre-fix behavior reset the clock every boot)."""
    import os
    import time

    cache = tmp_path / "provider_models_cache.json"
    cache.write_text("{}", encoding="utf-8")
    half_ago = time.time() - 12 * 3600
    os.utime(cache, (half_ago, half_ago))

    sched = catalog_refresh.CatalogRefreshScheduler(tmp_path, interval_hours=24)
    wait = sched._initial_wait()
    assert 10 * 3600 < wait < 14 * 3600


def test_scheduler_refreshes_immediately_when_cache_missing(tmp_path, monkeypatch):
    """start() with no cache refreshes right away (in the background thread)
    instead of sleeping a full interval first."""
    import threading
    import time

    fired = threading.Event()
    monkeypatch.setattr(
        catalog_refresh, "refresh_provider_models_cache",
        lambda data_dir: fired.set() or True,
    )

    sched = catalog_refresh.CatalogRefreshScheduler(tmp_path, interval_hours=99999)
    sched.start()
    try:
        assert fired.wait(timeout=5)
    finally:
        sched.stop()


def test_scheduler_waits_when_cache_fresh(tmp_path, monkeypatch):
    """start() with a fresh cache does NOT refresh immediately."""
    import time

    cache = tmp_path / "provider_models_cache.json"
    cache.write_text("{}", encoding="utf-8")

    calls = []
    monkeypatch.setattr(
        catalog_refresh, "refresh_provider_models_cache",
        lambda data_dir: calls.append(data_dir) or True,
    )

    sched = catalog_refresh.CatalogRefreshScheduler(tmp_path, interval_hours=99999)
    sched.start()
    try:
        time.sleep(0.15)
        assert calls == []
    finally:
        sched.stop()
