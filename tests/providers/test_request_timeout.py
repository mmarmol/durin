"""Per-model request_timeout_s tests.

Verifies that a model configured with request_timeout_s uses that value
instead of the global default, and that omitting it falls back to the
env/default path.
"""

from __future__ import annotations

from durin.providers.openai_compat_provider import (
    OpenAICompatProvider,
    _openai_compat_timeout_s,
)

# ---------------------------------------------------------------------------
# Provider-level: __init__ accepts request_timeout_s
# ---------------------------------------------------------------------------


def test_provider_uses_explicit_timeout() -> None:
    """request_timeout_s=1800 should be stored as _timeout_s."""
    provider = OpenAICompatProvider(
        api_key="test-key",
        api_base="https://example.com/v1",
        request_timeout_s=1800.0,
    )
    assert provider._timeout_s == 1800.0
    assert provider._client_kwargs["timeout"] == 1800.0


def test_provider_falls_back_to_default_when_none() -> None:
    """request_timeout_s=None must fall back to _openai_compat_timeout_s()."""
    provider = OpenAICompatProvider(
        api_key="test-key",
        api_base="https://example.com/v1",
        request_timeout_s=None,
    )
    assert provider._timeout_s == _openai_compat_timeout_s()


def test_provider_omitting_timeout_behaves_like_none() -> None:
    """Omitting request_timeout_s entirely is the same as None."""
    provider = OpenAICompatProvider(
        api_key="test-key",
        api_base="https://example.com/v1",
    )
    assert provider._timeout_s == _openai_compat_timeout_s()


def test_provider_timeout_overrides_env(monkeypatch) -> None:
    """Per-model timeout wins over the env override."""
    monkeypatch.setenv("DURIN_OPENAI_COMPAT_TIMEOUT_S", "45")
    provider = OpenAICompatProvider(
        api_key="test-key",
        api_base="https://example.com/v1",
        request_timeout_s=900.0,
    )
    assert provider._timeout_s == 900.0


# ---------------------------------------------------------------------------
# Schema level: ModelEntry + ModelPresetConfig carry request_timeout_s
# ---------------------------------------------------------------------------


def test_model_entry_accepts_request_timeout_s() -> None:
    from durin.config.schema import ModelEntry

    entry = ModelEntry(request_timeout_s=1800.0)
    assert entry.request_timeout_s == 1800.0


def test_model_entry_default_is_none() -> None:
    from durin.config.schema import ModelEntry

    entry = ModelEntry()
    assert entry.request_timeout_s is None


def test_model_preset_config_accepts_request_timeout_s() -> None:
    from durin.config.schema import ModelPresetConfig

    preset = ModelPresetConfig(model="my-model", request_timeout_s=600.0)
    assert preset.request_timeout_s == 600.0


def test_model_preset_config_default_is_none() -> None:
    from durin.config.schema import ModelPresetConfig

    preset = ModelPresetConfig(model="my-model")
    assert preset.request_timeout_s is None


# ---------------------------------------------------------------------------
# Factory level: request_timeout_s threads from ModelEntry → provider
# ---------------------------------------------------------------------------


def _make_config_with_timeout(provider_name: str, model_name: str, timeout_s: float):
    """Build a minimal Config with one provider + one model that has a timeout."""
    from durin.config.schema import (
        AgentDefaults,
        AgentsConfig,
        Config,
        ModelEntry,
        ProviderConfig,
        ProvidersConfig,
    )

    provider_cfg = ProviderConfig(
        api_key="test-key",
        api_base="https://example.com/v1",
        models={model_name: ModelEntry(request_timeout_s=timeout_s)},
    )
    providers = ProvidersConfig(**{provider_name: provider_cfg})
    agents = AgentsConfig(
        defaults=AgentDefaults(
            provider=provider_name,
            model=model_name,
        )
    )
    return Config(providers=providers, agents=agents)


def test_factory_threads_timeout_to_provider() -> None:
    """ModelEntry.request_timeout_s resolves through preset to OpenAICompatProvider."""
    from durin.providers.factory import _make_provider_core

    config = _make_config_with_timeout("openai", "gpt-4o", 900.0)
    provider = _make_provider_core(config)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._timeout_s == 900.0


def test_factory_no_timeout_falls_back_to_default() -> None:
    """When ModelEntry has no request_timeout_s, the global default is used."""
    from durin.config.schema import (
        AgentDefaults,
        AgentsConfig,
        Config,
        ModelEntry,
        ProviderConfig,
        ProvidersConfig,
    )
    from durin.providers.factory import _make_provider_core

    provider_cfg = ProviderConfig(
        api_key="test-key",
        api_base="https://example.com/v1",
        models={"gpt-4o": ModelEntry()},
    )
    providers = ProvidersConfig(openai=provider_cfg)
    agents = AgentsConfig(defaults=AgentDefaults(provider="openai", model="gpt-4o"))
    config = Config(providers=providers, agents=agents)

    provider = _make_provider_core(config)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._timeout_s == _openai_compat_timeout_s()
