"""What a compaction leaves out of the spans it summarizes: the placeholders
of turns that produced no answer, never what the user asked."""

from __future__ import annotations

from durin.utils.runtime import (
    MODEL_ERROR_PLACEHOLDER,
    NO_ROOM_PLACEHOLDER,
    OVERFLOW_PLACEHOLDER,
    without_failure_placeholders,
)


def test_the_placeholders_go_and_every_user_message_stays():
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

    assert [m["content"] for m in without_failure_placeholders(messages)] == [
        "first", "answer", "a paste far too long", "asked while the model was down",
        "asked with an AGENTS.md larger than the window", "last", "answer again",
    ]


def test_a_request_retried_after_a_model_error_stays():
    """The user's "try again" answers nothing by itself: the request it
    retries is what the summary has to keep."""
    messages = [
        {"role": "user", "content": "Set up the billing service: port 8443, vault path secret/billing/prod."},
        {"role": "assistant", "content": MODEL_ERROR_PLACEHOLDER},
        {"role": "user", "content": "try again"},
        {"role": "assistant", "content": "Done: the service is configured and running."},
    ]

    assert [m["content"] for m in without_failure_placeholders(messages)] == [
        messages[0]["content"], "try again", messages[3]["content"],
    ]


def test_two_queued_messages_before_a_failure_both_stay():
    messages = [
        {"role": "user", "content": "Rename the project to Atlas."},
        {"role": "user", "content": "Also bump the version to 2.0."},
        {"role": "assistant", "content": OVERFLOW_PLACEHOLDER},
        {"role": "user", "content": "please do both now"},
        {"role": "assistant", "content": "Renamed and bumped."},
    ]

    assert [m["content"] for m in without_failure_placeholders(messages)] == [
        "Rename the project to Atlas.", "Also bump the version to 2.0.", "please do both now", "Renamed and bumped.",
    ]


def test_a_turn_that_called_tools_before_failing_keeps_its_work():
    messages = [
        {"role": "user", "content": "look it up"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                                                            "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "grep", "content": "found it"},
        {"role": "assistant", "content": OVERFLOW_PLACEHOLDER},
    ]

    assert without_failure_placeholders(messages) == messages[:3]
