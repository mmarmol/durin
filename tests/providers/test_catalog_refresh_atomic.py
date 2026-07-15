"""Single-process atomic+lock tests for provider_models_cache.json."""

from __future__ import annotations

import json
from pathlib import Path


def test_refresh_creates_lock_file(tmp_path: Path, monkeypatch) -> None:
    from durin.providers import catalog_refresh

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
    catalog_refresh.refresh_provider_models_cache(tmp_path)
    cache = tmp_path / "provider_models_cache.json"
    assert cache.exists()
    assert Path(f"{cache}.lock").exists()


def test_refresh_cache_is_valid_json(tmp_path: Path, monkeypatch) -> None:
    from durin.providers import catalog_refresh

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
    catalog_refresh.refresh_provider_models_cache(tmp_path)
    data = json.loads((tmp_path / "provider_models_cache.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert "zai_coding_plan" in data["providers"]
