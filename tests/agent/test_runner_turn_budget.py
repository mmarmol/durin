"""Per-turn aggregate tool-result budget (Hermes-inspired Tier 1).

``max_tool_result_chars`` already caps each tool result individually, and
oversized single results spill to disk via ``maybe_persist_tool_result``.
But when an LLM emits N parallel tool calls each returning < per-tool cap,
the aggregate can still overflow the context window. The runner now sums
the sizes of tool results in a single turn and, if the total exceeds
``DURIN_TURN_BUDGET_CHARS`` (default 200 KB), spills the largest
not-yet-persisted ones to disk in priority order until under budget.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.config.schema import AgentDefaults
from durin.providers.base import LLMResponse, ToolCallRequest

_MAX_TOOL_RESULT_CHARS = AgentDefaults().max_tool_result_chars


class _RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _bind_telemetry(monkeypatch, sink: _RecordingTelemetry) -> None:
    from durin.agent import runner as runner_mod
    monkeypatch.setattr(runner_mod, "current_telemetry", lambda: sink)


def _make_spec_with_persistence(tmp_path):
    """Build a spec with a workspace so spillover actually writes a file."""
    from durin.agent.runner import AgentRunSpec
    return AgentRunSpec(
        initial_messages=[],
        tools=MagicMock(),
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=_MAX_TOOL_RESULT_CHARS,
        workspace=tmp_path,
        session_key="sess-budget",
    )


def test_under_budget_is_noop(tmp_path, monkeypatch):
    """Aggregate below budget → no spillover, no telemetry."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "100000")
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    runner = AgentRunner(provider=MagicMock())
    spec = _make_spec_with_persistence(tmp_path)
    msgs = [
        {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "x" * 10_000},
        {"role": "tool", "tool_call_id": "c2", "name": "t", "content": "x" * 20_000},
    ]
    before = [m["content"] for m in msgs]
    runner._enforce_turn_budget(spec, msgs)
    assert [m["content"] for m in msgs] == before

    events = [e for e in telemetry.events if e[0] == "turn_budget.enforced"]
    assert events == []


def test_over_budget_spills_largest_first(tmp_path, monkeypatch):
    """Three tool results totalling ~310 KB against a 100 KB budget.
    The largest (200 KB) should be spilled first; if that alone brings
    the aggregate under budget, no further results are touched."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "100000")
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    runner = AgentRunner(provider=MagicMock())
    spec = _make_spec_with_persistence(tmp_path)
    msgs = [
        {"role": "tool", "tool_call_id": "small", "name": "t", "content": "s" * 10_000},
        {"role": "tool", "tool_call_id": "huge",  "name": "t", "content": "H" * 200_000},
        {"role": "tool", "tool_call_id": "mid",   "name": "t", "content": "m" * 100_000},
    ]
    runner._enforce_turn_budget(spec, msgs)

    # Largest was the 200K "huge" — it must have been replaced with the
    # persistence-reference string (short). The 100K "mid" should follow
    # if still over budget; the 10K "small" should stay raw.
    huge_content = msgs[1]["content"]
    assert isinstance(huge_content, str)
    assert "[tool output persisted]" in huge_content
    assert msgs[0]["content"] == "s" * 10_000  # untouched

    events = [e for e in telemetry.events if e[0] == "turn_budget.enforced"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["budget_chars"] == 100_000
    assert payload["spilled_count"] >= 1
    assert payload["after_chars"] < payload["before_chars"]


def test_already_persisted_results_are_skipped(tmp_path, monkeypatch):
    """A result that already carries the persisted-output marker (because
    ``_normalize_tool_result`` spilled it for the per-tool cap) must NOT
    be picked again — re-persisting it would be wasted work."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "100000")
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    runner = AgentRunner(provider=MagicMock())
    spec = _make_spec_with_persistence(tmp_path)
    already_persisted = (
        "[tool output persisted]\nFull output saved to: /tmp/fake.txt\n"
        + "preview" * 50  # still small but contains the marker
    )
    msgs = [
        {"role": "tool", "tool_call_id": "huge", "name": "t", "content": "X" * 200_000},
        {"role": "tool", "tool_call_id": "old",  "name": "t", "content": already_persisted},
    ]
    runner._enforce_turn_budget(spec, msgs)

    # The already-persisted message should not be picked for spillover —
    # the huge one is the only valid candidate.
    assert msgs[1]["content"] == already_persisted


def test_budget_zero_disables_enforcement(tmp_path, monkeypatch):
    """``DURIN_TURN_BUDGET_CHARS=0`` is the kill switch."""
    from durin.agent.runner import AgentRunner

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "0")
    telemetry = _RecordingTelemetry()
    _bind_telemetry(monkeypatch, telemetry)

    runner = AgentRunner(provider=MagicMock())
    spec = _make_spec_with_persistence(tmp_path)
    msgs = [
        {"role": "tool", "tool_call_id": "huge", "name": "t", "content": "X" * 1_000_000},
    ]
    runner._enforce_turn_budget(spec, msgs)
    assert msgs[0]["content"] == "X" * 1_000_000

    events = [e for e in telemetry.events if e[0] == "turn_budget.enforced"]
    assert events == []


def test_budget_reader_default_and_override(monkeypatch):
    from durin.agent.runner import _turn_budget_chars

    monkeypatch.delenv("DURIN_TURN_BUDGET_CHARS", raising=False)
    assert _turn_budget_chars() == 200_000

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "50000")
    assert _turn_budget_chars() == 50_000

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "not-a-number")
    assert _turn_budget_chars() == 200_000

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "-5")
    assert _turn_budget_chars() == 0


@pytest.mark.asyncio
async def test_runner_integration_spills_in_aggregate_overflow(tmp_path, monkeypatch):
    """End-to-end: 4 tool calls each returning 60K → 240K aggregate.
    Per-tool cap (16K default) would spill each individually anyway in this
    case, so we raise it. With turn budget 100K, the largest still ends up
    spilled to disk so the aggregate fits."""
    from durin.agent.runner import AgentRunner, AgentRunSpec

    monkeypatch.setenv("DURIN_TURN_BUDGET_CHARS", "100000")

    provider = MagicMock()
    call_count = {"n": 0}

    tool_calls = [
        ToolCallRequest(id=f"call_{i}", name="big_tool", arguments={"i": i})
        for i in range(4)
    ]

    async def chat_with_retry(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=tool_calls,
                finish_reason="tool_calls",
                usage={},
            )
        return LLMResponse(content="done", tool_calls=[], usage={})

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    # Each tool returns 60K characters.
    tools.execute = AsyncMock(side_effect=[
        "A" * 60_000, "B" * 60_000, "C" * 60_000, "D" * 60_000,
    ])

    runner = AgentRunner(provider)
    result = await runner.run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "go"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        # High per-tool cap so individual results don't spill on their own —
        # forces the per-turn budget to be the active limit.
        max_tool_result_chars=500_000,
        workspace=tmp_path,
    ))

    assert result.final_content == "done"
    tool_messages = [m for m in result.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 4
    # At least one tool result was spilled (large ones replaced with a
    # persistence reference).
    persisted_count = sum(
        1 for m in tool_messages if isinstance(m["content"], str) and "[tool output persisted]" in m["content"]
    )
    assert persisted_count >= 1
