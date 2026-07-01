"""Test that replay builder carries render_as field for command outputs."""

from durin.utils.webui_transcript import replay_transcript_to_ui_messages


def test_replay_preserves_render_as():
    """Verify that render_as from transcript is carried to renderAs in UIMessage."""
    records = [
        {
            "event": "message",
            "role": "assistant",
            "text": "## Persona",
            "render_as": "text",
        }
    ]
    msgs = replay_transcript_to_ui_messages(records)
    assert any(m.get("renderAs") == "text" for m in msgs)


def test_replay_preserves_render_as_markdown():
    """Verify that render_as: markdown is carried."""
    records = [
        {
            "event": "message",
            "role": "assistant",
            "text": "# Header",
            "render_as": "markdown",
        }
    ]
    msgs = replay_transcript_to_ui_messages(records)
    assert any(m.get("renderAs") == "markdown" for m in msgs)


def test_replay_omits_invalid_render_as():
    """Verify that invalid render_as values are not carried."""
    records = [
        {
            "event": "message",
            "role": "assistant",
            "text": "Some text",
            "render_as": "invalid",
        }
    ]
    msgs = replay_transcript_to_ui_messages(records)
    # Should not have renderAs or it should not be "invalid"
    assert not any(m.get("renderAs") == "invalid" for m in msgs)


def test_replay_without_render_as():
    """Verify that messages without render_as don't get renderAs field."""
    records = [
        {
            "event": "message",
            "role": "assistant",
            "text": "Some text",
        }
    ]
    msgs = replay_transcript_to_ui_messages(records)
    # Should not add renderAs if it wasn't in the record
    assert not any("renderAs" in m for m in msgs)


def test_replay_reuses_persisted_message_id():
    """A persisted server id is reused as the replay UIMessage id so live and
    refetch rows share a React key and merge instead of swapping."""
    records = [{"event": "message", "role": "assistant", "text": "hi", "id": "msg-abc"}]
    msgs = replay_transcript_to_ui_messages(records)
    assert any(m.get("id") == "msg-abc" for m in msgs)


def test_replay_generates_id_when_record_has_none():
    """A record without an id still gets a generated id (back-compat with old
    transcripts written before the server stamped one)."""
    records = [{"event": "message", "role": "assistant", "text": "hi"}]
    msgs = replay_transcript_to_ui_messages(records)
    assert len(msgs) == 1
    assert isinstance(msgs[0].get("id"), str) and msgs[0]["id"]


def test_replay_reuses_persisted_id_over_reasoning_placeholder():
    """When the message absorbs a prior reasoning-only placeholder, the
    persisted id still wins (the row keys to the server id, not the buffer id)."""
    records = [
        {"event": "reasoning_delta", "text": "thinking"},
        {"event": "reasoning_end"},
        {"event": "message", "role": "assistant", "text": "answer", "id": "msg-xyz"},
    ]
    msgs = replay_transcript_to_ui_messages(records)
    answer_rows = [m for m in msgs if m.get("content") == "answer"]
    assert answer_rows and answer_rows[0].get("id") == "msg-xyz"
