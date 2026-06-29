"""Tests for session_messages_to_ui_messages — OpenAI-format messages → UIMessage dicts."""

from __future__ import annotations

import time

import pytest

from durin.utils.webui_transcript import session_messages_to_ui_messages


def test_user_message_basic() -> None:
    """A plain user message produces one UIMessage with role='user' and content."""
    msgs = [{"role": "user", "content": "hello", "timestamp": 1700000000.0}]
    result = session_messages_to_ui_messages(msgs)
    assert len(result) == 1
    m = result[0]
    assert m["role"] == "user"
    assert m["content"] == "hello"
    assert "id" in m
    assert isinstance(m["createdAt"], int)
    # timestamp * 1000
    assert m["createdAt"] == 1700000000000


def test_assistant_message_with_reasoning() -> None:
    """An assistant message with reasoning_content surfaces as reasoning field."""
    msgs = [
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "I thought about it",
            "timestamp": 1700000001.0,
        }
    ]
    result = session_messages_to_ui_messages(msgs)
    assert len(result) == 1
    m = result[0]
    assert m["role"] == "assistant"
    assert m["content"] == "answer"
    assert m["reasoning"] == "I thought about it"
    assert m["createdAt"] == 1700000001000


def test_tool_call_and_result_folded_into_assistant() -> None:
    """An assistant tool_call + matching tool result folds into toolEvents on the assistant
    message; the tool result does NOT appear as a standalone message."""
    msgs = [
        {
            "role": "user",
            "content": "do something",
            "timestamp": 1700000000.0,
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "/tmp/x"}'},
                }
            ],
            "timestamp": 1700000001.0,
        },
        {
            "role": "tool",
            "tool_call_id": "call_abc",
            "name": "read_file",
            "content": "file contents",
            "timestamp": 1700000002.0,
        },
    ]
    result = session_messages_to_ui_messages(msgs)
    # user + assistant; tool result folded in
    assert len(result) == 2
    assert result[0]["role"] == "user"
    asst = result[1]
    assert asst["role"] == "assistant"
    tool_events = asst.get("toolEvents")
    assert isinstance(tool_events, list)
    assert len(tool_events) == 1
    ev = tool_events[0]
    assert ev["name"] == "read_file"
    assert ev["call_id"] == "call_abc"
    # The result content must be attached
    assert ev.get("result") == "file contents"


def test_multimodal_user_content_extracts_text_and_images() -> None:
    """A user message whose content is a list: text part → content, image part → images."""
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
            "timestamp": 1700000000.0,
        }
    ]
    result = session_messages_to_ui_messages(msgs)
    assert len(result) == 1
    m = result[0]
    assert m["role"] == "user"
    assert m["content"] == "look at this"
    images = m.get("images")
    assert isinstance(images, list)
    assert len(images) == 1
    assert images[0]["url"] == "data:image/png;base64,abc"


def test_system_messages_and_header_skipped() -> None:
    """The leading session header dict (has _type key) and system messages are skipped."""
    msgs = [
        # session header line
        {"_type": "metadata", "key": "cli:foo", "created_at": "2024-01-01"},
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "hello", "timestamp": 1700000000.0},
    ]
    result = session_messages_to_ui_messages(msgs)
    assert len(result) == 1
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "hello"


def test_augment_user_media_called_for_media_paths() -> None:
    """When a user message has file-path content parts, augment_user_media is called."""
    seen_paths: list[list[str]] = []

    def fake_augment(paths: list[str]) -> list[dict]:
        seen_paths.append(paths)
        return [{"kind": "image", "url": f"/api/media/signed/{p}", "name": p} for p in paths]

    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "see this file"},
                {"type": "image_url", "image_url": {"url": "/tmp/image.png"}},
            ],
            "timestamp": 1700000000.0,
        }
    ]
    result = session_messages_to_ui_messages(msgs, augment_user_media=fake_augment)
    assert len(seen_paths) == 1
    assert "/tmp/image.png" in seen_paths[0]
    m = result[0]
    media = m.get("media")
    assert isinstance(media, list)
    assert any("signed" in att.get("url", "") for att in media)


def test_timestamp_fallback_when_missing() -> None:
    """Messages without a timestamp still get a numeric createdAt."""
    msgs = [{"role": "user", "content": "hi"}]
    result = session_messages_to_ui_messages(msgs)
    assert isinstance(result[0]["createdAt"], int)
    assert result[0]["createdAt"] > 0
