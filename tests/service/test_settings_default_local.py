"""A configured LOCAL provider (api_base set, no api_key) can be set as the
default via the settings update handler — it must not be rejected as
"provider is not configured" (that check used to validate by api_key only)."""

from __future__ import annotations

import pytest

from durin.service.principal import Principal
from durin.service.settings import (
    SettingsService,
    SettingsUpdateCommand,
)
from durin.service.types import ValidationFailedError


@pytest.fixture()
def local_default_env(tmp_path, monkeypatch):
    """Config wired to tmp_path with ollama api_base set (no api_key)."""
    from durin.config.loader import save_config
    from durin.config.schema import Config

    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "openai/gpt-4o"
    config.providers.ollama.api_base = "http://localhost:11434/v1"
    save_config(config, config_path)

    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )
    return tmp_path, config_path


async def test_local_provider_can_be_default(local_default_env):
    svc = SettingsService()
    result = await svc.update(
        SettingsUpdateCommand(provider="ollama"), Principal.local()
    )
    assert result.agent["provider"] == "ollama"


async def test_unconfigured_local_provider_rejected(tmp_path, monkeypatch):
    """A local provider with NO api_base must still be rejected."""
    from durin.config.loader import save_config
    from durin.config.schema import Config

    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "openai/gpt-4o"
    save_config(config, config_path)

    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )

    svc = SettingsService()
    with pytest.raises(ValidationFailedError):
        await svc.update(
            SettingsUpdateCommand(provider="ollama"), Principal.local()
        )
