"""Tests for the model catalog builder."""

from __future__ import annotations

from durin.cli.tui.model_catalog import (
    ModelEntry,
    build_entries,
    format_entry,
    infer_provider,
)


def _make_config(providers_with_keys: tuple[str, ...] = ()):
    """Minimal config mock with provider configs that have/don't have keys."""
    from unittest.mock import MagicMock

    config = MagicMock()
    config.providers = MagicMock()
    for name in ("anthropic", "openai", "zhipu", "zai_coding_plan", "gemini", "deepseek"):
        pc = MagicMock()
        pc.api_key = "test-key" if name in providers_with_keys else None
        pc.api_base = None
        setattr(config.providers, name, pc)
    config.model_presets = {}
    return config


def test_infer_provider_by_keyword():
    assert infer_provider("claude-sonnet-4-6") == "anthropic"
    assert infer_provider("gpt-5") == "openai"
    assert infer_provider("glm-5.2") in ("zhipu", "zai_coding_plan")


def test_infer_provider_unknown():
    assert infer_provider("some-random-model") == "auto"


def test_build_entries_includes_configured_presets():
    from durin.config.schema import ModelPresetConfig

    config = _make_config()
    config.model_presets = {
        "fast": ModelPresetConfig(model="glm-5-turbo"),
    }
    entries = build_entries(
        config=config,
        presets={"default": config.model_presets["fast"], "fast": config.model_presets["fast"]},
        recent=[],
        active="default",
    )
    names = [e.name for e in entries]
    assert "default" in names
    assert "fast" in names


def test_build_entries_includes_suggested_for_configured_providers():
    config = _make_config(providers_with_keys=("anthropic",))
    entries = build_entries(
        config=config,
        presets={"default": _preset("glm-5.2")},
        recent=[],
        active="default",
    )
    suggested_names = [e.name for e in entries if not e.is_preset]
    assert "claude-opus-4-7" in suggested_names


def test_build_entries_excludes_suggested_for_unconfigured_providers():
    config = _make_config(providers_with_keys=())
    entries = build_entries(
        config=config,
        presets={"default": _preset("glm-5.2")},
        recent=[],
        active="default",
    )
    suggested_names = [e.name for e in entries if not e.is_preset]
    assert "claude-opus-4-7" not in suggested_names


def test_build_entries_includes_recent():
    config = _make_config()
    entries = build_entries(
        config=config,
        presets={"default": _preset("glm-5.2")},
        recent=["claude-sonnet-4-6"],
        active="default",
    )
    recent_entries = [e for e in entries if e.is_recent]
    assert any(e.name == "claude-sonnet-4-6" for e in recent_entries)


def test_format_entry_shows_context_window():
    from durin.providers.capabilities import ModelCapabilities

    entry = ModelEntry(
        name="glm-5.2",
        provider="zhipu",
        is_preset=False,
        is_recent=False,
        capabilities=ModelCapabilities(
            model="glm-5.2",
            provider="zhipu",
            max_input_tokens=1_000_000,
            supports_reasoning=True,
            supports_vision=False,
        ),
    )
    text = format_entry(entry)
    assert "glm-5.2" in text
    assert "1M" in text
    assert "reasoning" in text.lower()


def test_format_entry_none_capabilities():
    from durin.cli.tui.model_catalog import ModelEntry, format_entry

    entry = ModelEntry(
        name="unknown-model",
        provider="auto",
        is_preset=False,
        is_recent=False,
        capabilities=None,
    )
    text = format_entry(entry)
    assert text == "unknown-model"


def _preset(model: str):
    from durin.config.schema import ModelPresetConfig

    return ModelPresetConfig(model=model)
