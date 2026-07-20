"""Retry-path hardening: overload backoff + reactive request recovery.

- Overload responses (Z.AI Coding Plan GLM returns HTTP 429 code 1305) walk a
  wider backoff schedule instead of hammering the still-hot endpoint every 1s.
- A non-transient error runs through ``_recover_request_for_error``: a provider
  can strip the offending piece of the request and let the loop retry once —
  the self-healing net that replaces per-model allowlists.
"""

import pytest

from durin.providers.base import LLMProvider, LLMResponse


class ScriptedProvider(LLMProvider):
    """Returns a pre-scripted sequence of responses, recording call count/kwargs."""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0
        self.last_kwargs: dict = {}

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        self.last_kwargs = kwargs
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def get_default_model(self) -> str:
        return "test-model"


def _record_sleep(delays: list[float]):
    async def _fake_sleep(delay: float) -> None:
        delays.append(delay)
    return _fake_sleep


async def _instant_sleep(delay: float) -> None:
    return None


# ── Z.AI overload (HTTP 429 code 1305) adaptive backoff ──────────────────


@pytest.mark.asyncio
async def test_overload_1305_uses_long_backoff(monkeypatch) -> None:
    provider = ScriptedProvider([
        LLMResponse(
            content="service is temporarily overloaded",
            finish_reason="error",
            error_status_code=429,
            error_code="1305",
        ),
        LLMResponse(content="ok"),
    ])
    delays: list[float] = []
    monkeypatch.setattr("durin.providers.base.asyncio.sleep", _record_sleep(delays))

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    assert delays == [30], "overload must use the long schedule, not the default 1s"


@pytest.mark.asyncio
async def test_ordinary_429_keeps_default_backoff(monkeypatch) -> None:
    provider = ScriptedProvider([
        LLMResponse(content="429 rate limit", finish_reason="error", error_status_code=429),
        LLMResponse(content="ok"),
    ])
    delays: list[float] = []
    monkeypatch.setattr("durin.providers.base.asyncio.sleep", _record_sleep(delays))

    await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert delays == [1], "a plain 429 must keep the default fast retry"


# ── reactive request recovery hook ───────────────────────────────────────


class _RecoveringProvider(ScriptedProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.recover_calls = 0

    def _recover_request_for_error(self, kw, response):
        self.recover_calls += 1
        if self.recover_calls > 1:  # one-shot
            return None
        return {**kw, "messages": [{"role": "user", "content": "recovered"}]}


@pytest.mark.asyncio
async def test_non_transient_error_recovers_and_retries_once(monkeypatch) -> None:
    provider = _RecoveringProvider([
        LLMResponse(content="400 unsupported request shape", finish_reason="error",
                    error_status_code=400),
        LLMResponse(content="ok"),
    ])
    monkeypatch.setattr("durin.providers.base.asyncio.sleep", _instant_sleep)

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    assert provider.calls == 2
    assert provider.last_kwargs.get("messages") == [{"role": "user", "content": "recovered"}]


@pytest.mark.asyncio
async def test_base_recovery_hook_is_noop_by_default(monkeypatch) -> None:
    provider = ScriptedProvider([
        LLMResponse(content="400 permanent bad request", finish_reason="error",
                    error_status_code=400),
    ])
    monkeypatch.setattr("durin.providers.base.asyncio.sleep", _instant_sleep)

    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "400 permanent bad request"
    assert provider.calls == 1
