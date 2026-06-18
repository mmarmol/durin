"""Surface-agnostic builder for the model picker (TUI + webui).

Single source of truth for what the picker shows and in what order: an
"Easy pick" group (active, default, presets, recents) followed by the
per-provider catalog of every *configured* provider, sourced from
``provider_models.json``. Every entry carries its provider so a selection
commits a ``(provider, model)`` pair, and its capabilities so surfaces can show
them. Providers are resolved from the catalog or the config's own resolution —
never from a bare-name keyword guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from durin.config.schema import ModelPresetConfig
from durin.providers.selection import configured_provider_names

_EASY = "Easy pick"


@dataclass(frozen=True, slots=True)
class PickerEntry:
    name: str
    provider: str
    group: str
    role: str  # active | default | preset | recent | catalog
    # The exact ``/model`` argument to commit this row. Named presets (and the
    # reserved ``default``) switch by name to preserve their params; everything
    # else commits an explicit ``provider model`` pair. Clients send
    # ``/model {ref}`` verbatim — no per-client branching.
    ref: str
    # Capabilities (from the catalog) so surfaces can show them inline.
    max_input_tokens: int | None = None
    supports_vision: bool = False
    supports_audio_input: bool = False
    supports_reasoning: bool = False


def picker_entries(
    config: Any,
    *,
    presets: dict[str, ModelPresetConfig],
    recent: list[str],
    active: str | None,
) -> list[PickerEntry]:
    """Ordered picker rows: easy-pick group first, then the per-provider catalog."""
    from durin.providers.provider_catalog import catalog_model_caps, provider_models

    out: list[PickerEntry] = []
    seen: set[str] = set()  # dedupe by ref (a model may appear under 2 providers)
    configured = configured_provider_names(config)

    # Pull each configured provider's catalog once; served_by resolves a recent's
    # provider WITHOUT keyword-guessing (glm-* would keyword-match zhipu, but the
    # user may run zai_coding_plan).
    catalog: dict[str, list] = {}
    served_by: dict[str, str] = {}
    for pname in configured:
        if pname == "custom":
            continue
        infos = provider_models(pname)
        catalog[pname] = infos
        for mi in infos:
            served_by.setdefault(mi.id, pname)

    def add(name: str, provider: str, group: str, role: str, ref: str) -> None:
        # Easy-pick rows (default/active/preset) commit by name, so an unresolved
        # "auto" provider is fine — the default must always be offered. Catalog
        # rows always pass a real provider; recents are pre-filtered to resolvable
        # ones. So only guard against empties and duplicate refs.
        if not name or not provider or ref in seen:
            return
        seen.add(ref)
        mi = catalog_model_caps(provider, name)
        out.append(
            PickerEntry(
                name=name,
                provider=provider,
                group=group,
                role=role,
                ref=ref,
                max_input_tokens=mi.max_input_tokens if mi else None,
                supports_vision=bool(mi and mi.supports_vision),
                supports_audio_input=bool(mi and mi.supports_audio_input),
                supports_reasoning=bool(mi and mi.supports_reasoning),
            )
        )

    def _provider_of(preset: ModelPresetConfig) -> str:
        if preset.provider != "auto":
            return preset.provider
        return served_by.get(preset.model) or config.get_provider_name(preset=preset) or "auto"

    # Easy pick — active, default, presets, recents.
    if active and active in presets:
        p = presets[active]
        add(p.model, _provider_of(p), _EASY, "active", active)
    d = config.agents.defaults
    default_prov = (
        d.provider
        if d.provider != "auto"
        else (served_by.get(d.model) or config.get_provider_name() or "auto")
    )
    add(d.model, default_prov, _EASY, "default", "default")
    for pname in sorted(presets):
        p = presets[pname]
        add(p.model, _provider_of(p), _EASY, "preset", pname)
    for m in recent:
        prov = served_by.get(m)
        if prov:  # only surface a recent we can resolve — no guessing
            add(m, prov, _EASY, "recent", f"{prov} {m}")

    # Catalog — every model of every configured provider.
    for pname in sorted(catalog):
        for mi in catalog[pname]:
            add(mi.id, pname, pname, "catalog", f"{pname} {mi.id}")

    return out
