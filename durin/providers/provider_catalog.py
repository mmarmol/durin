"""Per-provider model catalog — the source for picker groups and the Providers
settings model list. Vendored floor (``provider_models.json``) plus, when
present, a fresher user-cache overlay written by ``catalog_refresh`` (Part 3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_INDEX_PATH = Path(__file__).parent / "data" / "provider_models.json"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool = False
    supports_audio_input: bool = False
    supports_pdf_input: bool = False
    supports_reasoning: bool = False
    supports_function_calling: bool = False


def _coerce(entries: list) -> list[ModelInfo]:
    out: list[ModelInfo] = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        out.append(
            ModelInfo(
                id=e["id"],
                max_input_tokens=e.get("max_input_tokens"),
                max_output_tokens=e.get("max_output_tokens"),
                supports_vision=bool(e.get("supports_vision")),
                supports_audio_input=bool(e.get("supports_audio_input")),
                supports_pdf_input=bool(e.get("supports_pdf_input")),
                supports_reasoning=bool(e.get("supports_reasoning")),
                supports_function_calling=bool(e.get("supports_function_calling")),
            )
        )
    return out


@lru_cache(maxsize=1)
def _load_index() -> dict[str, list[ModelInfo]]:
    """Vendored floor; Part 3 extends this with the user-cache overlay."""
    try:
        raw = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    providers = (raw or {}).get("providers") or {}
    return {name: _coerce(entries) for name, entries in providers.items()}


def provider_models(provider: str, *, access_token: str | None = None) -> list[ModelInfo]:
    """Catalog models for *provider*. ``openai_codex`` is not in models.dev — it
    comes from the live codex model list (slugs only, no capability metadata)."""
    if provider in ("openai_codex", "openai-codex"):
        from durin.providers.codex_models import list_codex_models

        return [ModelInfo(id=s) for s in list_codex_models(access_token)]
    return list(_load_index().get(provider, ()))


def catalog_model_caps(provider: str, model: str) -> ModelInfo | None:
    for mi in provider_models(provider):
        if mi.id == model:
            return mi
    return None
