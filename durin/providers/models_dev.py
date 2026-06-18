"""models.dev (``api.json``) → durin per-provider catalog.

Shared by ``scripts/refresh_model_capabilities.py`` (vendored snapshot) and
``durin.providers.catalog_refresh`` (daily user-cache refresh). models.dev keys
providers by its own id; ``MODELS_DEV_TO_DURIN`` maps the ones whose id differs
from durin's ``ProvidersConfig`` field name. Exact-name matches need no entry.
"""

from __future__ import annotations

MODELS_DEV_URL = "https://models.dev/api.json"

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
