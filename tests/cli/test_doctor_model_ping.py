"""The `--ping-model` round-trip (also the dashboard's "Probar" button) judges
whether the provider talked back — not whether it talked back in prose.

Reasoning models spend the ping's tiny output budget on reasoning tokens and
return no content at all, which is a successful round-trip reported as a
failure. Errors travel as an ``LLMResponse`` with ``finish_reason="error"``
and a human-readable message in ``content``, which is a failure that used to
be reported as success."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.cli.doctor import check_model_ping_async
from durin.config.schema import Config
from durin.providers.base import LLMResponse, ToolCallRequest


def _cfg() -> Config:
    c = Config()
    c.agents.defaults.provider = "openrouter"
    c.agents.defaults.model = "deepseek/deepseek-v4-pro"
    c.providers.openrouter.api_key = "k-openrouter"
    return c


class _Provider:
    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    async def chat(self, **_kwargs) -> LLMResponse:
        return self._response


async def _run(response: LLMResponse, cfg: Config | None = None):
    with patch("durin.providers.factory.make_provider", return_value=_Provider(response)):
        return await check_model_ping_async(cfg=cfg or _cfg())


@pytest.mark.asyncio
async def test_ok_when_reasoning_model_spends_the_budget_thinking():
    # Live shape from OpenRouter + deepseek/deepseek-v4-pro at max_tokens=4:
    # every output token went to reasoning, so no content was ever emitted.
    r = await _run(LLMResponse(content=None, finish_reason="length", reasoning_content="Th"))
    assert r.status == "ok"
    assert "deepseek/deepseek-v4-pro" in r.message


@pytest.mark.asyncio
async def test_ok_when_content_comes_back():
    r = await _run(LLMResponse(content="pong", finish_reason="stop"))
    assert r.status == "ok"


@pytest.mark.asyncio
async def test_ok_when_only_tool_calls_come_back():
    call = ToolCallRequest(id="1", name="noop", arguments={})
    r = await _run(LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[call]))
    assert r.status == "ok"


@pytest.mark.asyncio
async def test_fails_when_the_model_produced_nothing_at_all():
    r = await _run(LLMResponse(content=None, finish_reason="stop"))
    assert r.status == "fail"
    assert r.message == "empty response"


@pytest.mark.asyncio
async def test_fails_when_the_provider_reports_an_error_as_content():
    # Providers do not raise: they return the error as an ordinary response.
    r = await _run(LLMResponse(
        content="Error calling LLM: stream stalled for more than 60 seconds",
        finish_reason="error",
    ))
    assert r.status == "fail"
    assert "stream stalled" in r.message
