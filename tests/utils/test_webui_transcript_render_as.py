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
