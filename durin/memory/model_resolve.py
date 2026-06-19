"""Resolve which LLM model to use for memory subsystem operations.

The ``memory_dream`` passes read their model name through this helper.
Precedence:

1. ``config.agents.aux_models.memory`` — when set, the user has opted
   into a memory-specific model. If it points at a preset, the preset's
   ``model`` field wins; otherwise the inline ``model`` field is used.
2. ``config.memory.dream.model_override`` — per-dream knob.
3. ``None`` — caller falls back to its own default (the bundled
   ``default_llm_invoke`` default).

Provider override is **not** supported by this helper. The dream
invokers hardcode the provider (entity-centric uses zhipu via
``default_llm_invoke``). If the resolved model name is not served by
the active provider the call will fail at LLM time — keep the model
name compatible with the dream's provider until the broader
aux-provider wiring lands.
"""

from __future__ import annotations

from typing import Any

__all__ = ["resolve_memory_model", "resolve_aux_preset"]


def resolve_memory_model(app_config: Any) -> str | None:
    """Return the model name memory ops should use, or ``None`` for default.

    ``app_config`` is the full ``DurinConfig``. Missing / partial config
    is tolerated — any ``AttributeError`` collapses to the next
    precedence level.
    """
    if app_config is None:
        return None

    # 1. aux_models.memory
    aux_mem = None
    try:
        aux_mem = app_config.agents.aux_models.memory
    except AttributeError:
        aux_mem = None
    if aux_mem is not None:
        preset_name = getattr(aux_mem, "preset", None)
        if preset_name:
            try:
                preset = app_config.resolve_preset(preset_name)
                model = getattr(preset, "model", None)
                if model:
                    return str(model)
            except Exception:
                pass
        inline_model = getattr(aux_mem, "model", None)
        if inline_model:
            return str(inline_model)

    # 2. dream.model_override
    try:
        override = app_config.memory.dream.model_override
    except AttributeError:
        override = None
    if override:
        return str(override)

    return None


def resolve_aux_preset(app_config: Any, *, purpose: str):
    """Return the fully-resolved ``ModelPresetConfig`` for an out-of-loop LLM call.

    Precedence — purpose-specific override → memory's ``model_override`` →
    the user's default preset. NEVER returns a hardcoded model or ``None``: the
    invoke builds the provider from this, so the call runs on the user's own
    provider / endpoint / key. ``purpose`` is ``"memory"`` or ``"judge"``.
    """
    default = app_config.resolve_default_preset()

    def _on_default_provider(model: str, provider: str = "auto"):
        prov = provider if provider and provider != "auto" else default.provider
        return default.model_copy(update={"model": str(model), "provider": prov})

    if purpose == "memory":
        try:
            aux = app_config.agents.aux_models.memory
        except AttributeError:
            aux = None
        if aux is not None:
            preset_name = getattr(aux, "preset", None)
            if preset_name:
                try:
                    return app_config.resolve_preset(preset_name)
                except Exception:  # noqa: BLE001
                    pass
            inline = getattr(aux, "model", None)
            if inline:
                return _on_default_provider(inline, getattr(aux, "provider", "auto"))
        try:
            override = app_config.memory.dream.model_override
        except AttributeError:
            override = None
        if override:
            return _on_default_provider(override)
        return default

    if purpose == "judge":
        try:
            jm = app_config.skills.security.llm_judge.model
        except AttributeError:
            jm = None
        if jm:
            return _on_default_provider(jm)
        return default

    return default
