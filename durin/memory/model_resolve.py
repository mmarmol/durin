"""Resolve which (provider, model) preset out-of-loop LLM calls should use.

A model name only means something under its provider — the provider-first
contract the model picker established. Every purpose-specific knob resolves
here to a full ``ModelPresetConfig`` (provider + model + endpoint + key):

1. Purpose override — ``agents.aux_models.memory`` (a preset ref or an inline
   model+provider pair) for ``purpose="memory"``; ``skills.security.llm_judge``
   (model + provider) for ``purpose="judge"``; ``agents.aux_models.loops`` for
   ``purpose="loops"``.
2. ``memory.dream.model_override`` (DEPRECATED, memory purpose only) — a bare
   name, placed by provider auto-detection from the name.
3. The user's default preset.

``purpose="loops"`` is the one exception to "never returns None" below: loops'
per-message trigger filter and goal-judge calls are meant to ride whatever
model is live in the interactive session by default (not a separately
resolved default preset that could lag a live ``/model`` switch), so an
unconfigured ``aux_models.loops`` resolves to ``None`` and the caller passes
that straight through as "no override" to ``AgentLoop.process_direct``.

When a knob names a model without a provider (or with ``"auto"``), the
provider is detected from the model name among the CONFIGURED providers — the
same matcher the main model path uses. A name no configured provider serves
falls back to the WHOLE default preset (specific-or-default): pairing a
foreign model name with the default provider is never correct and used to
produce silent 404s.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["resolve_aux_preset"]


def _place_model(app_config: Any, default: Any, model: str, provider: str | None,
                 *, purpose: str):
    """Pair `model` with its provider, returning a full preset copy.

    Explicit provider wins. Otherwise the provider is auto-detected from the
    model name among configured providers; no match → the untouched default
    preset (specific-or-default), with a loud log naming the dropped model.
    """
    model = str(model)
    if provider and provider != "auto":
        return default.model_copy(update={"model": model, "provider": str(provider)})
    spec_name = None
    try:
        _pconf, spec_name = app_config.match_provider_by_name(model)
    except Exception:  # noqa: BLE001 - detection is best-effort; fall through to default
        spec_name = None
    if spec_name:
        return default.model_copy(update={"model": model, "provider": spec_name})
    logger.warning(
        "aux model %r (purpose=%s) is not served by any configured provider; "
        "using the default preset %s/%s instead",
        model, purpose, default.provider, default.model,
    )
    return default


def resolve_aux_preset(app_config: Any, *, purpose: str):
    """Return the fully-resolved ``ModelPresetConfig`` for an out-of-loop LLM call.

    NEVER returns a hardcoded model: the invoke builds the provider from this,
    so the call runs on the user's own provider / endpoint / key. ``purpose``
    is ``"memory"``, ``"judge"``, or ``"loops"``. ``"loops"`` is the only
    purpose that can return ``None`` (see the module docstring) — every other
    purpose falls back to the whole default preset instead.
    """
    default = app_config.resolve_default_preset()

    if purpose == "loops":
        try:
            aux = app_config.agents.aux_models.loops
        except AttributeError:
            aux = None
        if aux is None:
            return None
        preset_name = getattr(aux, "preset", None)
        if preset_name:
            try:
                return app_config.resolve_preset(preset_name)
            except Exception:  # noqa: BLE001
                pass
        inline = getattr(aux, "model", None)
        if inline:
            return _place_model(app_config, default, inline,
                                getattr(aux, "provider", "auto"), purpose=purpose)
        return None

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
                return _place_model(app_config, default, inline,
                                    getattr(aux, "provider", "auto"), purpose=purpose)
        try:
            override = app_config.memory.dream.model_override
        except AttributeError:
            override = None
        if override:
            logger.info(
                "memory.dream.model_override is deprecated — prefer "
                "agents.aux_models.memory (model + provider)")
            return _place_model(app_config, default, override, "auto", purpose=purpose)
        return default

    if purpose == "judge":
        try:
            judge = app_config.skills.security.llm_judge
        except AttributeError:
            judge = None
        jm = getattr(judge, "model", None) if judge is not None else None
        if jm:
            return _place_model(app_config, default, jm,
                                getattr(judge, "provider", "auto"), purpose=purpose)
        return default

    return default
