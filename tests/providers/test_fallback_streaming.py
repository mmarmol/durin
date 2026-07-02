"""FallbackProvider failover vs. the streaming transport.

Every completion now rides the streaming transport (base ``chat_with_retry``),
so ``FallbackProvider.chat_stream`` sees ALL traffic. Its ``has_streamed``
guard exists to avoid delivering DUPLICATE content to a delta consumer after a
mid-stream failure — it must therefore only arm when the caller actually
supplied ``on_content_delta``. For consumer-less callers (dream, curation,
consolidation) a mid-stream primary failure must still fail over.
"""
from __future__ import annotations

import pytest

from durin.providers.base import LLMProvider, LLMResponse
from durin.providers.fallback_provider import FallbackProvider


class _StreamsThenErrors(LLMProvider):
    async def chat(self, **kw) -> LLMResponse:  # pragma: no cover - unused
        raise AssertionError("unused")

    async def chat_stream(self, *, on_content_delta=None, **kw) -> LLMResponse:
        if on_content_delta:
            await on_content_delta("partial ")
        return LLMResponse(content="partial", finish_reason="error",
                           error_kind="connection")

    def get_default_model(self) -> str:
        return "primary-model"


class _HealthyFallback(LLMProvider):
    async def chat(self, **kw) -> LLMResponse:
        return LLMResponse(content="fallback ok")

    async def chat_stream(self, **kw) -> LLMResponse:
        return LLMResponse(content="fallback ok")

    def get_default_model(self) -> str:
        return "fallback-model"


def _fp() -> FallbackProvider:
    from durin.config.schema import ModelPresetConfig
    preset = ModelPresetConfig(model="fallback-model", provider="p")
    return FallbackProvider(_StreamsThenErrors(), [preset],
                            lambda preset: _HealthyFallback())


@pytest.mark.asyncio
async def test_failover_runs_when_no_delta_consumer() -> None:
    resp = await _fp().chat_stream(messages=[{"role": "user", "content": "hi"}])
    assert resp.content == "fallback ok"


@pytest.mark.asyncio
async def test_failover_suppressed_when_deltas_reached_consumer() -> None:
    seen: list[str] = []

    async def sink(text: str) -> None:
        seen.append(text)

    resp = await _fp().chat_stream(messages=[{"role": "user", "content": "hi"}],
                                   on_content_delta=sink)
    assert resp.finish_reason == "error"  # consumer already saw partial content
    assert seen == ["partial "]
