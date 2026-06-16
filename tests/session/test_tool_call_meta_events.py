"""Tests for ``tool_call`` events in the session meta timeline.

Verifies the new helpers in ``session_meta.py`` plus the loop integration
that writes one event per assistant-emitted tool call with ``msg_index``
pointing back to the assistant message in ``session.messages``.
"""

from __future__ import annotations

import pytest

from durin.session.session_meta import (
    append_events_batch,
    make_tool_call_event,
    meta_path_for,
    read_meta,
)


def test_make_tool_call_event_has_required_fields():
    ev = make_tool_call_event(
        tool_call_id="call_abc",
        name="read_file",
        outcome="ok",
        msg_index=5,
        duration_ms=12.34,
    )
    assert ev["type"] == "tool_call"
    assert ev["id"] == "call_abc"
    assert ev["name"] == "read_file"
    assert ev["outcome"] == "ok"
    assert ev["msg_index"] == 5
    assert ev["duration_ms"] == 12.3  # rounded to 1 decimal
    assert "error" not in ev


def test_make_tool_call_event_includes_error_when_present():
    ev = make_tool_call_event(
        tool_call_id="call_x",
        name="grep",
        outcome="error",
        msg_index=2,
        duration_ms=0.0,
        error="pattern syntax error: unbalanced bracket",
    )
    assert ev["outcome"] == "error"
    assert "pattern syntax error" in ev["error"]


def test_make_tool_call_event_truncates_long_error():
    long_err = "x" * 500
    ev = make_tool_call_event(
        tool_call_id="c", name="n", outcome="error", msg_index=0,
        duration_ms=0.0, error=long_err,
    )
    assert len(ev["error"]) == 200


def test_append_events_batch_writes_atomically(tmp_path):
    meta_path = tmp_path / "test.meta.json"
    events = [
        make_tool_call_event(
            tool_call_id=f"call_{i}",
            name="read_file",
            outcome="ok",
            msg_index=i,
            duration_ms=1.0,
        )
        for i in range(3)
    ]
    append_events_batch(meta_path, "cli:test", events)

    data = read_meta(meta_path)
    assert data["session_key"] == "cli:test"
    assert len(data["events"]) == 3
    for i, ev in enumerate(data["events"]):
        assert ev["id"] == f"call_{i}"
        assert ev["msg_index"] == i
        assert "recorded_at" in ev  # auto-added


def test_append_events_batch_preserves_existing_events(tmp_path):
    """Appending should not clobber events from prior calls."""
    meta_path = tmp_path / "test.meta.json"
    append_events_batch(meta_path, "cli:test", [
        make_tool_call_event(
            tool_call_id="first", name="n", outcome="ok", msg_index=0, duration_ms=0.0,
        ),
    ])
    append_events_batch(meta_path, "cli:test", [
        make_tool_call_event(
            tool_call_id="second", name="n", outcome="ok", msg_index=1, duration_ms=0.0,
        ),
    ])

    data = read_meta(meta_path)
    assert [e["id"] for e in data["events"]] == ["first", "second"]


def test_append_events_batch_noop_when_empty(tmp_path):
    meta_path = tmp_path / "test.meta.json"
    append_events_batch(meta_path, "cli:test", [])
    # No file should have been written.
    assert not meta_path.exists()


def _make_minimal_loop(tmp_path):
    """Construct just enough of an AgentLoop to exercise ``_save_turn``.

    ``AgentLoop.__init__`` pulls in Pydantic models that require a full
    runtime; for testing ``_save_turn`` we only need
    ``max_tool_result_chars`` and a working ``SessionManager``.
    """
    from durin.agent.loop import AgentLoop
    from durin.config.schema import AgentDefaults
    from durin.session.manager import SessionManager

    loop = AgentLoop.__new__(AgentLoop)
    loop.max_tool_result_chars = AgentDefaults().max_tool_result_chars
    loop.sessions = SessionManager(tmp_path)
    return loop


@pytest.mark.asyncio
async def test_save_turn_records_tool_call_meta_event(tmp_path):
    """Integration: ``_save_turn`` writes one meta event per tool call
    in an assistant message, with msg_index pointing to that message."""
    loop = _make_minimal_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:meta-test")

    # Simulate one full turn: user + assistant-with-tool-calls + tool result + final assistant.
    messages = [
        {"role": "system", "content": "sys"},  # will be skipped via `skip` arg
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_one",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "id": "call_two",
                    "type": "function",
                    "function": {"name": "grep", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_one", "content": "ok"},
        {"role": "tool", "tool_call_id": "call_two", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]

    tool_events = [
        {"name": "read_file", "status": "ok", "detail": "ok",
         "tool_call_id": "call_one", "duration_ms": 12.0},
        {"name": "grep", "status": "error", "detail": "bad pattern",
         "tool_call_id": "call_two", "duration_ms": 45.0},
    ]

    loop._save_turn(session, messages, skip=1, turn_latency_ms=200, tool_events=tool_events)

    meta_path = meta_path_for(session.key, loop.sessions.sessions_dir)
    data = read_meta(meta_path)

    # Two tool_call events, both pointing to the assistant message that
    # held the tool_calls block (index of that message in session.messages).
    tc_events = [e for e in data["events"] if e["type"] == "tool_call"]
    assert len(tc_events) == 2

    by_id = {e["id"]: e for e in tc_events}
    assert by_id["call_one"]["name"] == "read_file"
    assert by_id["call_one"]["outcome"] == "ok"
    assert by_id["call_two"]["name"] == "grep"
    assert by_id["call_two"]["outcome"] == "error"
    assert "bad pattern" in by_id["call_two"]["error"]

    # Parallel tool calls share the same msg_index (their assistant message).
    assert by_id["call_one"]["msg_index"] == by_id["call_two"]["msg_index"]

    # That index must actually point at an assistant message in the
    # persisted timeline.
    persisted_assistant_idx = by_id["call_one"]["msg_index"]
    assert session.messages[persisted_assistant_idx]["role"] == "assistant"
    assert any(
        tc.get("id") == "call_one"
        for tc in session.messages[persisted_assistant_idx]["tool_calls"]
    )


@pytest.mark.asyncio
async def test_save_turn_with_no_tool_events_writes_no_meta_events(tmp_path):
    """A turn with zero tool calls leaves the meta file untouched."""
    loop = _make_minimal_loop(tmp_path)
    session = loop.sessions.get_or_create("cli:no-tools")

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]

    loop._save_turn(session, messages, skip=1, tool_events=[])

    meta_path = meta_path_for(session.key, loop.sessions.sessions_dir)
    assert not meta_path.exists()
