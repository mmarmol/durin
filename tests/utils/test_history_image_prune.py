"""History image pruning (OpenClaw-inspired Tier 2 B3).

Conversations that exchanged images / audio in earlier turns keep
shipping those payloads on every subsequent request — the LLM already
attended to them, but the bytes ride along forever. This module
replaces them with a text marker once they're older than
``preserve_turns`` completed turns (default 3).
"""

from __future__ import annotations

from durin.utils.history_image_prune import (
    PRUNED_HISTORY_AUDIO_MARKER,
    PRUNED_HISTORY_IMAGE_MARKER,
    prune_processed_history_images,
)


def _image_block(url: str = "data:image/png;base64,iVB...") -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def _anthropic_image_block() -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVB..."}}


def _audio_block() -> dict:
    return {"type": "input_audio", "input_audio": {"data": "Zm9v", "format": "wav"}}


def _turn(user_content, assistant_content="ok") -> list[dict]:
    """Build a complete user→assistant turn."""
    return [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": assistant_content},
    ]


def test_empty_messages_passes_through():
    assert prune_processed_history_images([]) == []


def test_fewer_than_preserve_turns_passes_through_identity():
    """With 3 turns and default preserve=3, nothing should be pruned —
    and the SAME list object is returned (no allocation)."""
    msgs = _turn([_image_block()]) + _turn([_image_block()]) + _turn([_image_block()])
    out = prune_processed_history_images(msgs, preserve_turns=3)
    assert out is msgs


def test_prunes_oldest_turn_when_window_exceeded():
    """4 turns with preserve=3 → the first (oldest) turn has its image
    replaced with the marker, the latest 3 stay intact."""
    oldest = _turn([
        {"type": "text", "text": "look at this"},
        _image_block("data:image/png;base64,OLD"),
    ])
    middle1 = _turn([_image_block("data:image/png;base64,M1")])
    middle2 = _turn([_image_block("data:image/png;base64,M2")])
    newest = _turn([_image_block("data:image/png;base64,NEW")])
    msgs = oldest + middle1 + middle2 + newest

    out = prune_processed_history_images(msgs, preserve_turns=3)
    assert out is not msgs

    # Oldest user message: image_url replaced, text preserved.
    oldest_user = out[0]
    assert oldest_user["role"] == "user"
    blocks = oldest_user["content"]
    assert blocks[0] == {"type": "text", "text": "look at this"}
    assert blocks[1] == {"type": "text", "text": PRUNED_HISTORY_IMAGE_MARKER}

    # Recent 3 turns untouched — same image data.
    for idx in (2, 4, 6):  # user messages of middle1, middle2, newest
        assert out[idx]["content"][0]["type"] == "image_url"


def test_prunes_anthropic_style_image_blocks():
    """Anthropic uses ``{"type": "image", ...}`` instead of image_url."""
    msgs = (
        _turn([_anthropic_image_block()])
        + _turn([])
        + _turn([])
        + _turn([])
    )
    out = prune_processed_history_images(msgs, preserve_turns=3)
    assert out is not msgs
    assert out[0]["content"][0] == {"type": "text", "text": PRUNED_HISTORY_IMAGE_MARKER}


def test_prunes_input_audio_blocks():
    """Audio blocks survive the same prune path with a distinct marker."""
    msgs = (
        _turn([_audio_block()])
        + _turn([])
        + _turn([])
        + _turn([])
    )
    out = prune_processed_history_images(msgs, preserve_turns=3)
    assert out is not msgs
    assert out[0]["content"][0] == {"type": "text", "text": PRUNED_HISTORY_AUDIO_MARKER}


def test_assistant_messages_with_images_are_not_pruned():
    """Only user and tool messages are pruned — assistant responses are
    preserved verbatim (they're durin's own output and may include
    image references the model needs to keep consistent)."""
    # Build 4 completed turns; one of the assistant replies carries an
    # image_url (unusual but possible — e.g. image_generation tool wired
    # into the assistant output).
    msgs = [
        {"role": "user", "content": "show me"},
        {"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,A"}}]},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]
    out = prune_processed_history_images(msgs, preserve_turns=3)
    # Assistant's image block is untouched.
    assert out[1]["content"][0]["type"] == "image_url"


def test_tool_messages_with_images_are_pruned():
    """Tool results that returned an image (e.g. interpret_image bridge)
    should be pruned the same as user messages."""
    msgs = [
        {"role": "user", "content": "analyze"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "interpret_image", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "interpret_image",
         "content": [
             {"type": "text", "text": "Caption:"},
             _image_block("data:image/png;base64,OLD"),
         ]},
        # 3 more complete turns so the first one ages out:
        {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"}, {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"}, {"role": "assistant", "content": "a4"},
    ]
    out = prune_processed_history_images(msgs, preserve_turns=3)
    tool_msg = out[2]
    assert tool_msg["role"] == "tool"
    blocks = tool_msg["content"]
    assert blocks[0] == {"type": "text", "text": "Caption:"}
    assert blocks[1] == {"type": "text", "text": PRUNED_HISTORY_IMAGE_MARKER}


def test_preserve_turns_zero_is_clamped_to_one():
    """``preserve_turns=0`` is clamped to 1 — pruning absolutely everything
    would lose context for the immediately preceding exchange."""
    msgs = _turn([_image_block()]) + _turn([_image_block()])
    out = prune_processed_history_images(msgs, preserve_turns=0)
    # First turn pruned, second (most recent) preserved.
    assert out[0]["content"][0] == {"type": "text", "text": PRUNED_HISTORY_IMAGE_MARKER}
    assert out[2]["content"][0]["type"] == "image_url"


def test_idempotent_when_already_pruned():
    """Calling the function twice on the same list must produce the same
    result — the second call sees only text markers and changes nothing."""
    msgs = _turn([_image_block()]) + _turn([_image_block()]) + _turn([_image_block()]) + _turn([_image_block()])
    once = prune_processed_history_images(msgs, preserve_turns=3)
    twice = prune_processed_history_images(once, preserve_turns=3)
    assert twice is once  # nothing left to prune → identity returned


def test_string_content_passes_through_unchanged():
    """We don't currently try to identify image references inside string
    content (durin doesn't have the OpenClaw ``media://inbound/`` URL
    pattern). String-content messages must not be mutated."""
    msgs = [
        {"role": "user", "content": "Here is media://inbound/xyz123"},
    ] + _turn([]) * 3 + _turn([])  # 4 completed turns total
    out = prune_processed_history_images(msgs, preserve_turns=3)
    # First user message's string is identical.
    assert out[0]["content"] == "Here is media://inbound/xyz123"


def test_env_override_default(monkeypatch):
    """``DURIN_HISTORY_IMAGE_PRESERVE_TURNS`` overrides default."""
    monkeypatch.setenv("DURIN_HISTORY_IMAGE_PRESERVE_TURNS", "1")
    # 2 turns; with preserve=1 (env), the first (oldest) prunes.
    msgs = _turn([_image_block()]) + _turn([_image_block()])
    out = prune_processed_history_images(msgs)  # no preserve_turns arg → reads env
    assert out[0]["content"][0]["type"] == "text"
    assert out[2]["content"][0]["type"] == "image_url"  # newest preserved


def test_env_override_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("DURIN_HISTORY_IMAGE_PRESERVE_TURNS", "not-a-number")
    msgs = _turn([_image_block()]) + _turn([_image_block()]) + _turn([_image_block()])
    # Should fall back to default 3, so nothing pruned.
    out = prune_processed_history_images(msgs)
    assert out is msgs


# ---------------------------------------------------------------------------
# Stats out-param (audit P1.2b: lets the runner emit telemetry without
# re-walking the list to count what changed).
# ---------------------------------------------------------------------------


def test_stats_populated_when_pruning_occurs():
    msgs = (
        _turn([_image_block()])
        + _turn([_audio_block()])
        + _turn([])
        + _turn([])
        + _turn([])
    )
    stats: dict = {}
    prune_processed_history_images(msgs, preserve_turns=3, stats=stats)
    assert stats["image_blocks_removed"] == 1
    assert stats["audio_blocks_removed"] == 1
    assert stats["preserve_turns"] == 3


def test_stats_zeroed_when_nothing_pruned():
    """Caller can rely on stats keys being present (and 0) even when the
    pruner was a no-op — avoids guarding every read with ``.get(..., 0)``."""
    msgs = _turn([_image_block()]) + _turn([_image_block()])  # 2 turns, preserve=3
    stats: dict = {}
    prune_processed_history_images(msgs, preserve_turns=3, stats=stats)
    assert stats == {"image_blocks_removed": 0, "audio_blocks_removed": 0, "preserve_turns": 3}


def test_stats_zeroed_when_messages_empty():
    """Empty input → all counts 0, preserve_turns reflects what was used."""
    stats: dict = {}
    prune_processed_history_images([], preserve_turns=3, stats=stats)
    assert stats["image_blocks_removed"] == 0
    assert stats["audio_blocks_removed"] == 0


def test_stats_counts_multiple_image_blocks_per_message():
    """One user message with multiple images → all counted."""
    msgs = (
        _turn([_image_block("data:image/png;base64,A"), _image_block("data:image/png;base64,B"), _image_block("data:image/png;base64,C")])
        + _turn([])
        + _turn([])
        + _turn([])
    )
    stats: dict = {}
    prune_processed_history_images(msgs, preserve_turns=3, stats=stats)
    assert stats["image_blocks_removed"] == 3
    assert stats["audio_blocks_removed"] == 0
