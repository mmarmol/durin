"""Construction of auxiliary-modality provider handles (vision / audio / …).

Shared by every surface that builds a tool registry with capability
bridges: the main loop builds the handles once at startup, while the
subagent manager and the workflow node runner build them on demand so a
hot-reloaded ``agents.aux_models`` change takes effect without a restart.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from durin.agent.tools.context import AuxProviderHandle


def build_aux_providers(config: Any) -> dict[str, AuxProviderHandle]:
    """Construct one provider per configured auxiliary modality.

    For each entry in ``config.agents.aux_models`` (vision / audio / …)
    we resolve the referenced preset or build an inline
    ``ModelPresetConfig`` and call :func:`make_provider`. The resulting
    providers are reused for every bridge-tool invocation within the
    registry they were built for — credentials and HTTP clients open
    once per registry build, not per call.

    Returns an empty dict when no aux models are configured; bridge
    tools then see no entry and stay hidden from the LLM's tool list.
    """
    from durin.config.schema import ModelPresetConfig
    from durin.providers.factory import make_provider

    out: dict[str, AuxProviderHandle] = {}
    aux = getattr(getattr(config, "agents", None), "aux_models", None)
    if aux is None:
        return out
    for kind in ("vision", "audio", "memory"):
        entry = getattr(aux, kind, None)
        if entry is None:
            continue
        preset: ModelPresetConfig
        if entry.preset:
            try:
                preset = config.resolve_preset(entry.preset)
            except Exception:
                logger.exception("Failed to resolve aux preset {!r} for {} bridge", entry.preset, kind)
                continue
        elif entry.model:
            preset = ModelPresetConfig(
                model=entry.model,
                provider=entry.provider or "auto",
            )
        else:
            continue
        try:
            provider = make_provider(config, preset=preset)
        except Exception:
            logger.exception("Failed to build aux provider for {} bridge (model={}, provider={})",
                             kind, preset.model, preset.provider)
            continue
        out[kind] = AuxProviderHandle(provider=provider, model=preset.model)
        logger.info("Aux {} bridge enabled — model={} provider={}",
                    kind, preset.model, preset.provider)
    return out
