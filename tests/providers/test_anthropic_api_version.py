"""The Anthropic provider speaks API version 2026-10-21 to Anthropic and keeps
the 2023-06-01 wire to Anthropic-compatible third parties (MiniMax, proxies)."""

from __future__ import annotations

import inspect
import json

import httpx2
import pytest
from anthropic.resources.messages import AsyncMessages

from durin.providers.anthropic_provider import AnthropicProvider

_MINIMAX = "https://api.minimax.io/anthropic"


def _build(provider: AnthropicProvider, model: str, reasoning_effort: str | None) -> dict:
    return provider._build_kwargs(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {
            "name": "f", "description": "d", "parameters": {"type": "object", "properties": {}}}}],
        model=model,
        max_tokens=1024,
        temperature=0.2,
        reasoning_effort=reasoning_effort,
        tool_choice="auto",
    )


@pytest.mark.parametrize("api_base", [None, _MINIMAX])
@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-opus-4-7", "MiniMax-M2"])
@pytest.mark.parametrize("effort", [None, "none", "adaptive", "high"])
def test_every_request_field_is_one_the_sdk_accepts(api_base, model, effort) -> None:
    """The SDK's create/stream take keyword arguments only; a field the SDK
    dropped (temperature, from 1.0 on) raises TypeError at call time."""
    provider = AnthropicProvider(api_key="sk-test", api_base=api_base)
    accepted = set(inspect.signature(AsyncMessages.create).parameters) - {"self"}
    assert set(_build(provider, model, effort)) <= accepted


def test_third_party_keeps_the_configured_temperature() -> None:
    provider = AnthropicProvider(api_key="sk-test", api_base=_MINIMAX)
    kw = _build(provider, "MiniMax-M2", None)
    assert kw["extra_body"] == {"temperature": 0.2}


def test_third_party_thinking_samples_at_one() -> None:
    provider = AnthropicProvider(api_key="sk-test", api_base=_MINIMAX)
    kw = _build(provider, "MiniMax-M2", "adaptive")
    assert kw["extra_body"] == {"temperature": 1.0}


@pytest.mark.parametrize("api_base", [None, "https://api.anthropic.com"])
def test_anthropic_gets_no_sampling_params(api_base) -> None:
    provider = AnthropicProvider(api_key="sk-test", api_base=api_base)
    kw = _build(provider, "claude-sonnet-4-6", None)
    assert "temperature" not in kw
    assert "extra_body" not in kw


async def _send(provider: AnthropicProvider) -> httpx2.Request:
    """Run one real chat() through the SDK and return the request it sent."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    provider._client._client._transport = httpx2.MockTransport(handler)
    result = await provider.chat(
        messages=[{"role": "user", "content": "hi"}], model="claude-sonnet-4-6",
        max_tokens=16, temperature=0.2)
    assert result.content == "ok"
    return seen[0]


async def test_anthropic_wire_is_api_version_2026_10_21_with_bearer() -> None:
    request = await _send(AnthropicProvider(api_key="sk-test"))
    assert request.headers["anthropic-version"] == "2026-10-21"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert "x-api-key" not in request.headers
    assert "temperature" not in json.loads(request.content)


async def test_third_party_wire_is_unchanged_2023_06_01_with_x_api_key() -> None:
    request = await _send(AnthropicProvider(api_key="sk-test", api_base=_MINIMAX))
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["x-api-key"] == "sk-test"
    assert "authorization" not in request.headers
    assert json.loads(request.content)["temperature"] == 0.2
