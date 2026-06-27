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
