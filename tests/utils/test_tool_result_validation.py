"""Tests for per-block tool-result validation middleware.

The aggregate ``max_tool_result_chars`` cap doesn't help when a tool returns
a *list of content blocks* that includes image/audio payloads — the existing
spillover path skips non-text content. These caps run BEFORE the aggregate
path so a single huge block can't crowd out its siblings.
"""

from __future__ import annotations

from durin.utils.tool_result_validation import (
    MAX_BLOCK_TEXT_CHARS,
    MAX_IMAGE_BLOCK_BYTES,
    MAX_AUDIO_BLOCK_BYTES,
    validate_tool_result_blocks,
)


def test_passthrough_for_string_content():
    """Strings are handled by the aggregate cap, not this middleware."""
    assert validate_tool_result_blocks("hello") == "hello"
    assert validate_tool_result_blocks("x" * (MAX_BLOCK_TEXT_CHARS * 2)) == "x" * (MAX_BLOCK_TEXT_CHARS * 2)


def test_passthrough_for_unchanged_list():
    """If no block exceeds caps, the original list object is returned
    (identity preserved — avoids allocating a new list every call)."""
    blocks = [
        {"type": "text", "text": "small text"},
        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
    ]
    result = validate_tool_result_blocks(blocks)
    assert result is blocks


def test_truncates_oversized_text_block():
    big = "a" * (MAX_BLOCK_TEXT_CHARS + 100)
    blocks = [{"type": "text", "text": big}]
    out = validate_tool_result_blocks(blocks)
    assert out is not blocks
    text = out[0]["text"]
    assert text.startswith("a" * MAX_BLOCK_TEXT_CHARS)
    assert "block truncated" in text
    assert str(MAX_BLOCK_TEXT_CHARS + 100) in text


def test_truncated_block_preserves_sibling_block_metadata():
    """Truncating one block must not remove other blocks or fields."""
    big = "a" * (MAX_BLOCK_TEXT_CHARS + 1)
    blocks = [
        {"type": "text", "text": "small"},
        {"type": "text", "text": big, "cache_control": {"type": "ephemeral"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
    ]
    out = validate_tool_result_blocks(blocks)
    assert len(out) == 3
    assert out[0]["text"] == "small"
    assert out[1]["cache_control"] == {"type": "ephemeral"}
    assert out[2]["type"] == "image_url"


def test_drops_oversized_image_block_replaces_with_text_placeholder():
    """A data-URL image whose base64 payload exceeds the 5MB cap is
    replaced with a text placeholder so the model knows what happened
    instead of silently receiving a partial image."""
    # 8 MB of base64 data → decodes to ~6 MB → exceeds 5 MB cap
    payload = "A" * (MAX_IMAGE_BLOCK_BYTES * 4 // 3 + 1024)
    blocks = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
    ]
    out = validate_tool_result_blocks(blocks)
    assert out is not blocks
    assert out[0]["type"] == "text"
    assert "image dropped" in out[0]["text"]
    assert "5242880" in out[0]["text"]  # 5 MB in bytes


def test_keeps_image_block_with_external_url():
    """Non-data URLs (http/https) are references, not payloads — they
    don't count against the size cap regardless of how the host responds."""
    blocks = [
        {"type": "image_url", "image_url": {"url": "https://example.com/giant.png"}},
    ]
    out = validate_tool_result_blocks(blocks)
    assert out is blocks


def test_keeps_image_block_with_small_data_url():
    """A small data-URL image passes through untouched."""
    blocks = [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="}},
    ]
    out = validate_tool_result_blocks(blocks)
    assert out is blocks


def test_drops_oversized_audio_block():
    # 14 MB of base64 → decodes to ~10.5 MB → exceeds 10 MB cap
    payload = "B" * (MAX_AUDIO_BLOCK_BYTES * 4 // 3 + 4096)
    blocks = [
        {"type": "input_audio", "input_audio": {"data": payload, "format": "wav"}},
    ]
    out = validate_tool_result_blocks(blocks)
    assert out[0]["type"] == "text"
    assert "audio dropped" in out[0]["text"]


def test_keeps_audio_block_with_small_payload():
    blocks = [
        {"type": "input_audio", "input_audio": {"data": "Zm9vYmFy", "format": "wav"}},
    ]
    out = validate_tool_result_blocks(blocks)
    assert out is blocks


def test_unknown_block_types_pass_through():
    """Anthropic / Bedrock can carry block types this validator doesn't
    know about (e.g. ``tool_use``, ``tool_result``). It must not mutate
    them — let the provider adapter sort it out."""
    blocks = [
        {"type": "tool_result", "tool_use_id": "abc", "content": "ok"},
        {"type": "video", "source": "..."},  # hypothetical future type
    ]
    out = validate_tool_result_blocks(blocks)
    assert out is blocks


def test_non_dict_block_items_pass_through():
    """Defensive: if a block is a bare string or None (malformed but
    possible), the validator must not crash."""
    blocks = ["raw string", None, {"type": "text", "text": "ok"}]
    out = validate_tool_result_blocks(blocks)
    assert out is blocks


def test_custom_caps_can_be_smaller():
    """Callers can pass smaller overrides for stricter contexts."""
    blocks = [{"type": "text", "text": "x" * 200}]
    out = validate_tool_result_blocks(blocks, max_block_chars=100)
    assert out[0]["text"].startswith("x" * 100)
    assert "block truncated" in out[0]["text"]


def test_malformed_image_block_does_not_crash():
    """An image_url block missing ``url`` or with a non-dict image_url
    must not crash — pass through unchanged."""
    blocks = [
        {"type": "image_url"},  # missing image_url
        {"type": "image_url", "image_url": "not a dict"},
        {"type": "image_url", "image_url": {}},  # missing url
    ]
    out = validate_tool_result_blocks(blocks)
    assert out == blocks
