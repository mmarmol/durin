"""Tests for the _is_live_progress_only predicate that guards transcript persistence."""

from durin.channels.websocket import _is_live_progress_only


def test_all_running_tool_events_returns_true() -> None:
    payload = {
        "event": "message",
        "tool_events": [
            {"phase": "running", "call_id": "c1", "name": "web_search"},
            {"phase": "running", "call_id": "c2", "name": "memory_search"},
        ],
    }
    assert _is_live_progress_only(payload) is True


def test_terminal_phase_end_returns_false() -> None:
    payload = {
        "event": "message",
        "tool_events": [
            {"phase": "end", "call_id": "c1", "name": "web_search", "result": "ok"},
        ],
    }
    assert _is_live_progress_only(payload) is False


def test_terminal_phase_error_returns_false() -> None:
    payload = {
        "event": "message",
        "tool_events": [
            {"phase": "error", "call_id": "c1", "name": "web_search"},
        ],
    }
    assert _is_live_progress_only(payload) is False


def test_mixed_running_and_end_returns_false() -> None:
    payload = {
        "event": "message",
        "tool_events": [
            {"phase": "running", "call_id": "c1", "name": "web_search"},
            {"phase": "end", "call_id": "c1", "name": "web_search", "result": "ok"},
        ],
    }
    assert _is_live_progress_only(payload) is False


def test_no_tool_events_key_returns_false() -> None:
    payload = {"event": "message", "content": "hello"}
    assert _is_live_progress_only(payload) is False


def test_empty_tool_events_returns_false() -> None:
    payload = {"event": "message", "tool_events": []}
    assert _is_live_progress_only(payload) is False


def test_tool_events_not_a_list_returns_false() -> None:
    payload = {"event": "message", "tool_events": "running"}
    assert _is_live_progress_only(payload) is False


def test_event_without_phase_returns_false() -> None:
    payload = {
        "event": "message",
        "tool_events": [{"call_id": "c1", "name": "web_search"}],
    }
    assert _is_live_progress_only(payload) is False


def test_memory_prefetch_start_returns_true() -> None:
    """The loop always follows a memory_prefetch 'start' with an 'end' frame
    (even on cancellation — AgentLoop._state_build), so persisting 'start' on
    its own would only ever produce an orphan chip; treat it as live-only
    like a 'running' frame, reconstructable from the terminal frame."""
    payload = {
        "event": "message",
        "tool_events": [
            {"phase": "start", "call_id": "memory_prefetch:1", "name": "memory_prefetch", "arguments": {"query": "q"}},
        ],
    }
    assert _is_live_progress_only(payload) is True


def test_other_tool_start_phase_returns_false() -> None:
    """The 'start' exemption is scoped to memory_prefetch specifically, not
    to phase 'start' in general — an ordinary tool's start frame still
    persists, same as before."""
    payload = {
        "event": "message",
        "tool_events": [
            {"phase": "start", "call_id": "c1", "name": "web_search", "arguments": {}},
        ],
    }
    assert _is_live_progress_only(payload) is False


def test_memory_prefetch_start_mixed_with_running_returns_true() -> None:
    payload = {
        "event": "message",
        "tool_events": [
            {"phase": "running", "call_id": "c1", "name": "web_search"},
            {"phase": "start", "call_id": "memory_prefetch:1", "name": "memory_prefetch", "arguments": {"query": "q"}},
        ],
    }
    assert _is_live_progress_only(payload) is True
