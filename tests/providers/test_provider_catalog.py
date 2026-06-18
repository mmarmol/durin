import json

import durin.providers.provider_catalog as pc


def test_provider_models_reads_index_with_caps(tmp_path, monkeypatch):
    idx = tmp_path / "provider_models.json"
    idx.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "providers": {
                    "zai_coding_plan": [
                        {
                            "id": "glm-5.2",
                            "max_input_tokens": 1_000_000,
                            "supports_vision": False,
                            "supports_reasoning": True,
                        },
                        {
                            "id": "glm-5v-turbo",
                            "max_input_tokens": 200_000,
                            "supports_vision": True,
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pc, "_INDEX_PATH", idx)
    pc._load_index.cache_clear()

    models = pc.provider_models("zai_coding_plan")
    assert [m.id for m in models] == ["glm-5.2", "glm-5v-turbo"]
    assert pc.catalog_model_caps("zai_coding_plan", "glm-5v-turbo").supports_vision is True
    assert pc.catalog_model_caps("zai_coding_plan", "glm-5.2").supports_reasoning is True
    assert pc.catalog_model_caps("zai_coding_plan", "nope") is None
    assert pc.provider_models("unconfigured-x") == []
    pc._load_index.cache_clear()


def test_missing_index_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_INDEX_PATH", tmp_path / "does-not-exist.json")
    pc._load_index.cache_clear()
    assert pc.provider_models("anything") == []
    pc._load_index.cache_clear()
