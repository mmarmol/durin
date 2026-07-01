"""The settings update handler applies a new default model/provider LIVE: it
invokes its ``on_default_changed`` callback after saving config so the running
loop re-resolves the default without a gateway restart (mirrors PersonasService's
``on_config_changed`` plumbing)."""

from __future__ import annotations

import pytest

from durin.service.principal import Principal
from durin.service.settings import (
    SettingsService,
    SettingsUpdateCommand,
)


@pytest.fixture()
def settings_env(tmp_path, monkeypatch):
    from durin.config.loader import save_config
    from durin.config.schema import Config

    config_path = tmp_path / "config.json"
    config = Config()
    config.agents.defaults.model = "openai/gpt-4o"
    config.providers.openai.api_key = "sk-test"
    save_config(config, config_path)

    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )
    return tmp_path, config_path


async def test_update_model_invokes_default_changed_hook(settings_env):
    called = {"n": 0}
    svc = SettingsService(on_default_changed=lambda: called.__setitem__("n", called["n"] + 1))
    await svc.update(SettingsUpdateCommand(model="anthropic/claude-3-5-sonnet"), Principal.local())
    assert called["n"] == 1


async def test_update_provider_invokes_default_changed_hook(settings_env):
    called = {"n": 0}
    svc = SettingsService(on_default_changed=lambda: called.__setitem__("n", called["n"] + 1))
    await svc.update(SettingsUpdateCommand(provider="openai"), Principal.local())
    assert called["n"] == 1


async def test_no_change_does_not_invoke_hook(settings_env):
    """Submitting the already-set model must not re-apply (no change)."""
    called = {"n": 0}
    svc = SettingsService(on_default_changed=lambda: called.__setitem__("n", called["n"] + 1))
    await svc.update(SettingsUpdateCommand(model="openai/gpt-4o"), Principal.local())
    assert called["n"] == 0


async def test_no_hook_is_noop(settings_env):
    """SettingsService without a hook works normally (backward compat)."""
    svc = SettingsService()
    result = await svc.update(SettingsUpdateCommand(model="anthropic/claude-3-5-sonnet"), Principal.local())
    assert result.agent["model"] == "anthropic/claude-3-5-sonnet"
