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


def test_prefetch_block_is_billed_as_its_own_line(monkeypatch, tmp_path):
    """The automatic prefetch's block rides in the user message, but it is not
    the user's message: it gets its own breakdown line, and the current-message
    count is what the person actually wrote."""
    from durin.agent.context import build_memory_context_block

    events = _bind_telemetry(monkeypatch)
    b = _make_builder(tmp_path)
    block = build_memory_context_block(
        "=== CANONICAL: person:ana (canonical entity page) ===\n"
        + "Ana runs the bakery on Main St. " * 40
        + "\n=== END CANONICAL ==="
    )

    b.build_messages(history=[], current_message="what's the weather")
    plain = [e for e in events if e[0] == "context.composition"][-1][1]

    b.build_messages(history=[], current_message="what's the weather", memory_prefetch=block)
    payload = [e for e in events if e[0] == "context.composition"][-1][1]

    prefetch_tokens = payload["volatile_breakdown"]["memory_prefetch"]
    assert prefetch_tokens > 100                       # the block really is big
    assert "memory_prefetch" not in plain["volatile_breakdown"]
    # The user's message costs the same with or without the block.
    assert abs(payload["current_msg_tokens"] - plain["current_msg_tokens"]) <= 2
    assert payload["volatile_tokens"] == sum(payload["volatile_breakdown"].values())
    assert payload["estimated_total"] == (
        payload["stable_tokens"]
        + payload["context_tokens"]
        + payload["volatile_tokens"]
        + payload["history_msg_tokens"]
        + payload["current_msg_tokens"]
        + payload["tools_tokens"]
    )
    # And it reaches the operator surfaces as its own row.
    from durin.agent.context import summarize_composition

    assert summarize_composition(payload)["conversation_breakdown"]["Memory prefetch"] == prefetch_tokens


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


class _SessionLoggerRecorder:
    """Stand-in for ``get_session_logger``: one recorder for every key."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, session_key, base_dir=None):
        return self

    def log(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))


def test_turn_build_emits_through_the_session_logger_without_a_bound_contextvar(
    monkeypatch, tmp_path,
):
    """The real turn's first build runs before the loop binds the per-run
    telemetry ContextVar, so the row has to reach the session logger by key —
    otherwise the turn that carries the prefetch never reports its composition."""
    from durin.agent.context import build_memory_context_block
    from durin.telemetry.logger import current_telemetry

    assert current_telemetry() is None
    rec = _SessionLoggerRecorder()
    monkeypatch.setattr("durin.telemetry.logger.get_session_logger", rec)

    b = _make_builder(tmp_path)
    block = build_memory_context_block(
        "=== CANONICAL: person:ana (canonical entity page) ===\n"
        + "Ana runs the bakery on Main St. " * 40
        + "\n=== END CANONICAL ==="
    )

    b.build_messages(
        history=[{"role": "user", "content": "hi"}],
        current_message="what does Ana do",
        session_key="websocket:abc",
        iteration=0,
        memory_prefetch=block,
    )

    composition = [e for e in rec.events if e[0] == "context.composition"]
    assert len(composition) == 1, f"got events: {[e[0] for e in rec.events]}"
    payload = composition[0][1]
    assert payload["session_key"] == "websocket:abc"
    assert payload["iteration"] == 0
    assert payload["volatile_breakdown"]["memory_prefetch"] > 0
    assert b.last_composition == payload


def test_no_session_key_and_no_telemetry_emits_nothing(monkeypatch, tmp_path):
    """Without a bound logger and without a session key there is nowhere to
    route the row; the build must not invent a destination."""
    rec = _SessionLoggerRecorder()
    monkeypatch.setattr("durin.telemetry.logger.get_session_logger", rec)
    monkeypatch.setattr("durin.telemetry.logger.current_telemetry", lambda: None)

    b = _make_builder(tmp_path)
    b.build_messages(history=[], current_message="hi")

    assert rec.events == []
    assert b.last_composition is None


def test_compaction_probe_emits_nothing_and_leaves_last_composition(monkeypatch, tmp_path):
    """The consolidator's token probe builds a throwaway prompt. It must not
    emit a composition row nor overwrite the cached payload that /status and
    the CLI footer read — those describe the real turn."""
    from unittest.mock import AsyncMock, MagicMock

    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus
    from durin.providers.base import LLMResponse

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
    )
    loop.provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok", tool_calls=[]))
    loop.tools.get_definitions = MagicMock(return_value=[])

    session = loop.sessions.get_or_create("cli:probe")
    for i in range(3):
        session.add_message("user", f"question {i}")
        session.add_message("assistant", f"answer {i}")
    loop.sessions.save(session)

    sentinel = {"estimated_total": 1234, "session_key": "cli:probe"}
    loop.context.last_composition = sentinel

    # Consolidation runs with the session's telemetry bound — that binding is
    # what let the probe's rows reach the log in the first place.
    events = _bind_telemetry(monkeypatch)
    rec = _SessionLoggerRecorder()
    monkeypatch.setattr("durin.telemetry.logger.get_session_logger", rec)

    tokens, _source = loop.consolidator.estimate_session_prompt_tokens(session)

    assert tokens > 0
    assert [e for e in events if e[0] == "context.composition"] == []
    assert [e for e in rec.events if e[0] == "context.composition"] == []
    assert loop.context.last_composition is sentinel
