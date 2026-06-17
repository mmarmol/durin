"""SP1: ConfigService — unit tests (no HTTP, no gateway)."""

from __future__ import annotations

import pytest

from durin.service.config import (
    ChannelsListQuery,
    ConfigGetQuery,
    ConfigService,
    ConfigSetCommand,
    CrossEncoderTestQuery,
    ModelCapabilitiesQuery,
    ModelsListQuery,
    ModelTestQuery,
)
from durin.service.principal import Principal, Scope
from durin.service.types import ForbiddenError, ValidationFailedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

LOCAL = Principal.local()


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    """Point config loader at a fresh tmp config."""
    from durin.config.loader import save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)
    return path


# ---------------------------------------------------------------------------
# config get
# ---------------------------------------------------------------------------


async def test_config_get_returns_config_and_schema(config_path):
    result = await ConfigService().get(ConfigGetQuery(), LOCAL)
    # Wire dump uses alias "schema" for the json_schema field
    wire = result.model_dump(by_alias=True)
    assert "config" in wire
    assert "schema" in wire
    assert isinstance(result.config, dict)
    assert isinstance(result.json_schema, dict)
    # "agents" key is present in the effective config
    assert "agents" in result.config


async def test_config_get_requires_read_scope():
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await ConfigService().get(ConfigGetQuery(), principal)


# ---------------------------------------------------------------------------
# config set
# ---------------------------------------------------------------------------


async def test_config_set_persists_value(config_path):
    from durin.config.loader import load_config

    result = await ConfigService().set(
        ConfigSetCommand(key="agents.defaults.temperature", value="0.42"),
        LOCAL,
    )
    assert result.ok is True
    assert load_config(config_path).agents.defaults.temperature == pytest.approx(0.42)


async def test_config_set_rejects_invalid_value(config_path):
    from durin.config.loader import load_config

    with pytest.raises(ValidationFailedError, match="validation failed"):
        await ConfigService().set(
            ConfigSetCommand(key="agents.defaults.maxTokens", value='"nope"'),
            LOCAL,
        )
    # config unchanged
    assert load_config(config_path).agents.defaults.max_tokens == 8192


async def test_config_set_requires_write_scope():
    principal = Principal.remote("t", frozenset({Scope.CONFIG_READ.value}))
    with pytest.raises(ForbiddenError):
        await ConfigService().set(
            ConfigSetCommand(key="agents.defaults.temperature", value="0.1"),
            principal,
        )


# ---------------------------------------------------------------------------
# models list
# ---------------------------------------------------------------------------


async def test_models_list_returns_suggested_and_catalog(config_path):
    result = await ConfigService().models_list(
        ModelsListQuery(provider="zhipu"), LOCAL
    )
    assert isinstance(result.suggested, list)
    assert isinstance(result.models, list)
    assert any("glm" in m for m in result.suggested)


async def test_models_list_empty_provider_no_filter(config_path):
    result = await ConfigService().models_list(ModelsListQuery(), LOCAL)
    assert isinstance(result.models, list)


async def test_models_list_empty_provider_suggests_configured_provider_models(
    tmp_path, monkeypatch
):
    """The composer model popover opens with no provider filter. ``suggested``
    must not be empty when a provider is configured, so the picker isn't blank
    until the user types. Regression: opening it showed 'no models available'.
    """
    from durin.config.loader import save_config
    from durin.config.schema import Config

    config = Config()
    config.providers.zhipu.api_key = "sk-test-zhipu"
    path = tmp_path / "config.json"
    save_config(config, path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)

    result = await ConfigService().models_list(ModelsListQuery(), LOCAL)

    assert result.suggested, "expected suggestions for a configured provider"
    assert any("glm" in m for m in result.suggested)


async def test_models_list_empty_provider_no_config_stays_empty(config_path):
    """With nothing configured, suggested is empty (catalog still browsable)."""
    result = await ConfigService().models_list(ModelsListQuery(), LOCAL)
    assert result.suggested == []


async def test_models_list_provider_uses_catalog_with_capability(config_path, monkeypatch):
    """A provider filter returns the per-provider catalog, capability-filtered."""
    import durin.providers.provider_catalog as pc
    from durin.providers.provider_catalog import ModelInfo

    monkeypatch.setattr(
        pc, "_load_index",
        lambda: {"zai_coding_plan": [
            ModelInfo(id="glm-5.2", supports_vision=False),
            ModelInfo(id="glm-5v-turbo", supports_vision=True),
        ]},
    )
    vision = await ConfigService().models_list(
        ModelsListQuery(provider="zai_coding_plan", capability="vision"), LOCAL
    )
    assert vision.models == ["glm-5v-turbo"]
    all_models = await ConfigService().models_list(
        ModelsListQuery(provider="zai_coding_plan"), LOCAL
    )
    assert all_models.models == ["glm-5.2", "glm-5v-turbo"]


async def test_model_picker_returns_easy_pick_and_catalog(tmp_path, monkeypatch):
    from durin.config.loader import save_config
    from durin.config.schema import Config
    from durin.service.config import ModelPickerQuery

    config = Config()
    config.agents.defaults.model = "base-model"
    config.providers.gemini.api_key = "k"
    path = tmp_path / "config.json"
    save_config(config, path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)
    import durin.providers.codex_device_auth as _cda

    monkeypatch.setattr("durin.utils.oauth.any_token_present", lambda _n: False)
    # Keep codex undetected so the picker build stays network-free in tests
    # (codex detection consults the secret store, then would list models live).
    monkeypatch.setattr(_cda, "codex_token_present", lambda: False)

    result = await ConfigService().model_picker(ModelPickerQuery(recent=""), LOCAL)
    groups = {e.group for e in result.entries}
    assert "Easy pick" in groups
    assert "gemini" in groups
    assert all(e.provider for e in result.entries)
    # Catalog entries carry capabilities from provider_models.json.
    gemini_catalog = [
        e for e in result.entries if e.provider == "gemini" and e.role == "catalog"
    ]
    assert gemini_catalog, "expected gemini catalog entries"
    assert any(e.max_input_tokens for e in gemini_catalog), "expected caps from the catalog"


async def test_model_picker_requires_read_scope():
    from durin.service.config import ModelPickerQuery

    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await ConfigService().model_picker(ModelPickerQuery(recent=""), principal)


async def test_models_list_requires_read_scope():
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await ConfigService().models_list(ModelsListQuery(), principal)


async def test_provider_models_route_lists_caps_and_overrides(config_path, monkeypatch):
    import durin.providers.provider_catalog as pc
    from durin.providers.provider_catalog import ModelInfo
    from durin.service.config import ProviderModelsQuery

    monkeypatch.setattr(
        pc, "_load_index",
        lambda: {"zai_coding_plan": [
            ModelInfo(id="glm-5.2", max_input_tokens=1_000_000, supports_reasoning=True),
            ModelInfo(id="glm-5v-turbo", supports_vision=True),
        ]},
    )
    res = await ConfigService().provider_models_route(
        ProviderModelsQuery(provider="zai_coding_plan"), LOCAL
    )
    assert {m.id for m in res.models} == {"glm-5.2", "glm-5v-turbo"}
    glm = next(m for m in res.models if m.id == "glm-5.2")
    assert glm.supports_reasoning is True
    assert glm.max_input_tokens == 1_000_000
    assert glm.configured is False


async def test_provider_model_upsert_and_remove(config_path):
    from durin.config.loader import load_config
    from durin.service.config import (
        ProviderModelDeleteCommand,
        ProviderModelUpsertCommand,
    )

    await ConfigService().provider_model_upsert(
        ProviderModelUpsertCommand(
            provider="zai_coding_plan", model="glm-5.2", context_window_tokens=1_000_000
        ),
        LOCAL,
    )
    cfg = load_config(config_path)
    assert cfg.providers.zai_coding_plan.models["glm-5.2"].context_window_tokens == 1_000_000

    await ConfigService().provider_model_remove(
        ProviderModelDeleteCommand(provider="zai_coding_plan", model="glm-5.2"), LOCAL
    )
    cfg2 = load_config(config_path)
    assert "glm-5.2" not in (cfg2.providers.zai_coding_plan.models or {})


async def test_provider_model_upsert_requires_write_scope():
    from durin.service.config import ProviderModelUpsertCommand

    principal = Principal.remote("t", frozenset({Scope.CONFIG_READ.value}))
    with pytest.raises(ForbiddenError):
        await ConfigService().provider_model_upsert(
            ProviderModelUpsertCommand(provider="zai_coding_plan", model="x"), principal
        )


# ---------------------------------------------------------------------------
# model capabilities
# ---------------------------------------------------------------------------


async def test_model_capabilities_returns_fields(config_path):
    result = await ConfigService().model_capabilities(
        ModelCapabilitiesQuery(model="glm-5.1", provider="zhipu"), LOCAL
    )
    assert result.model == "glm-5.1"
    assert isinstance(result.supports_vision, bool)
    assert isinstance(result.supports_function_calling, bool)


async def test_model_capabilities_requires_read_scope():
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await ConfigService().model_capabilities(
            ModelCapabilitiesQuery(model="gpt-4o"), principal
        )


# ---------------------------------------------------------------------------
# channels list
# ---------------------------------------------------------------------------


async def test_channels_list_returns_channels(config_path):
    result = await ConfigService().channels_list(ChannelsListQuery(), LOCAL)
    assert isinstance(result.channels, list)
    names = {c["name"] for c in result.channels}
    assert "telegram" in names
    tg = next(c for c in result.channels if c["name"] == "telegram")
    assert tg["enabled"] is False
    assert "display_name" in tg
    assert "credential_field" in tg


async def test_channels_list_requires_read_scope():
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await ConfigService().channels_list(ChannelsListQuery(), principal)


# ---------------------------------------------------------------------------
# model test (async probe — external call mocked)
# ---------------------------------------------------------------------------


async def test_model_test_returns_status(config_path, monkeypatch):
    import types

    fake_result = types.SimpleNamespace(
        status="ok",
        message="ping succeeded",
        fix=None,
    )

    async def _fake_ping(cfg):  # noqa: ANN001
        return fake_result

    monkeypatch.setattr(
        "durin.service.config.ConfigService.model_test.__wrapped__"
        if hasattr(ConfigService.model_test, "__wrapped__")
        else "durin.cli.doctor.check_model_ping_async",
        _fake_ping,
        raising=False,
    )
    # Direct monkeypatch of the imported name inside the service
    import durin.cli.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "check_model_ping_async", _fake_ping)

    result = await ConfigService().model_test(
        ModelTestQuery(model="gpt-4o", provider="openai"), LOCAL
    )
    assert result.status == "ok"
    assert result.message == "ping succeeded"
    assert result.fix == ""


async def test_model_test_requires_read_scope():
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await ConfigService().model_test(ModelTestQuery(), principal)


# ---------------------------------------------------------------------------
# cross-encoder test (async probe — off-thread call mocked)
# ---------------------------------------------------------------------------


async def test_cross_encoder_test_returns_result(monkeypatch):
    fake_probe_result = {
        "status": "ok",
        "message": "model loaded",
        "model_id": "cross-encoder/ms-marco-MiniLM-L-6-v2",
        "duration_ms": 42.0,
    }

    def _fake_probe(model_id: str) -> dict:  # noqa: ANN001
        return fake_probe_result

    monkeypatch.setattr("durin.memory.cross_encoder.probe_model", _fake_probe)

    result = await ConfigService().cross_encoder_test(
        CrossEncoderTestQuery(
            model="cross-encoder/ms-marco-MiniLM-L-6-v2"
        ),
        LOCAL,
    )
    assert result.status == "ok"
    assert result.model_id == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert result.duration_ms == pytest.approx(42.0)


async def test_cross_encoder_test_handles_exception(monkeypatch):
    def _bad_probe(model_id: str) -> dict:  # noqa: ANN001
        raise RuntimeError("model not found")

    monkeypatch.setattr("durin.memory.cross_encoder.probe_model", _bad_probe)

    result = await ConfigService().cross_encoder_test(
        CrossEncoderTestQuery(model="bad-model"),
        LOCAL,
    )
    assert result.status == "fail"
    assert "RuntimeError" in result.message
    assert result.duration_ms == 0.0


async def test_cross_encoder_test_requires_read_scope():
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await ConfigService().cross_encoder_test(
            CrossEncoderTestQuery(model="any"), principal
        )
