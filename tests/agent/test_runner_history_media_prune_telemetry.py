"""Runner integration: ``history_media.pruned`` telemetry.

The runner's sanitize pipeline calls ``prune_processed_history_images``
on each iteration. When that pruner actually removes media blocks, the
runner emits a structured event so we can measure prune frequency and
volume in production.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    from durin.agent import runner as runner_mod
    monkeypatch.setattr(runner_mod, "current_telemetry", lambda: sink)


def _image_block() -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVB..."}}


def _build_initial_messages_with_prunable_media() -> list[dict]:
    """4 completed turns with images in user messages → with preserve=3,
    the oldest turn's image is prune-eligible."""
    messages = []
    for i in range(4):
        messages.append({"role": "user", "content": [_image_block(), {"type": "text", "text": f"u{i}"}]})
        messages.append({"role": "assistant", "content": f"a{i}"})
    return messages


@pytest.mark.asyncio
async def test_runner_emits_telemetry_when_prune_removes_media(monkeypatch):
    from durin.agent.runner import AgentRunner, AgentRunSpec

    sink = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, sink)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[], usage={}))
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    initial = _build_initial_messages_with_prunable_media() + [
        {"role": "user", "content": "follow up"},
    ]
    result = await runner.run(AgentRunSpec(
        initial_messages=initial,
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        session_key="sess-prune",
    ))

    assert result.stop_reason == "completed"

    events = [e for e in sink.events if e[0] == "history_media.pruned"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["image_blocks_removed"] >= 1
    assert payload["audio_blocks_removed"] == 0
    assert payload["preserve_turns"] == 3
    assert payload["iteration"] == 0
    assert payload["session_key"] == "sess-prune"


@pytest.mark.asyncio
async def test_runner_emits_no_telemetry_when_nothing_pruned(monkeypatch):
    """A short conversation with no pruneable history → no event.
    Confirms we don't spam the channel with zero-count noise."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    sink = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, sink)

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[], usage={}))
    tools = MagicMock()
    tools.get_definitions.return_value = []

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        session_key="sess-short",
    ))

    assert result.stop_reason == "completed"
    assert [e for e in sink.events if e[0] == "history_media.pruned"] == []
