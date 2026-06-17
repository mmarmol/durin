"""Surface-agnostic builder for the model picker (TUI + webui).

Single source of truth for what the picker shows and in what order: an
"Easy pick" group (active, default, presets, recents) followed by the
curated catalog of every *configured* provider. Every entry carries its
provider so a selection commits a ``(provider, model)`` pair — no
name-based provider inference downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from durin.config.schema import ModelPresetConfig
from durin.providers.selection import configured_provider_names, infer_provider

_EASY = "Easy pick"


@dataclass(frozen=True, slots=True)
class PickerEntry:
    name: str
    provider: str
    group: str
    role: str  # active | default | preset | recent | catalog


def picker_entries(
    config: Any,
    *,
    presets: dict[str, ModelPresetConfig],
    recent: list[str],
    active: str | None,
) -> list[PickerEntry]:
    """Ordered picker rows: easy-pick group first, then configured catalog."""
    from durin.cli.onboard_wizard import DEFAULT_MODELS

    out: list[PickerEntry] = []
    seen: set[tuple[str, str]] = set()
    default_provider = config.agents.defaults.provider
    default_model = config.agents.defaults.model

    def _provider_of(preset: ModelPresetConfig) -> str:
        return preset.provider if preset.provider != "auto" else infer_provider(preset.model)

    def add(name: str, provider: str, group: str, role: str) -> None:
        key = (provider, name)
        if not name or key in seen:
            return
        seen.add(key)
        out.append(PickerEntry(name=name, provider=provider, group=group, role=role))

    # Easy pick — active, default, presets, recents.
    if active and active in presets:
        p = presets[active]
        add(p.model, _provider_of(p), _EASY, "active")
    add(
        default_model,
        default_provider if default_provider != "auto" else infer_provider(default_model),
        _EASY,
        "default",
    )
    for pname in sorted(presets):
        p = presets[pname]
        add(p.model, _provider_of(p), _EASY, "preset")
    for m in recent:
        add(m, infer_provider(m), _EASY, "recent")

    # Catalog — curated defaults of every configured provider.
    configured = configured_provider_names(config)
    for provider_name, models in DEFAULT_MODELS.items():
        if provider_name == "custom" or provider_name not in configured:
            continue
        for m in models:
            add(m, provider_name, provider_name, "catalog")

    return out
