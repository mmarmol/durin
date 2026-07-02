"""LLM invocation helpers for out-of-loop callers (memory dream, skill judge).

Every call resolves a full ``ModelPresetConfig`` via
:func:`durin.memory.model_resolve.resolve_aux_preset` — the purpose-specific
model when configured (``aux_models.memory`` / ``dream.model_override`` /
``skills.security.llm_judge.model``), otherwise the user's default preset
(provider + model + endpoint + key) — and runs the prompt through that provider.
No model name or endpoint is hardcoded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "DreamError",
    "LLMResponse",
    "LLMInvoke",
    "aux_llm_invoke",
    "aux_llm_invoke_astream",
    "default_llm_invoke",
    "emit_parse_failure",
    "judge_llm_invoke",
    "judge_llm_invoke_astream",
]


class DreamError(Exception):
    """Raised when an LLM invocation can't proceed (missing key, bad output, IO)."""


def emit_parse_failure(stage: str, *, source: str | None = None, raw: str = "") -> None:
    """Telemetry for an unparseable dream-pass LLM response.

    Best-effort: telemetry must never break a dream pass. Callers keep
    their empty-result behavior; this only makes the failure visible.
    """
    try:
        from durin.agent.tools._telemetry import emit_tool_event
        emit_tool_event("memory.dream.parse_failure", {
            "stage": stage,
            "source": source,
            "raw_head": raw[:200],
        })
    except Exception:  # pragma: no cover — never break the dream
        pass


@dataclass
class LLMResponse:
    """Outcome of one LLM call: generated text + best-effort token accounting."""

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMInvoke(Protocol):
    """Any callable taking ``prompt`` + ``model`` → :class:`LLMResponse`."""

    def __call__(self, prompt: str, *, model: str) -> LLMResponse: ...


def _run_blocking(make_coro):
    """Run a coroutine to completion from sync code, whether or not THIS thread
    already drives an event loop.

    The skill-audit tool calls the judge from inside the async agent loop, while
    the service path runs the same sync helper via ``asyncio.to_thread``. A bare
    ``asyncio.run`` would raise "cannot be called from a running event loop" in
    the former; falling back to a one-shot worker thread keeps both safe.
    ``make_coro`` is a zero-arg factory so exactly one coroutine is created.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(make_coro())

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(make_coro())).result()


def _retry_mode(config) -> str:
    try:
        return config.defaults.provider_retry_mode
    except Exception:  # noqa: BLE001 — config optional; default to standard
        return "standard"


def aux_llm_invoke(prompt, *, preset, config, temperature: float = 0.1) -> LLMResponse:
    """Provider-aware single-prompt completion for out-of-loop callers. Builds the
    provider from ``preset`` — the user's resolved provider / model / endpoint /
    key — and runs one user turn through the provider's own retry policy. No
    hardcoded model or endpoint. Loop-safe (see :func:`_run_blocking`)."""
    from durin.providers.factory import make_provider

    provider = make_provider(config, preset=preset)
    mode = _retry_mode(config)

    resp = _run_blocking(
        lambda: provider.chat_with_retry(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            model=preset.model,
            temperature=temperature,
            retry_mode=mode,
        )
    )
    usage = getattr(resp, "usage", None) or {}
    content = getattr(resp, "content", None)
    if content is None:
        logger.warning(
            "aux LLM (%s) returned no content (refusal or empty); treating as empty",
            preset.model,
        )
    return LLMResponse(
        text=content or "",
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
    )


async def aux_llm_invoke_astream(
    prompt,
    *,
    preset,
    config,
    temperature: float = 0.1,
    on_reasoning=None,
    on_content=None,
) -> str:
    """Streaming mirror of :func:`aux_llm_invoke`. Forwards reasoning deltas to
    ``on_reasoning`` and content deltas to ``on_content`` (each an async callable),
    returning the assembled answer text. Provider built from ``preset``; the stream
    open is retried per the provider's policy. No hardcoded model or endpoint."""
    from durin.providers.factory import make_provider

    provider = make_provider(config, preset=preset)
    resp = await provider.chat_stream_with_retry(
        messages=[{"role": "user", "content": prompt}],
        tools=None,
        model=preset.model,
        temperature=temperature,
        on_content_delta=on_content,
        on_thinking_delta=on_reasoning,
        retry_mode=_retry_mode(config),
    )
    return getattr(resp, "content", None) or ""


def _purpose_invoke(prompt, *, purpose: str, model=None, temperature: float = 0.1) -> LLMResponse:
    """Resolve the ``purpose`` preset (specific-or-default, never hardcoded) and run
    ``prompt`` through it. A truthy ``model`` overrides only the model name on the
    resolved provider (legacy per-call override)."""
    from durin.config.loader import load_config
    from durin.memory.model_resolve import resolve_aux_preset

    config = load_config()
    preset = resolve_aux_preset(config, purpose=purpose)
    if model:
        preset = preset.model_copy(update={"model": str(model)})
    return aux_llm_invoke(prompt, preset=preset, config=config, temperature=temperature)


async def _purpose_astream(
    prompt, *, purpose: str, model=None, temperature: float = 0.1, on_reasoning=None, on_content=None
) -> str:
    from durin.config.loader import load_config
    from durin.memory.model_resolve import resolve_aux_preset

    config = load_config()
    preset = resolve_aux_preset(config, purpose=purpose)
    if model:
        preset = preset.model_copy(update={"model": str(model)})
    return await aux_llm_invoke_astream(
        prompt,
        preset=preset,
        config=config,
        temperature=temperature,
        on_reasoning=on_reasoning,
        on_content=on_content,
    )


def default_llm_invoke(prompt: str, *, model: str | None = None, temperature: float = 0.1) -> LLMResponse:
    """Memory-dream default invoke. Resolves the memory preset (``aux_models.memory``
    → ``dream.model_override`` → the user's default preset) and runs through that
    provider. No hardcoded z.ai endpoint, no hardcoded glm-5.1."""
    return _purpose_invoke(prompt, purpose="memory", model=model, temperature=temperature)


def judge_llm_invoke(prompt: str, *, model: str | None = None, temperature: float = 0.1) -> LLMResponse:
    """Skill-judge default invoke. Resolves the judge preset
    (``skills.security.llm_judge.model`` → the user's default preset) — never
    glm-5.1."""
    return _purpose_invoke(prompt, purpose="judge", model=model, temperature=temperature)


async def judge_llm_invoke_astream(
    prompt: str,
    *,
    model: str | None = None,
    temperature: float = 0.1,
    on_reasoning=None,
    on_content=None,
) -> str:
    """Streaming skill-judge invoke (websocket on-demand audit). Resolves the judge
    preset and forwards reasoning deltas to ``on_reasoning`` as they arrive."""
    return await _purpose_astream(
        prompt,
        purpose="judge",
        model=model,
        temperature=temperature,
        on_reasoning=on_reasoning,
        on_content=on_content,
    )
