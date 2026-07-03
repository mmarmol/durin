"""models.dev (``api.json``) → durin per-provider catalog.

Shared by ``scripts/refresh_model_capabilities.py`` (vendored snapshot) and
``durin.providers.catalog_refresh`` (daily user-cache refresh). models.dev keys
providers by its own id; ``MODELS_DEV_TO_DURIN`` maps the ones whose id differs
from durin's ``ProvidersConfig`` field name. Exact-name matches need no entry.

NVIDIA is special-cased: its ``/v1/models`` endpoint is public (no API key
needed), so the model *ids* come from NVIDIA itself — models.dev drifts badly
for this provider (stale ids, and it re-spells version separators, e.g.
``3_1``/``v03`` for NVIDIA's ``3.1``/``v0.3``, producing ids the API 404s on).
models.dev only fills in the capability metadata the endpoint doesn't expose.
"""

from __future__ import annotations

import json
import urllib.request

MODELS_DEV_URL = "https://models.dev/api.json"

NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"

#: models.dev provider id -> durin ProvidersConfig field name (only mismatches).
MODELS_DEV_TO_DURIN: dict[str, str] = {
    "zhipuai": "zhipu",
    "zai-coding-plan": "zai_coding_plan",
    "moonshotai": "moonshot",
    "google": "gemini",
    "amazon-bedrock": "bedrock",
    "azure": "azure_openai",
    "alibaba": "dashscope",
    "xiaomi": "xiaomi_mimo",
    "lmstudio": "lm_studio",
    "atomic-chat": "atomic_chat",
    "github-copilot": "github_copilot",
}


def build_provider_models(
    data: dict, durin_provider_names: set[str]
) -> dict[str, list[dict]]:
    """Per-provider model index from a raw models.dev ``api.json`` dict.

    Unlike the ``scripts`` capability flattener this keeps the per-provider
    structure and does NOT gate on a trusted-vendor list — the catalog must
    show every model a configured provider can actually serve.
    """
    out: dict[str, list[dict]] = {}
    for md_id, prov in data.items():
        durin = MODELS_DEV_TO_DURIN.get(md_id)
        if durin is None:
            durin = md_id if md_id in durin_provider_names else None
        if durin is None:
            continue
        models = (prov or {}).get("models") or {}
        for mid, m in models.items():
            if not isinstance(m, dict):
                continue
            mods = m.get("modalities") or {}
            in_mods = set(mods.get("input") or [])
            limits = m.get("limit") or {}
            out.setdefault(durin, []).append(
                {
                    "id": m.get("id", mid),
                    "max_input_tokens": limits.get("context"),
                    "max_output_tokens": limits.get("output"),
                    "supports_vision": "image" in in_mods,
                    "supports_audio_input": "audio" in in_mods,
                    "supports_pdf_input": "pdf" in in_mods,
                    "supports_reasoning": bool(m.get("reasoning")),
                    "supports_function_calling": bool(m.get("tool_call")),
                }
            )
    for entries in out.values():
        entries.sort(key=lambda e: e["id"])
    return out


def fetch_nvidia_model_ids(timeout: float = 30.0) -> list[str] | None:
    """Model ids from NVIDIA's public ``/v1/models``. ``None`` on any failure
    (network, parse, empty list) so callers can fall back to prior data."""
    try:
        req = urllib.request.Request(NVIDIA_MODELS_URL, headers={"User-Agent": "durin"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [
            m["id"]
            for m in (data.get("data") or [])
            if isinstance(m, dict) and m.get("id")
        ]
        return ids or None
    except Exception:  # noqa: BLE001 — network/parse: caller keeps prior data
        return None


def _loose_id(model_id: str) -> str:
    """Separator-insensitive form of a model id, for matching NVIDIA's ids
    against models.dev's re-spelled ones (``v03``→``v0.3``, ``3_1``→``3.1``)."""
    return model_id.lower().replace(".", "").replace("_", "").replace("-", "")


def apply_nvidia_live_ids(entries: list[dict], live_ids: list[str]) -> list[dict]:
    """Rebuild the nvidia catalog from *live_ids* (ground truth for which
    models exist and how their ids are spelled), keeping each model's
    capability fields from the models.dev-derived *entries* when one matches
    loosely. Unmatched live models get a bare entry with unknown caps."""
    by_loose: dict[str, dict] = {}
    for e in entries:
        by_loose.setdefault(_loose_id(e["id"]), e)
    out: list[dict] = []
    for lid in sorted(set(live_ids)):
        matched = by_loose.get(_loose_id(lid)) or {}
        out.append(
            {
                "id": lid,
                "max_input_tokens": matched.get("max_input_tokens"),
                "max_output_tokens": matched.get("max_output_tokens"),
                "supports_vision": bool(matched.get("supports_vision")),
                "supports_audio_input": bool(matched.get("supports_audio_input")),
                "supports_pdf_input": bool(matched.get("supports_pdf_input")),
                "supports_reasoning": bool(matched.get("supports_reasoning")),
                "supports_function_calling": bool(
                    matched.get("supports_function_calling")
                ),
            }
        )
    return out
