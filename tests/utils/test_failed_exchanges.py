"""What a compaction leaves out of the spans it summarizes: the exchanges of
turns that produced no answer."""

from __future__ import annotations

from durin.utils.runtime import (
    MODEL_ERROR_PLACEHOLDER,
    NO_ROOM_PLACEHOLDER,
    OVERFLOW_PLACEHOLDER,
    without_failed_exchanges,
)


def test_a_failed_turn_leaves_out_its_message_and_placeholder():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "a paste far too long"},
        {"role": "assistant", "content": OVERFLOW_PLACEHOLDER},
        {"role": "user", "content": "asked while the model was down"},
        {"role": "assistant", "content": MODEL_ERROR_PLACEHOLDER},
        {"role": "user", "content": "asked with an AGENTS.md larger than the window"},
        {"role": "assistant", "content": NO_ROOM_PLACEHOLDER},
        {"role": "user", "content": "last"},
        {"role": "assistant", "content": "answer again"},
    ]

    assert [m["content"] for m in without_failed_exchanges(messages)] == [
        "first", "answer", "last", "answer again",
    ]


def test_a_turn_that_called_tools_before_failing_keeps_its_work():
    messages = [
        {"role": "user", "content": "look it up"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                            "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "grep", "content": "found it"},
        {"role": "assistant", "content": OVERFLOW_PLACEHOLDER},
    ]

    assert without_failed_exchanges(messages) == messages[:3]
