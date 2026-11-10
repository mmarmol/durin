"""Retry events land in the same sink as the call's own ``provider.call`` row."""

import pytest

from durin.providers.base import LLMProvider, LLMResponse
from durin.telemetry.logger import bind_telemetry, reset_telemetry


class _Sink:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def log(self, event: str, data: dict) -> None:
        self.rows.append((event, data))

    def named(self, event: str) -> list[dict]:
        return [d for e, d in self.rows if e == event]


class _Scripted(LLMProvider):
    provider_key = "acme"

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "acme-1"


MSGS = [{"role": "user", "content": "hi"}]


def _limited(content: str = "429 rate limit", **kw) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="error", **kw)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("durin.providers.base.asyncio.sleep", _sleep)
    return slept


async def _call(provider, method="chat_with_retry", **kw):
    return await getattr(provider, method)(messages=MSGS, **kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat_with_retry", "chat_stream_with_retry"])
async def test_bound_sink_alone_gets_retry_row_then_call_row(method) -> None:
    sink = _Sink()
    provider = _Scripted([
        _limited(error_status_code=429, error_kind="rate_limit", error_code="too_fast"),
        LLMResponse(content="ok"),
    ])
    token = bind_telemetry(sink, purpose="dream")
    try:
        response = await _call(provider, method)
    finally:
        reset_telemetry(token)

    assert response.content == "ok"
    assert [e for e, _ in sink.rows] == ["provider.rate_limit", "provider.call"]
    row = sink.named("provider.rate_limit")[0]
    assert row["provider"] == "acme"
    assert row["model"] == "acme-1"
    assert row["purpose"] == "dream"
    assert row["attempt"] == 1
    assert row["delay_s"] == 1.0
    assert row["status_code"] == 429
    assert row["error_kind"] == "rate_limit"
    assert row["error_code"] == "too_fast"
    assert row["persistent"] is False
    assert row["error"] == "429 rate limit"


@pytest.mark.asyncio
async def test_attached_logger_alone_gets_the_same_rows() -> None:
    sink = _Sink()
    provider = _Scripted([_limited(), LLMResponse(content="ok")])
    provider.set_telemetry(sink)

    await _call(provider, model="explicit-model")

    assert [e for e, _ in sink.rows] == ["provider.rate_limit", "provider.call"]
    row = sink.named("provider.rate_limit")[0]
    assert row["model"] == "explicit-model"
    assert row["purpose"] == "unknown"


@pytest.mark.asyncio
async def test_bound_sink_wins_and_nothing_is_duplicated() -> None:
    bound, attached = _Sink(), _Sink()
    provider = _Scripted([_limited(), LLMResponse(content="ok")])
    provider.set_telemetry(attached)
    token = bind_telemetry(bound)
    try:
        await _call(provider)
    finally:
        reset_telemetry(token)

    assert [e for e, _ in bound.rows] == ["provider.rate_limit", "provider.call"]
    assert attached.rows == []


@pytest.mark.asyncio
async def test_exhausted_retries_emit_one_exhausted_row() -> None:
    sink = _Sink()
    provider = _Scripted([_limited(f"429 rate limit {i}") for i in range(7)])
    token = bind_telemetry(sink, purpose="chat")
    try:
        response = await _call(provider)
    finally:
        reset_telemetry(token)

    assert response.finish_reason == "error"
    assert len(sink.named("provider.rate_limit")) == 6
    exhausted = sink.named("provider.rate_limit_exhausted")
    assert len(exhausted) == 1
    assert exhausted[0]["attempts"] == 7
    assert exhausted[0]["provider"] == "acme"
    assert exhausted[0]["model"] == "acme-1"
    assert exhausted[0]["purpose"] == "chat"


@pytest.mark.asyncio
async def test_persistent_give_up_on_identical_errors_emits_one_exhausted_row() -> None:
    sink = _Sink()
    limit = LLMProvider._PERSISTENT_IDENTICAL_ERROR_LIMIT
    provider = _Scripted([_limited("429 rate limit") for _ in range(limit)])
    token = bind_telemetry(sink)
    try:
        await _call(provider, retry_mode="persistent")
    finally:
        reset_telemetry(token)

    assert len(sink.named("provider.rate_limit")) == limit - 1
    assert len(sink.named("provider.rate_limit_exhausted")) == 1


@pytest.mark.asyncio
async def test_error_that_is_not_retried_emits_no_retry_row() -> None:
    sink = _Sink()
    provider = _Scripted([LLMResponse(content="401 unauthorized", finish_reason="error")])
    token = bind_telemetry(sink)
    try:
        await _call(provider)
    finally:
        reset_telemetry(token)

    assert [e for e, _ in sink.rows] == ["provider.call"]


@pytest.mark.asyncio
async def test_no_sink_leaves_the_outcome_unchanged() -> None:
    provider = _Scripted([_limited(), LLMResponse(content="ok")])

    response = await _call(provider)

    assert response.content == "ok"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_a_failing_sink_never_breaks_the_call() -> None:
    class _Broken:
        def log(self, event, data):
            raise RuntimeError("disk full")

    provider = _Scripted([_limited(), LLMResponse(content="ok")])
    provider.set_telemetry(_Broken())

    response = await _call(provider)

    assert response.content == "ok"
