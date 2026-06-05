"""LLM invocation helper for the memory subsystem.

Extracted from ``durin.memory.dream`` (§8e) so the new dreams (extract / refine)
and the absorb judge don't depend on the legacy ``DreamConsolidator`` module.
``dream.py`` re-exports these names for the remaining legacy importers until
that module is removed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

__all__ = ["DreamError", "LLMResponse", "LLMInvoke", "default_llm_invoke"]


class DreamError(Exception):
    """Raised when an LLM invocation can't proceed (missing key, bad output, IO)."""


@dataclass
class LLMResponse:
    """Outcome of one LLM call: generated text + best-effort token accounting."""

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMInvoke(Protocol):
    """Any callable taking ``prompt`` + ``model`` → :class:`LLMResponse`."""

    def __call__(self, prompt: str, *, model: str) -> LLMResponse: ...


def default_llm_invoke(
    prompt: str,
    *,
    model: str = "glm-5.1",
    temperature: float = 0.1,
) -> LLMResponse:
    """Production-default LLM invocation via litellm + the zhipu coding plan.

    Reads the API key from durin's secret store and uses the OpenAI-compatible
    adapter (``openai/<model>``) against ``https://api.z.ai/api/coding/paas/v4``.
    Token counts are best-effort (0 when the provider omits usage).
    """
    from durin.security.secrets import get_secret_store

    store = get_secret_store()
    entry = store.get("ZHIPU_API_KEY")
    if entry is None:
        raise DreamError("ZHIPU_API_KEY missing from secret store")
    api_key = entry.value

    import litellm

    response = litellm.completion(
        model=f"openai/{model}",
        messages=[{"role": "user", "content": prompt}],
        api_key=api_key,
        api_base="https://api.z.ai/api/coding/paas/v4",
        temperature=temperature,
    )
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    content = response.choices[0].message.content
    if content is None:
        logger.warning(
            "memory LLM (%s) returned no content (refusal or empty); "
            "treating as empty", model,
        )
    return LLMResponse(
        text=content or "",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
