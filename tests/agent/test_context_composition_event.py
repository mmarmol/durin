"""Tests for the ``context.composition`` telemetry event.

Verifies that ``ContextBuilder.build_messages`` emits a structured
breakdown of the rendered prompt's tokens per tier — the signal that
lets us measure how memory, history, hot layer, etc. consume the
context budget across turns.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from durin.agent.context import ContextBuilder


class _CapturingLogger:
    """In-memory TelemetryLogger drop-in that records every .log() call."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))


def _make_builder(tmp_path):
    """ContextBuilder with memory/skills stubbed for unit-test scope."""
    b = ContextBuilder(workspace=tmp_path)

    memory = MagicMock()
    memory.get_memory_context.return_value = ""
    memory.read_memory.return_value = ""
    memory.read_unprocessed_history.return_value = []
    memory.get_last_dream_cursor.return_value = None
    b.memory = memory

    skills = MagicMock()
    skills.get_always_skills.return_value = []
    skills.load_skills_for_context.return_value = ""
    skills.build_skills_summary.return_value = ""
    b.skills = skills
    return b


def _bind_telemetry(monkeypatch) -> list[tuple[str, dict]]:
    """Install a capturing logger and return the live event list."""
    logger = _CapturingLogger()
    monkeypatch.setattr(
        "durin.telemetry.logger.current_telemetry", lambda: logger
    )
    return logger.events


def test_composition_event_emits_on_build_messages(monkeypatch, tmp_path):
    """A turn with non-trivial content emits one composition event with
    a complete breakdown."""
    events = _bind_telemetry(monkeypatch)
    b = _make_builder(tmp_path)
    b.memory.get_memory_context.return_value = "User prefers terse responses."
    b.memory.read_memory.return_value = "User prefers terse responses."

    b.build_messages(
        history=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        current_message="what's the weather",
        session_key="abc",
        iteration=3,
    )

    composition = [e for e in events if e[0] == "context.composition"]
    assert len(composition) == 1, f"got events: {[e[0] for e in events]}"
    payload = composition[0][1]

    # Core fields present.
    assert "stable_tokens" in payload
    assert "stable_breakdown" in payload
    assert "context_tokens" in payload
    assert "volatile_tokens" in payload
    assert "volatile_breakdown" in payload
    assert "history_msg_tokens" in payload
    assert "current_msg_tokens" in payload
    assert "tools_tokens" in payload
    assert "estimated_total" in payload

    # Routing metadata.
    assert payload["session_key"] == "abc"
    assert payload["iteration"] == 3

    # Stable always has identity at minimum.
    assert "identity" in payload["stable_breakdown"]
    assert payload["stable_breakdown"]["identity"] > 0

    # History contributes; current message contributes.
    assert payload["history_msg_tokens"] > 0
    assert payload["current_msg_tokens"] > 0

    # Total is the sum of the parts (within rounding).
    expected = (
        payload["stable_tokens"]
        + payload["context_tokens"]
        + payload["volatile_tokens"]
        + payload["history_msg_tokens"]
        + payload["current_msg_tokens"]
        + payload["tools_tokens"]
    )
    assert payload["estimated_total"] == expected


def test_composition_event_skips_when_no_telemetry(monkeypatch, tmp_path):
    """No global telemetry bound → no event is emitted, build doesn't error."""
    monkeypatch.setattr(
        "durin.telemetry.logger.current_telemetry", lambda: None
    )
    b = _make_builder(tmp_path)
    # Should not raise.
    b.build_messages(history=[], current_message="hi")


def test_composition_event_counts_tools_tokens(monkeypatch, tmp_path):
    """When tool definitions are passed, their JSON is counted into
    tools_tokens (the part of the prompt the model actually sees but
    the conversation message list doesn't expose to estimate_message_tokens)."""
    events = _bind_telemetry(monkeypatch)
    b = _make_builder(tmp_path)

    fake_tool = {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Run arithmetic over two numbers and return the result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"},
                    "op": {"type": "string", "enum": ["+", "-", "*", "/"]},
                },
                "required": ["a", "b", "op"],
            },
        },
    }
    b.build_messages(
        history=[],
        current_message="hi",
        tools=[fake_tool],
    )

    payload = [e for e in events if e[0] == "context.composition"][0][1]
    assert payload["tools_tokens"] > 0


def test_composition_event_history_growth_is_observable(monkeypatch, tmp_path):
    """A turn with a longer history must report more history tokens —
    this is the property dashboards will plot."""
    events_a = _bind_telemetry(monkeypatch)
    b = _make_builder(tmp_path)

    b.build_messages(
        history=[{"role": "user", "content": "one short message"}],
        current_message="probe",
    )
    short_history_tokens = [
        e for e in events_a if e[0] == "context.composition"
    ][-1][1]["history_msg_tokens"]

    # Reset captured events and call again with a much bigger history.
    events_a.clear()
    long_msg = "x" * 5000
    b.build_messages(
        history=[
            {"role": "user", "content": long_msg},
            {"role": "assistant", "content": long_msg},
        ],
        current_message="probe",
    )
    long_history_tokens = [
        e for e in events_a if e[0] == "context.composition"
    ][-1][1]["history_msg_tokens"]

    assert long_history_tokens > short_history_tokens * 5
