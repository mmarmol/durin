"""Provider selection helpers (Textual-free, shared by TUI/webui/CLI)."""

from __future__ import annotations

from typing import Any

from durin.providers.registry import PROVIDERS


def matching_provider_names(model: str) -> list[str]:
    """Provider config-names whose keywords match *model*, in registry order."""
    model_lower = model.lower()
    model_normalized = model_lower.replace("-", "_")
    out: list[str] = []
    for spec in PROVIDERS:
        for kw in spec.keywords:
            if kw in model_lower or kw.replace("-", "_") in model_normalized:
                out.append(spec.name)
                break
    return out


def infer_provider(model: str) -> str:
    """First keyword-matched provider config-name, or ``"auto"``."""
    matches = matching_provider_names(model)
    return matches[0] if matches else "auto"


def configured_provider_names(config: Any) -> set[str]:
    """Provider config-names the user has set up (key / base / OAuth)."""
    from durin.utils.oauth import oauth_token_present

    out: set[str] = set()
    for spec in PROVIDERS:
        pc = getattr(config.providers, spec.name, None)
        if getattr(spec, "is_oauth", False):
            ok = oauth_token_present(spec.name)
        elif getattr(spec, "is_local", False):
            ok = bool(pc and getattr(pc, "api_base", None))
        else:
            ok = bool(pc and getattr(pc, "api_key", None))
        if ok:
            out.add(spec.name)
    return out
