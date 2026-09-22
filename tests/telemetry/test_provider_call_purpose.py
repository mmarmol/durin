"""``provider.call`` says what the call was for.

The row already said which provider and model spent the tokens; it did not
say on whose behalf — a chat turn, a cron run, the compaction summarizer, a
subagent, the dream, the judge, a workflow node. Splitting a session's spend
by caller meant bracketing rows between other events by timestamp. The
purpose is a ContextVar bound next to the telemetry logger: a subsystem that
binds one names itself, a binding without one inherits the enclosing purpose
(the agent loop under a cron run stays ``cron``), and a call site that knows
better passes it explicitly.
"""

from __future__ import annotations

import asyncio
import json

from durin.providers.base import LLMProvider, LLMResponse
from durin.telemetry.logger import (
    TelemetryLogger,
    bind_telemetry,
    current_call_purpose,
    reset_telemetry,
)
from durin.telemetry.schema import ProviderCallEvent


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="k", api_base="http://unit.test")

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:  # noqa: ANN001
        return LLMResponse(content="ok", finish_reason="stop", usage={"prompt_tokens": 3})

    def get_default_model(self) -> str:
        return "stub-model"


def _purposes(path) -> list[str]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r["data"]["purpose"] for r in rows if r["type"] == "provider.call"]


async def _call(p: _StubProvider) -> None:
    await p.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="m")


def test_purpose_is_part_of_the_schema() -> None:
    assert "purpose" in ProviderCallEvent.__annotations__


def test_a_call_with_no_purpose_bound_is_recorded_as_unknown(tmp_path) -> None:
    p = _StubProvider()
    log = TelemetryLogger(tmp_path / "t.jsonl", session_key="s")
    token = bind_telemetry(log)
    try:
        asyncio.run(_call(p))
    finally:
        reset_telemetry(token)
    assert _purposes(log.path) == ["unknown"]


def test_the_bound_purpose_tags_the_call_and_reset_restores_it(tmp_path) -> None:
    p = _StubProvider()
    log = TelemetryLogger(tmp_path / "t.jsonl", session_key="s")
    token = bind_telemetry(log, purpose="compaction")
    try:
        assert current_call_purpose() == "compaction"
        asyncio.run(_call(p))
    finally:
        reset_telemetry(token)
    assert current_call_purpose() is None
    assert _purposes(log.path) == ["compaction"]


def test_a_binding_without_a_purpose_inherits_the_enclosing_one(tmp_path) -> None:
    """The cron handler binds ``cron`` around the turn; the agent loop's own
    per-turn binding must not relabel the turn's calls as chat."""
    p = _StubProvider()
    outer_log = TelemetryLogger(tmp_path / "outer.jsonl", session_key="cron:job")
    inner_log = TelemetryLogger(tmp_path / "inner.jsonl", session_key="cron:job")
    outer = bind_telemetry(outer_log, purpose="cron")
    try:
        inner = bind_telemetry(inner_log)
        try:
            asyncio.run(_call(p))
        finally:
            reset_telemetry(inner)
        assert current_call_purpose() == "cron"
    finally:
        reset_telemetry(outer)
    assert _purposes(inner_log.path) == ["cron"]


def test_an_inner_binding_with_its_own_purpose_overrides_and_restores(tmp_path) -> None:
    """The consolidator runs inside a chat turn and must not be billed as chat."""
    p = _StubProvider()
    log = TelemetryLogger(tmp_path / "t.jsonl", session_key="s")
    outer = bind_telemetry(log, purpose="chat")
    try:
        inner = bind_telemetry(log, purpose="compaction")
        try:
            asyncio.run(_call(p))
        finally:
            reset_telemetry(inner)
        asyncio.run(_call(p))
    finally:
        reset_telemetry(outer)
    assert _purposes(log.path) == ["compaction", "chat"]


def test_an_explicit_purpose_on_the_emit_wins(tmp_path) -> None:
    """The vision/audio bridges emit their own row from inside a chat turn."""
    p = _StubProvider()
    log = TelemetryLogger(tmp_path / "t.jsonl", session_key="s")
    token = bind_telemetry(log, purpose="chat")
    try:
        p.emit_call_telemetry(
            model="m", response=LLMResponse(content="", finish_reason="stop"),
            duration_ms=1.0, purpose="vision",
        )
    finally:
        reset_telemetry(token)
    assert _purposes(log.path) == ["vision"]
