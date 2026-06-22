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


def _user_cache_path() -> Path | None:
    try:
        from durin.config.paths import get_data_dir

        return get_data_dir() / "provider_models_cache.json"
    except Exception:  # noqa: BLE001
        return None


def _read_index_file(path: Path) -> dict[str, list[ModelInfo]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    providers = (raw or {}).get("providers") or {}
    return {name: _coerce(entries) for name, entries in providers.items()}


@lru_cache(maxsize=1)
def _load_index() -> dict[str, list[ModelInfo]]:
    """Vendored floor, overlaid by the daily user-cache refresh when present."""
    index = _read_index_file(_INDEX_PATH)  # vendored floor
    cache = _user_cache_path()
    if cache is not None and cache.exists():
        index = {**index, **_read_index_file(cache)}  # fresher cache wins per provider
    return index


def provider_models(provider: str, *, access_token: str | None = None) -> list[ModelInfo]:
    """Catalog models for *provider*.

    ``openai_codex`` is not in models.dev — its slugs come from the live codex
    model list. Codex serves the same underlying OpenAI models, so each slug
    inherits the capability metadata (context window, output limit, feature
    flags) of the matching ``openai`` catalog entry when one exists; a slug
    with no openai match keeps a bare id rather than being dropped.
    """
    if provider in ("openai_codex", "openai-codex"):
        from durin.providers.codex_models import list_codex_models

        openai_caps = {mi.id: mi for mi in _load_index().get("openai", ())}
        return [
            openai_caps.get(slug) or ModelInfo(id=slug)
            for slug in list_codex_models(access_token)
        ]
    return list(_load_index().get(provider, ()))


def catalog_model_caps(provider: str, model: str) -> ModelInfo | None:
    for mi in provider_models(provider):
        if mi.id == model:
            return mi
    return None
