"""An account or plan state no retry can change is answered once, not walked down
the backoff ladder: an exhausted usage window, an expired plan, a plan or key
that does not cover the request.

Every test drives the real OpenAI-compatible adapter: the SDK receives an HTTP
response from a mock transport, raises its own status error, the adapter turns
that into an ``LLMResponse``, and the retry loop classifies it. Attempts are
counted on the wire.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from durin.providers.openai_compat_provider import OpenAICompatProvider
from durin.providers.registry import find_by_name

_RESET_AT = "2031-03-04 05:06:07"
_WINDOW_MESSAGE = f"Usage limit reached for 5 hour. Your limit will reset at {_RESET_AT}"
_MESSAGES = [{"role": "user", "content": "hello"}]
_IMAGE_MESSAGES = [{"role": "user", "content": [
    {"type": "text", "text": "what is in this picture?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
]}]


def _error(code: str, message: str, headers: dict[str, str] | None = None) -> httpx2.Response:
    """The endpoint's error wire shape: HTTP 429 with a business code in the body."""
    return httpx2.Response(
        429, json={"error": {"code": code, "message": message}}, headers=headers,
    )


def _completion(text: str) -> httpx2.Response:
    """A minimal successful streamed completion."""
    def chunk(delta: dict, finish_reason: str | None) -> str:
        return "data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }) + "\n\n"

    body = (
        chunk({"role": "assistant", "content": text}, None)
        + chunk({}, "stop")
        + "data: [DONE]\n\n"
    )
    return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})


class _Wire:
    """Serves scripted HTTP responses in order (the last one repeats) and counts requests."""

    def __init__(self, provider: OpenAICompatProvider, responses: list[httpx2.Response]):
        self._responses = responses
        self.requests = 0
        provider._client._client._transport = httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        index = min(self.requests, len(self._responses) - 1)
        self.requests += 1
        return self._responses[index]


@pytest.fixture
def provider(monkeypatch) -> OpenAICompatProvider:
    spec = find_by_name("zai_coding_plan")
    monkeypatch.setenv(spec.env_key, "sk-test")
    return OpenAICompatProvider(api_key="sk-test", default_model="test-model", spec=spec)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    recorded: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr("durin.providers.base.asyncio.sleep", _fake_sleep)
    return recorded


async def _call(provider: OpenAICompatProvider, entry_point: str, messages=_MESSAGES):
    return await getattr(provider, entry_point)(messages=list(messages))


_ENTRY_POINTS = ("chat_with_retry", "chat_stream_with_retry")


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_exhausted_usage_window_is_attempted_once_without_backoff(
    provider, sleeps, entry_point,
) -> None:
    wire = _Wire(provider, [_error("1308", _WINDOW_MESSAGE)])

    response = await _call(provider, entry_point)

    assert response.finish_reason == "error"
    assert wire.requests == 1
    assert sleeps == []


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_exhausted_usage_window_error_keeps_the_reset_time(
    provider, sleeps, entry_point,
) -> None:
    _Wire(provider, [_error("1308", _WINDOW_MESSAGE)])

    response = await _call(provider, entry_point)

    assert _WINDOW_MESSAGE in response.content
    assert _RESET_AT in response.content


async def test_exhausted_usage_window_is_not_retried_when_the_endpoint_asks_for_a_retry(
    provider, sleeps,
) -> None:
    wire = _Wire(provider, [_error("1308", _WINDOW_MESSAGE, {"x-should-retry": "true"})])

    response = await _call(provider, "chat_with_retry")

    assert response.finish_reason == "error"
    assert wire.requests == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("1310", f"Weekly/Monthly Limit Exhausted. Your limit will reset at {_RESET_AT}"),
        (
            "1321",
            "Usage limit reached for the past 7 days. Extra usage is not available due "
            f"to monthly spend limit. Resets at {_RESET_AT}.",
        ),
    ],
)
async def test_other_exhausted_windows_are_attempted_once(
    provider, sleeps, code, message,
) -> None:
    wire = _Wire(provider, [_error(code, message)])

    response = await _call(provider, "chat_with_retry")

    assert wire.requests == 1
    assert sleeps == []
    assert _RESET_AT in response.content


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_exhausted_usage_window_is_attempted_once_on_a_conversation_with_images(
    provider, sleeps, entry_point,
) -> None:
    wire = _Wire(provider, [_error("1308", _WINDOW_MESSAGE)])

    response = await _call(provider, entry_point, _IMAGE_MESSAGES)

    assert response.finish_reason == "error"
    assert _RESET_AT in response.content
    assert wire.requests == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (
            "1309",
            "Your GLM Coding Plan package has expired and is temporarily unavailable. "
            "You can resume using it after renewing the subscription on the official "
            "website.",
        ),
        ("1311", "Your current subscription plan does not yet include access to test-model"),
        ("1314", "Your enterprise package has expired. Please contact your enterprise administrator."),
        (
            "1315",
            "This API Key is limited to enterprise coding package scenarios. Please go "
            "to the official website to replace the API Key of the corresponding "
            "product type.",
        ),
    ],
)
@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_plan_or_key_that_does_not_cover_the_request_is_attempted_once(
    provider, sleeps, entry_point, code, message,
) -> None:
    wire = _Wire(provider, [_error(code, message)])

    response = await _call(provider, entry_point)

    assert response.finish_reason == "error"
    assert message in response.content
    assert wire.requests == 1
    assert sleeps == []


async def test_expired_plan_is_attempted_once_on_a_conversation_with_images(
    provider, sleeps,
) -> None:
    wire = _Wire(provider, [
        _error("1314", "Your enterprise package has expired. Please contact your enterprise administrator."),
    ])

    await _call(provider, "chat_with_retry", _IMAGE_MESSAGES)

    assert wire.requests == 1


async def test_fair_usage_frequency_limit_is_still_retried(provider, sleeps) -> None:
    wire = _Wire(provider, [
        _error(
            "1313",
            "Your account's current usage pattern does not comply with the Fair Usage "
            "Policy, and your request frequency has been limited.",
        ),
        _completion("ok"),
    ])

    response = await _call(provider, "chat_with_retry")

    assert response.content == "ok"
    assert wire.requests == 2
    assert sleeps == [1]


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_request_rate_limit_is_still_retried_on_the_default_schedule(
    provider, sleeps, entry_point,
) -> None:
    wire = _Wire(provider, [_error("1302", "Rate limit reached for requests")])

    response = await _call(provider, entry_point)

    assert response.finish_reason == "error"
    assert sleeps == [1, 2, 4, 8, 15, 30]
    assert wire.requests == 7


async def test_request_rate_limit_recovers_after_one_retry(provider, sleeps) -> None:
    wire = _Wire(provider, [
        _error("1302", "Rate limit reached for requests"),
        _completion("ok"),
    ])

    response = await _call(provider, "chat_with_retry")

    assert response.content == "ok"
    assert wire.requests == 2
    assert sleeps == [1]


@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
async def test_overload_is_still_retried_on_the_overload_schedule(
    provider, sleeps, entry_point,
) -> None:
    wire = _Wire(provider, [
        _error("1305", "The service may be temporarily overloaded, please try again later"),
        _completion("ok"),
    ])

    response = await _call(provider, entry_point)

    assert response.content == "ok"
    assert wire.requests == 2
    assert sleeps == [30]
