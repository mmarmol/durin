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


def test_codex_models_inherit_openai_caps(monkeypatch):
    """openai_codex serves the same OpenAI models, so each codex slug must
    inherit the matching ``openai`` catalog caps — a slug with no openai
    match falls back to a bare id (not dropped)."""
    import durin.providers.codex_models as cm

    monkeypatch.setattr(
        pc, "_load_index",
        lambda: {"openai": [
            pc.ModelInfo(
                id="gpt-5.5", max_input_tokens=1_050_000,
                max_output_tokens=128_000, supports_reasoning=True,
            ),
        ]},
    )
    monkeypatch.setattr(cm, "list_codex_models", lambda token=None: ["gpt-5.5", "codex-only-slug"])

    by_id = {m.id: m for m in pc.provider_models("openai_codex")}
    assert by_id["gpt-5.5"].max_input_tokens == 1_050_000
    assert by_id["gpt-5.5"].max_output_tokens == 128_000
    assert by_id["gpt-5.5"].supports_reasoning is True
    # unknown codex slug survives with no caps (not dropped)
    assert by_id["codex-only-slug"].max_input_tokens is None
    # the user's invariant: codex caps == openai caps for shared ids
    assert pc.catalog_model_caps("openai_codex", "gpt-5.5") == pc.catalog_model_caps("openai", "gpt-5.5")
