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
from durin.providers.selection import infer_provider

__all__ = ["ModelEntry", "build_entries", "format_entry", "infer_provider"]


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One row in the model picker."""

    name: str
    provider: str
    is_preset: bool
    is_recent: bool
    capabilities: ModelCapabilities = field(default_factory=lambda: ModelCapabilities(model=""))
    # The ``/model`` argument to commit this row (see model_picker.PickerEntry).
    ref: str = ""
    # Section header for the picker ("Easy pick" or a provider name).
    group: str = ""


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
    if caps is None:
        return entry.name
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
    active: str | None,
) -> list[ModelEntry]:
    """Picker rows via the shared builder: easy-pick (active/default/presets/
    recents) first, then the curated catalog of every configured provider.

    Each row carries its provider, role flags, and the ``/model`` ref to
    commit it (a preset/default switches by name; a catalog model by pair).
    """
    from durin.agent.model_picker import picker_entries

    rows = picker_entries(config, presets=presets, recent=recent, active=active)
    return [
        ModelEntry(
            name=r.name,
            provider=r.provider,
            is_preset=(r.role == "preset"),
            is_recent=(r.role == "recent"),
            capabilities=_get_caps(r.name, r.provider),
            ref=r.ref,
            group=r.group,
        )
        for r in rows
    ]
