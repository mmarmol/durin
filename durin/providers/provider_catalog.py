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


def _load_config_for_local():
    """Config accessor for the local-provider live-model path. Split out so
    tests can stub it without touching the on-disk config."""
    from durin.config.loader import load_config

    return load_config()


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

    # Local providers (ollama/lm_studio/vllm/…) serve only what the user has
    # actually pulled/loaded, so the static catalog would show phantom models.
    # Prefer the live /v1/models list; fall back to the static floor on any
    # failure (unreachable server, empty list).
    from durin.providers.registry import find_by_name

    spec = find_by_name(provider)
    if spec is not None and getattr(spec, "is_local", False):
        api_base = None
        api_key = None
        try:
            pconf = getattr(_load_config_for_local().providers, provider, None)
            api_base = getattr(pconf, "api_base", None) or getattr(spec, "default_api_base", None)
            api_key = getattr(pconf, "api_key", None)
        except Exception:  # noqa: BLE001
            api_base = getattr(spec, "default_api_base", None)
        if api_base:
            from durin.providers import local_models

            ids = local_models.list_local_models(api_base, api_key)
            if ids:
                return [ModelInfo(id=i) for i in ids]
        return list(_load_index().get(provider, ()))

    return list(_load_index().get(provider, ()))


def catalog_model_caps(provider: str, model: str) -> ModelInfo | None:
    for mi in provider_models(provider):
        if mi.id == model:
            return mi
    return None
