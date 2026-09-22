"""Tests for the optional ``agents.aux_models.subagents`` model.

Mirrors the vision/audio/memory aux bridges: unset inherits the parent
session's provider/model, a preset or inline model+provider pins the
spawned subagent to a different model, and any resolution failure must
fall back to the inherited model rather than break spawning.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.agent.subagent import SubagentManager, _resolve_subagent_provider
from durin.bus.queue import MessageBus
from durin.config.loader import get_config_path, load_config
from durin.config.schema import AuxModelConfig, ModelEntry, ModelPresetConfig
from durin.providers.base import LLMProvider


def _config(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    return load_config(get_config_path())


# ---------------------------------------------------------------------------
# _resolve_subagent_provider
# ---------------------------------------------------------------------------


def test_resolve_returns_none_when_unset(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    assert cfg.agents.aux_models.subagents is None
    assert _resolve_subagent_provider(cfg) is None


def test_resolve_returns_none_for_none_config():
    assert _resolve_subagent_provider(None) is None


def test_resolve_via_preset(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    cfg.model_presets["cheap"] = ModelPresetConfig(model="cheap-model", provider="ollama")
    cfg.agents.aux_models.subagents = AuxModelConfig(preset="cheap")

    sentinel_provider = MagicMock(spec=LLMProvider)
    with patch("durin.providers.factory.make_provider", return_value=sentinel_provider) as m:
        result = _resolve_subagent_provider(cfg)

    assert result is not None
    provider, model, window = result
    assert provider is sentinel_provider
    assert model == "cheap-model"
    assert window == ModelPresetConfig(model="x").context_window_tokens
    # resolved through the preset, not an inline ModelPresetConfig
    _, kwargs = m.call_args
    assert kwargs["preset"].model == "cheap-model"
    assert kwargs["preset"].provider == "ollama"


def test_resolve_via_inline_model_and_provider(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    cfg.agents.aux_models.subagents = AuxModelConfig(model="inline-model", provider="ollama")

    sentinel_provider = MagicMock(spec=LLMProvider)
    with patch("durin.providers.factory.make_provider", return_value=sentinel_provider) as m:
        result = _resolve_subagent_provider(cfg)

    assert result[:2] == (sentinel_provider, "inline-model")
    _, kwargs = m.call_args
    assert kwargs["preset"].model == "inline-model"
    assert kwargs["preset"].provider == "ollama"


def test_resolve_via_preset_carries_the_presets_window(tmp_path, monkeypatch):
    """The child's context budget must follow the aux model, not the parent's:
    a 16K local model spawned from a 200K session would otherwise run with
    the parent's window and hit the provider's context error first."""
    cfg = _config(tmp_path, monkeypatch)
    cfg.model_presets["cheap"] = ModelPresetConfig(
        model="cheap-model", provider="ollama", context_window_tokens=16_000,
    )
    cfg.agents.aux_models.subagents = AuxModelConfig(preset="cheap")
    with patch("durin.providers.factory.make_provider", return_value=MagicMock(spec=LLMProvider)):
        _, _, window = _resolve_subagent_provider(cfg)
    assert window == 16_000


def test_resolve_via_inline_model_takes_the_users_model_entry_window(tmp_path, monkeypatch):
    """An inline ``model``/``provider`` pair resolves like the picker's
    ``provider model`` ref: the user's per-model entry wins over the schema
    default, so a configured 8K local model is not run as a 64K one."""
    cfg = _config(tmp_path, monkeypatch)
    cfg.providers.ollama.models["inline-model"] = ModelEntry(context_window_tokens=8_192)
    cfg.agents.aux_models.subagents = AuxModelConfig(model="inline-model", provider="ollama")
    with patch("durin.providers.factory.make_provider", return_value=MagicMock(spec=LLMProvider)):
        _, _, window = _resolve_subagent_provider(cfg)
    assert window == 8_192


def test_resolve_bad_preset_raises_to_caller(tmp_path, monkeypatch):
    """A bad preset name raises from resolve_preset(); the caller
    (SubagentManager._run_subagent) is responsible for catching this and
    falling back — verified separately below."""
    cfg = _config(tmp_path, monkeypatch)
    cfg.agents.aux_models.subagents = AuxModelConfig(preset="does-not-exist")
    with pytest.raises(KeyError):
        _resolve_subagent_provider(cfg)


# ---------------------------------------------------------------------------
# SubagentManager._run_subagent wiring
# ---------------------------------------------------------------------------


def _manager(tmp_path, provider, app_config_getter=None) -> SubagentManager:
    sm = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="parent-model",
        max_tool_result_chars=16_000,
        app_config_getter=app_config_getter,
    )
    sm.runner.run = AsyncMock(return_value=AgentRunResult(
        final_content="done", messages=[], stop_reason="completed",
    ))
    return sm


@pytest.mark.asyncio
async def test_spawn_uses_inherited_model_when_aux_unset(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    parent_provider = MagicMock(spec=LLMProvider)
    parent_provider.get_default_model.return_value = "parent-model"
    sm = _manager(tmp_path, parent_provider, app_config_getter=lambda: cfg)

    await sm.spawn("do something")
    import asyncio
    await asyncio.sleep(0.05)

    spec = sm.runner.run.await_args.args[0]
    assert spec.provider is parent_provider
    assert spec.model == "parent-model"


@pytest.mark.asyncio
async def test_spawn_uses_aux_model_via_preset(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    cfg.model_presets["cheap"] = ModelPresetConfig(model="cheap-model", provider="ollama")
    cfg.agents.aux_models.subagents = AuxModelConfig(preset="cheap")

    parent_provider = MagicMock(spec=LLMProvider)
    parent_provider.get_default_model.return_value = "parent-model"
    aux_provider = MagicMock(spec=LLMProvider)
    sm = _manager(tmp_path, parent_provider, app_config_getter=lambda: cfg)

    with patch("durin.providers.factory.make_provider", return_value=aux_provider):
        await sm.spawn("do something")
        import asyncio
        await asyncio.sleep(0.05)

    spec = sm.runner.run.await_args.args[0]
    assert spec.provider is aux_provider
    assert spec.model == "cheap-model"


@pytest.mark.asyncio
async def test_spawn_on_aux_model_runs_under_the_aux_models_window(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    cfg.model_presets["cheap"] = ModelPresetConfig(
        model="cheap-model", provider="ollama", context_window_tokens=16_000,
    )
    cfg.agents.aux_models.subagents = AuxModelConfig(preset="cheap")
    parent_provider = MagicMock(spec=LLMProvider)
    parent_provider.get_default_model.return_value = "parent-model"
    sm = _manager(tmp_path, parent_provider, app_config_getter=lambda: cfg)
    sm.context_window_tokens = 200_000
    with patch("durin.providers.factory.make_provider", return_value=MagicMock(spec=LLMProvider)):
        await sm.spawn("do something")
        import asyncio
        await asyncio.sleep(0.05)

    spec = sm.runner.run.await_args.args[0]
    assert spec.model == "cheap-model"
    assert spec.context_window_tokens == 16_000


@pytest.mark.asyncio
async def test_spawn_uses_aux_model_via_inline_fields(tmp_path, monkeypatch):
    cfg = _config(tmp_path, monkeypatch)
    cfg.agents.aux_models.subagents = AuxModelConfig(model="inline-model", provider="ollama")

    parent_provider = MagicMock(spec=LLMProvider)
    parent_provider.get_default_model.return_value = "parent-model"
    aux_provider = MagicMock(spec=LLMProvider)
    sm = _manager(tmp_path, parent_provider, app_config_getter=lambda: cfg)

    with patch("durin.providers.factory.make_provider", return_value=aux_provider):
        await sm.spawn("do something")
        import asyncio
        await asyncio.sleep(0.05)

    spec = sm.runner.run.await_args.args[0]
    assert spec.provider is aux_provider
    assert spec.model == "inline-model"


@pytest.mark.asyncio
async def test_spawn_falls_back_and_warns_on_resolution_failure(tmp_path, monkeypatch):
    """A misconfigured aux model (bad preset) must never break spawning —
    the subagent falls back to the inherited session model and a warning
    is logged."""
    cfg = _config(tmp_path, monkeypatch)
    cfg.agents.aux_models.subagents = AuxModelConfig(preset="does-not-exist")

    parent_provider = MagicMock(spec=LLMProvider)
    parent_provider.get_default_model.return_value = "parent-model"
    sm = _manager(tmp_path, parent_provider, app_config_getter=lambda: cfg)

    with patch("durin.agent.subagent.logger") as mock_logger:
        await sm.spawn("do something")
        import asyncio
        await asyncio.sleep(0.05)
        assert mock_logger.warning.called

    spec = sm.runner.run.await_args.args[0]
    assert spec.provider is parent_provider
    assert spec.model == "parent-model"


@pytest.mark.asyncio
async def test_spawn_without_app_config_getter_uses_inherited_model(tmp_path):
    """Callers that don't wire an app_config_getter (older constructions,
    tests) keep the pre-existing inherit-the-session-model behavior."""
    parent_provider = MagicMock(spec=LLMProvider)
    parent_provider.get_default_model.return_value = "parent-model"
    sm = _manager(tmp_path, parent_provider, app_config_getter=None)

    await sm.spawn("do something")
    import asyncio
    await asyncio.sleep(0.05)

    spec = sm.runner.run.await_args.args[0]
    assert spec.provider is parent_provider
    assert spec.model == "parent-model"
