"""Tests that local providers (ollama/lm_studio/vllm) appear in the settings
provider list and report configured=True when api_base is set."""

from __future__ import annotations

import pytest

from durin.service.principal import Principal
from durin.service.settings import SettingsQuery, SettingsService


@pytest.fixture()
def local_provider_env(tmp_path, monkeypatch):
    """Config wired to tmp_path with ollama api_base set."""
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


async def test_local_provider_listed_and_configured(local_provider_env):
    svc = SettingsService()
    result = await svc.get(SettingsQuery(), Principal.local())
    names = {p["name"]: p for p in result.providers}
    assert "ollama" in names, f"ollama missing from providers: {list(names)}"
    assert names["ollama"]["configured"] is True
    assert names["ollama"]["is_local"] is True


async def test_local_provider_unconfigured_when_no_api_base(tmp_path, monkeypatch):
    """Without api_base set, ollama should appear but configured=False."""
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
    result = await svc.get(SettingsQuery(), Principal.local())
    names = {p["name"]: p for p in result.providers}
    assert "ollama" in names, f"ollama missing from providers: {list(names)}"
    assert names["ollama"]["configured"] is False
    assert names["ollama"]["is_local"] is True
