"""Build the list of models shown in the picker.

Three sources merged into one list of :class:`ModelEntry`:
1. **Configured presets** — from ``loop.model_presets`` dict.
2. **Curated suggestions** — ``DEFAULT_MODELS`` entries for providers
   whose API key is configured.
3. **Recent models** — persisted via :mod:`durin.cli.tui.state`.

Each entry is formatted with capability metadata (context window,
reasoning, vision) from the vendored catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from durin.config.schema import ModelPresetConfig
from durin.providers.capabilities import ModelCapabilities, get_model_capabilities
from durin.providers.registry import PROVIDERS


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One row in the model picker."""

    name: str
    provider: str
    is_preset: bool
    is_recent: bool
    capabilities: ModelCapabilities = field(default_factory=lambda: ModelCapabilities(model=""))


def infer_provider(model: str) -> str:
    """Infer the provider name for *model* via keyword matching.

    Returns the first matching provider's config name, or ``"auto"``
    if no keywords match.
    """
    model_lower = model.lower()
    model_normalized = model_lower.replace("-", "_")
    for spec in PROVIDERS:
        for kw in spec.keywords:
            if kw in model_lower or kw.replace("-", "_") in model_normalized:
                return spec.name
    return "auto"


def _provider_has_key(config, provider_name: str) -> bool:
    """Check if a provider has an API key configured."""
    pc = getattr(config.providers, provider_name, None)
    if pc is None:
        return False
    return bool(pc.api_key)


def _get_caps(model: str, provider: str | None = None) -> ModelCapabilities:
    """Safe wrapper around get_model_capabilities."""
    try:
        return get_model_capabilities(model, provider)
    except Exception:  # noqa: BLE001
        return ModelCapabilities(model=model, provider=provider)


def _fmt_ctx(tokens: int | None) -> str:
    if not tokens:
        return "?"
    if tokens >= 1_000_000:
        return f"{tokens // 1_000_000}M"
    if tokens >= 1_000:
        return f"{tokens // 1_000}K"
    return str(tokens)


def format_entry(entry: ModelEntry) -> str:
    """Format an entry for display in the OptionList.

    Layout: ``model_name<padding>meta``
    Meta: ``1000K ctx · reasoning ✓ · vision ✗``
    """
    caps = entry.capabilities
    parts: list[str] = []
    if caps.max_input_tokens:
        parts.append(f"{_fmt_ctx(caps.max_input_tokens)} ctx")
    if caps.supports_reasoning:
        parts.append("reasoning ✓")
    if caps.supports_vision:
        parts.append("vision ✓")
    meta = " · ".join(parts) if parts else ""
    name_padded = entry.name.ljust(30)
    if meta:
        return f"{name_padded}{meta}"
    return entry.name


def build_entries(
    *,
    config,
    presets: dict[str, ModelPresetConfig],
    recent: list[str],
    active: str,
) -> list[ModelEntry]:
    """Build the full list of model entries for the picker.

    Order: recent first, then configured presets, then suggestions.
    Duplicates are removed (a model in both presets and suggestions
    only appears in presets).
    """
    from durin.cli.onboard_wizard import DEFAULT_MODELS

    entries: list[ModelEntry] = []
    seen_names: set[str] = set()

    # --- Recent ---
    for model in recent:
        if model in seen_names:
            continue
        seen_names.add(model)
        provider = infer_provider(model)
        entries.append(
            ModelEntry(
                name=model,
                provider=provider,
                is_preset=False,
                is_recent=True,
                capabilities=_get_caps(model, provider),
            )
        )

    # --- Configured presets ---
    for name in sorted(presets):
        if name in seen_names:
            continue
        seen_names.add(name)
        preset = presets[name]
        provider = preset.provider if preset.provider != "auto" else infer_provider(preset.model)
        entries.append(
            ModelEntry(
                name=name,
                provider=provider,
                is_preset=True,
                is_recent=False,
                capabilities=_get_caps(preset.model, provider),
            )
        )

    # --- Suggested (curated DEFAULT_MODELS for configured providers) ---
    for provider_name, model_names in DEFAULT_MODELS.items():
        if provider_name == "custom":
            continue
        if not _provider_has_key(config, provider_name):
            continue
        for model in model_names:
            if model in seen_names:
                continue
            seen_names.add(model)
            entries.append(
                ModelEntry(
                    name=model,
                    provider=provider_name,
                    is_preset=False,
                    is_recent=False,
                    capabilities=_get_caps(model, provider_name),
                )
            )

    return entries
