"""LLM invocation helper for the memory subsystem.

The dream passes (extract / refine) and the absorb judge call the LLM through
this helper.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

__all__ = ["DreamError", "LLMResponse", "LLMInvoke", "default_llm_invoke"]


def _retry_llm_call(call, *, mode: str = "standard"):
    """Run ``call()`` with the same retry policy as chat (single source: the
    constants + transient classifier on ``LLMProvider``). ``standard`` walks the
    6-delay schedule (7 attempts); ``persistent`` caps each delay and keeps
    retrying until the same error repeats ``_PERSISTENT_IDENTICAL_ERROR_LIMIT``
    times. Non-transient errors are re-raised immediately."""
    from durin.providers.base import LLMProvider

    delays = LLMProvider._CHAT_RETRY_DELAYS
    attempt = 0
    identical = 0
    last_text: str | None = None
    while True:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — re-raised below if not transient
            text = str(exc)
            if not LLMProvider._is_transient_error(text):
                raise
            if mode == "persistent":
                identical = identical + 1 if text == last_text else 1
                last_text = text
                if identical >= LLMProvider._PERSISTENT_IDENTICAL_ERROR_LIMIT:
                    raise
                delay = min(delays[min(attempt, len(delays) - 1)],
                            LLMProvider._PERSISTENT_MAX_DELAY)
            else:  # standard
                if attempt >= len(delays):
                    raise
                delay = delays[attempt]
            logger.info("memory LLM transient error, retrying in %ss: %s", delay, text)
            time.sleep(delay)
            attempt += 1


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

    try:
        from durin.config.loader import load_config
        mode = load_config().defaults.provider_retry_mode
    except Exception:  # noqa: BLE001 — config optional; default to standard
        mode = "standard"

    response = _retry_llm_call(
        lambda: litellm.completion(
            model=f"openai/{model}",
            messages=[{"role": "user", "content": prompt}],
            api_key=api_key,
            api_base="https://api.z.ai/api/coding/paas/v4",
            temperature=temperature,
        ),
        mode=mode,
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
