"""SP1: SettingsService — unit tests (no HTTP, no channel)."""

from __future__ import annotations

import pytest

from durin.service.principal import Principal, Scope
from durin.service.settings import (
    SettingsProviderUpdateCommand,
    SettingsQuery,
    SettingsService,
    SettingsUpdateCommand,
    SettingsWebSearchUpdateCommand,
)
from durin.service.types import ForbiddenError, ValidationFailedError


@pytest.fixture()
def config_env(tmp_path, monkeypatch):
    """Minimal config + secret store wired to tmp_path."""
    from durin.config.loader import save_config
    from durin.config.schema import Config

    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "openai/gpt-4o"
    config.providers.openai.api_key = "plain-openai-key"
    config.tools.web.search.provider = "duckduckgo"
    save_config(config, config_path)

    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )
    return tmp_path, config_path


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_returns_full_payload(config_env):
    svc = SettingsService()
    result = await svc.get(SettingsQuery(), Principal.local())
    assert result.agent["model"] == "openai/gpt-4o"
    assert result.agent["has_api_key"] is True
    assert isinstance(result.providers, list)
    provider_map = {p["name"]: p for p in result.providers}
    assert "openai" in provider_map
    assert provider_map["openai"]["configured"] is True
    # Secret value never returned, only hint
    assert "plain-openai-key" not in str(result.model_dump())
    assert result.web_search["provider"] == "duckduckgo"
    assert result.requires_restart is False


async def test_get_requires_read_scope(config_env):
    principal = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await SettingsService().get(SettingsQuery(), principal)


# ---------------------------------------------------------------------------
# update — model / provider
# ---------------------------------------------------------------------------


async def test_update_model(config_env):
    _, config_path = config_env
    svc = SettingsService()
    result = await svc.update(
        SettingsUpdateCommand(model="openrouter/gpt-4o-mini"),
        Principal.local(),
    )
    assert result.agent["model"] == "openrouter/gpt-4o-mini"
    # Persisted
    from durin.config.loader import load_config
    saved = load_config(config_path)
    assert saved.agents.defaults.model == "openrouter/gpt-4o-mini"


async def test_update_empty_model_raises(config_env):
    with pytest.raises(ValidationFailedError, match="model is required"):
        await SettingsService().update(
            SettingsUpdateCommand(model="   "), Principal.local()
        )


async def test_update_unknown_provider_raises(config_env):
    with pytest.raises(ValidationFailedError, match="unknown provider"):
        await SettingsService().update(
            SettingsUpdateCommand(provider="no_such_provider"), Principal.local()
        )


async def test_update_unconfigured_provider_raises(config_env, monkeypatch):
    # openrouter has no api_key set in the fixture config
    with pytest.raises(ValidationFailedError, match="not configured"):
        await SettingsService().update(
            SettingsUpdateCommand(provider="openrouter"), Principal.local()
        )


async def test_update_requires_write_scope(config_env):
    principal = Principal.remote("t", frozenset({Scope.SETTINGS_READ.value}))
    with pytest.raises(ForbiddenError):
        await SettingsService().update(
            SettingsUpdateCommand(model="openai/gpt-4o"), principal
        )


# ---------------------------------------------------------------------------
# provider_update — stores secret ref, never plaintext
# ---------------------------------------------------------------------------


async def test_provider_update_stores_secret_ref(config_env):
    _, config_path = config_env
    svc = SettingsService()
    result = await svc.provider_update(
        SettingsProviderUpdateCommand(provider="openrouter", api_key="sk-or-secret"),
        Principal.local(),
    )
    # Provider should now appear configured
    provider_map = {p["name"]: p for p in result.providers}
    assert provider_map["openrouter"]["configured"] is True
    # Plaintext MUST NOT appear in the payload
    assert "sk-or-secret" not in str(result.model_dump())
    # Config on disk must hold a secret ref, not the plaintext
    from durin.config.loader import load_config
    from durin.security.secrets import SecretStore, is_secret_ref

    saved = load_config(config_path)
    assert is_secret_ref(saved.providers.openrouter.api_key)
    store = SecretStore(path=config_path.parent / "secrets.json").load()
    entry = store.get("OPENROUTER_API_KEY")
    assert entry is not None
    assert entry.value == "sk-or-secret"


async def test_provider_update_api_base(config_env):
    _, config_path = config_env
    await SettingsService().provider_update(
        SettingsProviderUpdateCommand(
            provider="openrouter", api_base="https://openrouter.ai/api/v1"
        ),
        Principal.local(),
    )
    from durin.config.loader import load_config
    saved = load_config(config_path)
    assert saved.providers.openrouter.api_base == "https://openrouter.ai/api/v1"


async def test_provider_update_unknown_provider_raises(config_env):
    with pytest.raises(ValidationFailedError, match="unknown provider"):
        await SettingsService().provider_update(
            SettingsProviderUpdateCommand(provider="no_such"),
            Principal.local(),
        )


async def test_provider_update_empty_provider_raises(config_env):
    with pytest.raises(ValidationFailedError, match="provider is required"):
        await SettingsService().provider_update(
            SettingsProviderUpdateCommand(provider="   "),
            Principal.local(),
        )


async def test_provider_update_requires_write_scope(config_env):
    principal = Principal.remote("t", frozenset({Scope.SETTINGS_READ.value}))
    with pytest.raises(ForbiddenError):
        await SettingsService().provider_update(
            SettingsProviderUpdateCommand(provider="openrouter"), principal
        )


# ---------------------------------------------------------------------------
# web_search_update
# ---------------------------------------------------------------------------


async def test_web_search_update_searxng(config_env):
    _, config_path = config_env
    result = await SettingsService().web_search_update(
        SettingsWebSearchUpdateCommand(
            provider="searxng", base_url="https://search.example.com"
        ),
        Principal.local(),
    )
    assert result.web_search["provider"] == "searxng"
    assert result.web_search["base_url"] == "https://search.example.com"
    assert result.web_search["api_key_hint"] is None

    from durin.config.loader import load_config
    saved = load_config(config_path)
    assert saved.tools.web.search.provider == "searxng"
    assert saved.tools.web.search.base_url == "https://search.example.com"
    assert saved.tools.web.search.api_key == ""


async def test_web_search_update_duckduckgo_clears_creds(config_env):
    _, config_path = config_env
    # Seed some prior credentials
    from durin.config.loader import load_config, save_config
    config = load_config(config_path)
    config.tools.web.search.provider = "brave"
    config.tools.web.search.api_key = "bravekey"
    save_config(config, config_path)

    result = await SettingsService().web_search_update(
        SettingsWebSearchUpdateCommand(provider="duckduckgo"),
        Principal.local(),
    )
    assert result.web_search["provider"] == "duckduckgo"
    assert result.web_search["api_key_hint"] is None


async def test_web_search_update_stores_secret_ref(config_env):
    _, config_path = config_env
    result = await SettingsService().web_search_update(
        SettingsWebSearchUpdateCommand(provider="brave", api_key="brave-secret"),
        Principal.local(),
    )
    assert result.web_search["provider"] == "brave"
    # Plaintext MUST NOT appear in the payload
    assert "brave-secret" not in str(result.model_dump())
    # Config on disk must hold a secret ref, not the plaintext
    from durin.config.loader import load_config
    from durin.security.secrets import SecretStore, is_secret_ref

    saved = load_config(config_path)
    assert is_secret_ref(saved.tools.web.search.api_key)
    store = SecretStore(path=config_path.parent / "secrets.json").load()
    entry = store.get("BRAVE_API_KEY")
    assert entry is not None
    assert entry.value == "brave-secret"


async def test_web_search_update_preserves_existing_secret_ref(config_env):
    """Re-saving the same provider without a new key keeps the stored ref
    intact and does not double-wrap it into a nested secret."""
    _, config_path = config_env
    svc = SettingsService()
    await svc.web_search_update(
        SettingsWebSearchUpdateCommand(provider="brave", api_key="brave-secret"),
        Principal.local(),
    )
    from durin.config.loader import load_config

    ref = load_config(config_path).tools.web.search.api_key
    # Update only base_url-irrelevant field by re-submitting brave w/o api_key
    await svc.web_search_update(
        SettingsWebSearchUpdateCommand(provider="brave"),
        Principal.local(),
    )
    saved = load_config(config_path)
    assert saved.tools.web.search.api_key == ref


async def test_web_search_update_unknown_provider_raises(config_env):
    with pytest.raises(ValidationFailedError, match="unknown web search provider"):
        await SettingsService().web_search_update(
            SettingsWebSearchUpdateCommand(provider="no_such"),
            Principal.local(),
        )


async def test_web_search_update_searxng_missing_base_url_raises(config_env):
    with pytest.raises(ValidationFailedError, match="base_url is required"):
        await SettingsService().web_search_update(
            SettingsWebSearchUpdateCommand(provider="searxng"),
            Principal.local(),
        )


async def test_web_search_update_brave_missing_api_key_raises(config_env):
    with pytest.raises(ValidationFailedError, match="api_key is required"):
        await SettingsService().web_search_update(
            SettingsWebSearchUpdateCommand(provider="brave"),
            Principal.local(),
        )


async def test_web_search_update_requires_write_scope(config_env):
    principal = Principal.remote("t", frozenset({Scope.SETTINGS_READ.value}))
    with pytest.raises(ForbiddenError):
        await SettingsService().web_search_update(
            SettingsWebSearchUpdateCommand(provider="duckduckgo"), principal
        )
