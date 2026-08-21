"""FallbackProvider must delegate `provider_key` to its primary — like it
already does for `generation` and `get_default_model`.

Without this, ``self.provider_key`` on a FallbackProvider falls through to the
``LLMProvider`` base class's class-level default (``None``), so:
- ``LLMProvider.emit_call_telemetry`` (called by ``_run_with_retry`` on every
  retry-wrapped call) logs ``"provider": type(self).__name__`` — the literal
  class name ``"FallbackProvider"`` — instead of the primary's config-registry
  key, defeating the "who spent the tokens" point of the telemetry.
- ``AgentNodeRunner.reuse_identity()`` (``durin/workflow/node_runner.py``)
  reads ``getattr(self.runner.provider, "provider_key", None)``, so with
  fallback models configured it always resolves ``provider=None`` and the
  reuse gate can never match.
"""
from __future__ import annotations

from typing import Any

import pytest

from durin.config.schema import ModelPresetConfig
from durin.providers.base import LLMProvider, LLMResponse
from durin.providers.fallback_provider import FallbackProvider


class _StampedProvider(LLMProvider):
    """A minimal provider carrying a `provider_key` the way the real factory
    stamps one (`provider.provider_key = provider_name or None`)."""

    def __init__(self, key: str):
        super().__init__()
        self.provider_key = key

    def get_default_model(self) -> str:
        return "primary-model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        return LLMResponse(content="ok")


class _FakeTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


def test_fallback_provider_exposes_the_primary_provider_key() -> None:
    primary = _StampedProvider("zai_coding_plan")
    preset = ModelPresetConfig(model="fallback-model", provider="p")
    wrapped = FallbackProvider(primary, [preset], lambda _preset: primary)

    assert wrapped.provider_key == "zai_coding_plan"


@pytest.mark.asyncio
async def test_call_through_the_wrapper_reports_the_primary_key_in_telemetry() -> None:
    primary = _StampedProvider("zai_coding_plan")
    # No fallback presets: chat_with_retry rides the FallbackProvider's own
    # chat_stream, which (with nothing to fail over to) just delegates
    # straight to the primary — the interesting thing under test is which
    # provider_key the WRAPPER stamps on the telemetry event, not failover.
    wrapped = FallbackProvider(primary, [], lambda _preset: primary)
    sink = _FakeTelemetry()
    wrapped.set_telemetry(sink)

    await wrapped.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    calls = [payload for name, payload in sink.events if name == "provider.call"]
    assert len(calls) == 1
    assert calls[0]["provider"] == "zai_coding_plan"
