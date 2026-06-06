"""Codex model list: live discovery from the OAuth backend, static fallback.

Live discovery is the source of truth. The static fallback mirrors hermes and
is only used when there is no token or the endpoint is unreachable.
"""

from __future__ import annotations

import httpx
from loguru import logger

MODELS_URL = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
ORIGINATOR = "codex_cli_rs"

STATIC_FALLBACK: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
)

DEFAULT_MODEL = "gpt-5.5"


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(10.0))


def _discover(access_token: str) -> list[str]:
    with _client() as client:
        resp = client.get(
            MODELS_URL,
            headers={"Authorization": f"Bearer {access_token}", "originator": ORIGINATOR},
        )
    if resp.status_code != 200:
        return []
    data = resp.json()
    entries = data.get("models", []) if isinstance(data, dict) else []
    ranked: list[tuple[int, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = str(item.get("visibility", "")).strip().lower()
        if visibility in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        ranked.append((rank, slug.strip()))
    ranked.sort(key=lambda x: (x[0], x[1]))
    return [slug for _, slug in ranked]


def list_codex_models(access_token: str | None) -> list[str]:
    """Discovered models when a token is available; static fallback otherwise."""
    if access_token:
        try:
            models = _discover(access_token)
            if models:
                return models
        except Exception as exc:  # noqa: BLE001
            logger.debug("codex model discovery failed: {}", exc)
    return list(STATIC_FALLBACK)
