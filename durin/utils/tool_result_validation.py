"""Tool result middleware validation: enforces size and format constraints.

The aggregate ``max_tool_result_chars`` cap and the disk spill
(:func:`maybe_persist_tool_result`) handle text: a long result is saved
whole and replaced by a pointer the model pages. They cannot save an image
or audio payload — a list holding one is not text (``stringify_text_blocks``
returns ``None``) — so a vision/image-gen tool returning a 30 MB base64
image would put 30 MB straight into the LLM context.

This module caps each block of a list-of-blocks result:

- ``image_url`` data URL whose base64 payload exceeds
  ``MAX_IMAGE_BLOCK_BYTES`` (5 MB) → replaced with a text placeholder.
- ``input_audio`` block whose ``data`` field exceeds
  ``MAX_AUDIO_BLOCK_BYTES`` (10 MB) → replaced with a text placeholder.
- Text block longer than ``MAX_BLOCK_TEXT_CHARS`` (100 KB) → truncated with
  a clear marker. The runner applies this cap last, only to text the spill
  could not save; nothing cut here is on disk.

Defaults are constants here, not config, because they're protective limits
not policy. Callers can pass overrides if they have a specific reason.
"""

from __future__ import annotations

from typing import Any

# Caps: 100KB details / 5MB image. Audio cap is a durin addition for
# ``interpret_audio``-style tool results.
MAX_BLOCK_TEXT_CHARS = 100_000
MAX_IMAGE_BLOCK_BYTES = 5 * 1024 * 1024
MAX_AUDIO_BLOCK_BYTES = 10 * 1024 * 1024

_TEXT_TRUNCATION_MARKER = "\n[block truncated: original size {original} chars]"


def _image_data_url_payload_size(url: Any) -> int:
    """Return the size in bytes a ``data:...;base64,...`` URL decodes to.

    For non-data URLs (http(s)://, file://, …) returns 0 — those are
    references, not embedded payloads, and don't count against the cap.
    """
    if not isinstance(url, str):
        return 0
    if not url.startswith("data:"):
        return 0
    comma = url.find(",")
    if comma == -1:
        return 0
    payload = url[comma + 1:]
    # base64 inflates by ~4/3; decoded bytes ≈ len * 3 / 4 (ignoring padding).
    # Good enough for a size guard — no need to actually b64decode.
    return (len(payload) * 3) // 4


def _truncate_text_block(block: dict[str, Any], max_chars: int) -> dict[str, Any]:
    text = block.get("text")
    if not isinstance(text, str) or len(text) <= max_chars:
        return block
    head = text[:max_chars]
    marker = _TEXT_TRUNCATION_MARKER.format(original=len(text))
    return {**block, "text": head + marker}


def _validate_image_block(
    block: dict[str, Any],
    max_image_bytes: int,
) -> dict[str, Any]:
    image_url = block.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else None
    size = _image_data_url_payload_size(url)
    if size <= max_image_bytes:
        return block
    placeholder = (
        f"[image dropped: {size} bytes exceeds {max_image_bytes}-byte cap. "
        f"The model would have run out of context before reaching other "
        f"results. Re-run with a smaller image or a URL reference.]"
    )
    return {"type": "text", "text": placeholder}


def _validate_audio_block(
    block: dict[str, Any],
    max_audio_bytes: int,
) -> dict[str, Any]:
    audio = block.get("input_audio")
    data = audio.get("data") if isinstance(audio, dict) else None
    if not isinstance(data, str):
        return block
    # input_audio.data is raw base64 (no data-URL wrapping).
    size = (len(data) * 3) // 4
    if size <= max_audio_bytes:
        return block
    placeholder = (
        f"[audio dropped: {size} bytes exceeds {max_audio_bytes}-byte cap.]"
    )
    return {"type": "text", "text": placeholder}


def validate_tool_result_blocks(
    content: Any,
    *,
    max_block_chars: int | None = MAX_BLOCK_TEXT_CHARS,
    max_image_bytes: int = MAX_IMAGE_BLOCK_BYTES,
    max_audio_bytes: int = MAX_AUDIO_BLOCK_BYTES,
) -> Any:
    """Enforce per-block caps on a list-of-blocks tool result.

    Pass-through for non-list content (the aggregate ``max_tool_result_chars``
    cap and disk spillover handle string/dict results). For lists, returns a
    new list with offending blocks replaced or truncated. Returns the
    original object if no block needed modification. ``max_block_chars=None``
    leaves text blocks whole and caps only image and audio payloads.
    """
    if not isinstance(content, list):
        return content
    out: list[Any] = []
    changed = False
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype == "text":
            new_block = (
                block if max_block_chars is None
                else _truncate_text_block(block, max_block_chars)
            )
        elif btype == "image_url":
            new_block = _validate_image_block(block, max_image_bytes)
        elif btype == "input_audio":
            new_block = _validate_audio_block(block, max_audio_bytes)
        else:
            new_block = block
        if new_block is not block:
            changed = True
        out.append(new_block)
    return out if changed else content
