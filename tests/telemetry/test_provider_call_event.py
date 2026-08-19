"""`provider.call` — the universal per-round-trip token/latency event.

Every LLM round-trip through the retry wrappers must land one
``provider.call`` event in the bound session telemetry, regardless of which
subsystem made the call. ``cache.usage`` only covers the channel agent loop;
this event is what makes workflow nodes, aux bridges, dream and judge
traffic visible, so "who spent the tokens?" is answerable from telemetry
alone.
"""

import asyncio
import json

from durin.providers.base import LLMProvider, LLMResponse
from durin.telemetry.logger import (
    TelemetryLogger,
    bind_telemetry,
    reset_telemetry,
)
from durin.telemetry.schema import EVENTS, ProviderCallEvent


class _StubProvider(LLMProvider):
    """Minimal concrete provider: one canned response, no network."""

    def __init__(self, response: LLMResponse) -> None:
        super().__init__(api_key="k", api_base="http://unit.test")
        self._response = response

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:  # noqa: ANN001
        return self._response

    def get_default_model(self) -> str:
        return "stub-model"


def _provider(usage=None, finish_reason="stop") -> _StubProvider:
    p = _StubProvider(LLMResponse(content="ok", finish_reason=finish_reason, usage=usage or {}))
    p.provider_key = "zai_coding_plan"
    return p


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_provider_call_is_in_the_events_catalog():
    assert EVENTS["provider.call"] is ProviderCallEvent


def test_chat_with_retry_emits_one_provider_call(tmp_path):
    p = _provider(usage={"prompt_tokens": 100, "cached_tokens": 40, "completion_tokens": 7})
    log_path = tmp_path / "t.jsonl"
    token = bind_telemetry(TelemetryLogger(log_path, session_key="s1"))
    try:
        resp = asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="glm-5.3"))
    finally:
        reset_telemetry(token)
    assert resp.content == "ok"
    events = _events(log_path)
    calls = [e for e in events if e["type"] == "provider.call"]
    assert len(calls) == 1
    data = calls[0]["data"]
    assert data["provider"] == "zai_coding_plan"
    assert data["model"] == "glm-5.3"
    assert data["prompt_tokens"] == 100
    assert data["cached_tokens"] == 40
    assert data["completion_tokens"] == 7
    assert data["finish_reason"] == "stop"
    assert data["duration_ms"] >= 0


def test_chat_stream_with_retry_emits_one_provider_call(tmp_path):
    p = _provider(usage={"prompt_tokens": 5, "completion_tokens": 2})
    log_path = tmp_path / "t.jsonl"
    token = bind_telemetry(TelemetryLogger(log_path, session_key="s1"))
    try:
        asyncio.run(p.chat_stream_with_retry(messages=[{"role": "user", "content": "hi"}], model="m"))
    finally:
        reset_telemetry(token)
    calls = [e for e in _events(log_path) if e["type"] == "provider.call"]
    assert len(calls) == 1
    assert calls[0]["data"]["prompt_tokens"] == 5
    assert calls[0]["data"]["cached_tokens"] == 0  # absent in usage → 0


def test_contextvar_bound_logger_wins_over_set_telemetry(tmp_path):
    p = _provider(usage={"prompt_tokens": 1})
    attr_path, bound_path = tmp_path / "attr.jsonl", tmp_path / "bound.jsonl"
    p.set_telemetry(TelemetryLogger(attr_path, session_key="attr"))
    token = bind_telemetry(TelemetryLogger(bound_path, session_key="bound"))
    try:
        asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "x"}]))
    finally:
        reset_telemetry(token)
    assert len(_events(bound_path)) == 1
    assert not attr_path.exists()


def test_set_telemetry_is_the_fallback_when_nothing_is_bound(tmp_path):
    p = _provider(usage={"prompt_tokens": 1})
    attr_path = tmp_path / "attr.jsonl"
    p.set_telemetry(TelemetryLogger(attr_path, session_key="attr"))
    asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "x"}]))
    calls = [e for e in _events(attr_path) if e["type"] == "provider.call"]
    assert len(calls) == 1


def test_no_sink_means_no_crash_and_no_event():
    p = _provider()
    resp = asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "x"}]))
    assert resp.content == "ok"


def test_a_raising_sink_never_breaks_the_call(tmp_path):
    class _Boom:
        session_key = "boom"

        def log(self, *_a, **_k):
            raise RuntimeError("sink down")

    p = _provider(usage={"prompt_tokens": 1})
    token = bind_telemetry(_Boom())
    try:
        resp = asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "x"}]))
    finally:
        reset_telemetry(token)
    assert resp.content == "ok"


def test_missing_usage_defaults_to_zero_tokens(tmp_path):
    p = _provider(usage=None)
    log_path = tmp_path / "t.jsonl"
    token = bind_telemetry(TelemetryLogger(log_path, session_key="s"))
    try:
        asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "x"}]))
    finally:
        reset_telemetry(token)
    data = _events(log_path)[0]["data"]
    assert data["prompt_tokens"] == 0
    assert data["completion_tokens"] == 0


def test_unstamped_provider_falls_back_to_class_name(tmp_path):
    p = _StubProvider(LLMResponse(content="ok", usage={}))  # no provider_key stamped
    log_path = tmp_path / "t.jsonl"
    token = bind_telemetry(TelemetryLogger(log_path, session_key="s"))
    try:
        asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "x"}]))
    finally:
        reset_telemetry(token)
    assert _events(log_path)[0]["data"]["provider"] == "_StubProvider"


def test_default_model_is_reported_when_model_arg_is_none(tmp_path):
    p = _provider(usage={"prompt_tokens": 1})
    log_path = tmp_path / "t.jsonl"
    token = bind_telemetry(TelemetryLogger(log_path, session_key="s"))
    try:
        asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "x"}]))
    finally:
        reset_telemetry(token)
    assert _events(log_path)[0]["data"]["model"] == "stub-model"
