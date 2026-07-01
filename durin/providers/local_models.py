"""Live model discovery for local OpenAI-compatible providers.

Queries GET <api_base>/models (the standard /v1/models endpoint) and returns
the list of model ids.  Returns [] on any error so the caller can fall back to
the static catalog without propagating exceptions.
"""

from __future__ import annotations

import httpx
from loguru import logger


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def list_local_models(
    api_base: str,
    api_key: str | None = None,
    timeout: float = 3.0,
) -> list[str]:
    """Return model ids available at *api_base*/models, or [] on any failure.

    api_base already contains /v1 (e.g. http://localhost:11434/v1), so we
    append /models to reach the standard OpenAI-compatible list endpoint.
    """
    url = api_base.rstrip("/") + "/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with _client(timeout) as client:
            resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [item["id"] for item in data if isinstance(item, dict) and item.get("id")]
    except Exception as exc:  # noqa: BLE001
        logger.debug("local model discovery failed for {}: {}", url, exc)
        return []
