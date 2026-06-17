"""Helpers for runtime model preset selection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from durin.config.schema import ModelPresetConfig
from durin.providers.base import LLMProvider
from durin.providers.factory import ProviderSnapshot, build_provider_snapshot

# Loaders receive the preset name and the in-memory preset object (when one is
# known). Forwarding the object lets a runtime-injected preset — one the loader
# would otherwise fail to find when it re-resolves the name against the on-disk
# config — resolve from memory while the config still supplies credentials.
PresetSnapshotLoader = Callable[..., ProviderSnapshot]


def default_selection_signature(signature: tuple[object, ...] | None) -> tuple[object, ...] | None:
    return signature[:2] if signature else None


def configured_model_presets(config: Any) -> dict[str, ModelPresetConfig]:
    return {**config.model_presets, "default": config.resolve_default_preset()}


def make_preset_snapshot_loader(
    config: Any,
    provider_snapshot_loader: Callable[..., ProviderSnapshot] | None,
) -> PresetSnapshotLoader:
    if provider_snapshot_loader is not None:
        return lambda name, preset=None: provider_snapshot_loader(
            preset_name=name, preset=preset
        )
    return lambda name, preset=None: build_provider_snapshot(
        config, preset_name=name, preset=preset
    )


def build_static_preset_snapshot(
    provider: LLMProvider,
    name: str,
    preset: ModelPresetConfig,
) -> ProviderSnapshot:
    provider.generation = preset.to_generation_settings()
    return ProviderSnapshot(
        provider=provider,
        model=preset.model,
        context_window_tokens=preset.context_window_tokens,
        signature=("model_preset", name, preset.model_dump_json()),
        preemptive_compact_ratio=preset.preemptive_compact_ratio,
    )


def build_runtime_preset_snapshot(
    *,
    name: str,
    presets: dict[str, ModelPresetConfig],
    provider: LLMProvider,
    loader: PresetSnapshotLoader | None,
) -> ProviderSnapshot:
    if loader is not None:
        return loader(name, preset=presets.get(name))
    return build_static_preset_snapshot(provider, name, presets[name])


def normalize_preset_name(name: str | None, presets: dict[str, ModelPresetConfig]) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("model_preset must be a non-empty string")
    name = name.strip()
    if name not in presets:
        raise KeyError(f"model_preset {name!r} not found. Available: {', '.join(presets) or '(none)'}")
    return name

