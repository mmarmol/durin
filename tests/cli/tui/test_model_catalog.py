"""Tests for the model catalog builder."""

from __future__ import annotations

from durin.cli.tui.model_catalog import (
    ModelEntry,
    build_entries,
    format_entry,
    infer_provider,
)


def _cfg(monkeypatch, *, model="base-model", **keys):
    """Real Config with given providers' api_key set; no OAuth tokens present."""
    from durin.config.schema import Config

    monkeypatch.setattr("durin.utils.oauth.any_token_present", lambda _n: False)
    config = Config()
    config.agents.defaults.model = model
    for name, val in keys.items():
        getattr(config.providers, name).api_key = val
    return config


def test_infer_provider_by_keyword():
    assert infer_provider("claude-sonnet-4-6") == "anthropic"
    assert infer_provider("gpt-5") == "openai"
    assert infer_provider("glm-5.2") in ("zhipu", "zai_coding_plan")


def test_infer_provider_unknown():
    assert infer_provider("some-random-model") == "auto"


def test_build_entries_pins_default(monkeypatch):
    cfg = _cfg(monkeypatch, gemini="k")
    entries = build_entries(config=cfg, presets={}, recent=[], active=None)
    assert entries[0].name == "base-model"  # default pinned first
    assert all(e.provider for e in entries)


def test_build_entries_preset_row_switches_by_name(monkeypatch):
    from durin.config.schema import ModelPresetConfig

    cfg = _cfg(monkeypatch, gemini="k")
    entries = build_entries(
        config=cfg,
        presets={"fast": ModelPresetConfig(model="gemini-2.5-flash", provider="gemini")},
        recent=[],
        active=None,
    )
    preset_entries = [e for e in entries if e.is_preset]
    assert any(e.name == "gemini-2.5-flash" and e.ref == "fast" for e in preset_entries)


def test_build_entries_includes_catalog_for_configured_providers(monkeypatch):
    cfg = _cfg(monkeypatch, anthropic="k")
    entries = build_entries(config=cfg, presets={}, recent=[], active=None)
    assert any(e.name == "claude-opus-4-7" for e in entries)


def test_build_entries_excludes_catalog_for_unconfigured_providers(monkeypatch):
    cfg = _cfg(monkeypatch)  # nothing configured
    entries = build_entries(config=cfg, presets={}, recent=[], active=None)
    assert not any(e.name == "claude-opus-4-7" for e in entries)


def test_build_entries_includes_recent(monkeypatch):
    cfg = _cfg(monkeypatch, gemini="k")
    entries = build_entries(
        config=cfg, presets={}, recent=["gemini-2.5-flash"], active=None,
    )
    assert any(e.name == "gemini-2.5-flash" and e.is_recent for e in entries)


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
