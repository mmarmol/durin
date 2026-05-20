"""History image pruning (OpenClaw-inspired Tier 2 B3).

A conversation that exchanged images / audio in earlier turns keeps shipping
those payloads on every subsequent request — the LLM already attended to
them, but the bytes ride along forever. For a 5 MB image, every turn after
the 3rd pays for that 5 MB in upload cost AND in tokenisation overhead.

This module identifies "completed" user→assistant turns and, for any turn
older than the last ``preserve_recent_turns`` (default 3), replaces
``image_url`` / ``image`` blocks in user/tool messages with a textual
marker so the model knows the data existed without seeing the bytes.

Mirrors OpenClaw ``run/history-image-prune.ts::pruneProcessedHistoryImages``.

Per-block validation (Tier 1 2D) caps images on the WAY IN at write time.
History prune handles the READ-time problem of accumulated images surviving
across many turns.
"""

from __future__ import annotations

import os
from typing import Any

PRUNED_HISTORY_IMAGE_MARKER = "[image data removed - already processed by model]"
PRUNED_HISTORY_AUDIO_MARKER = "[audio data removed - already processed by model]"

# Mirrors OpenClaw PRESERVE_RECENT_COMPLETED_TURNS. Override with
# DURIN_HISTORY_IMAGE_PRESERVE_TURNS.
_DEFAULT_PRESERVE_TURNS = 3

# Block types that carry attended-to media bytes. ``image_url`` is the
# OpenAI / OpenAI-compat name; ``image`` is the Anthropic name;
# ``input_audio`` is the chat-multimodal audio block. ``input_image`` is
# the Responses-API equivalent.
_PRUNABLE_IMAGE_TYPES = frozenset({"image_url", "image", "input_image"})
_PRUNABLE_AUDIO_TYPES = frozenset({"input_audio"})


def _preserve_turns_setting() -> int:
    raw = os.getenv("DURIN_HISTORY_IMAGE_PRESERVE_TURNS")
    if raw is None:
        return _DEFAULT_PRESERVE_TURNS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_PRESERVE_TURNS
    # Negative clamps to 0 (prune everything except in-flight turn).
    return max(0, value)


def _resolve_prune_before_index(
    messages: list[dict[str, Any]],
    preserve_turns: int,
) -> int:
    """Return the index BEFORE which images can be pruned safely.

    Walks ``messages`` looking for *completed* turns — a user (or
    tool-result-led) sequence followed by an assistant reply. Returns
    the start-of-turn index of the Nth-most-recent completed turn,
    where N = ``preserve_turns``. Returns ``-1`` when there are fewer
    completed turns than the preservation window (nothing to prune).
    """
    completed_turn_starts: list[int] = []
    current_turn_start = -1
    current_turn_has_assistant_reply = False

    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "user":
            if current_turn_start >= 0 and current_turn_has_assistant_reply:
                completed_turn_starts.append(current_turn_start)
            current_turn_start = i
            current_turn_has_assistant_reply = False
            continue
        if role == "tool":
            if current_turn_start < 0:
                current_turn_start = i
            continue
        if role == "assistant" and current_turn_start >= 0:
            current_turn_has_assistant_reply = True

    if current_turn_start >= 0 and current_turn_has_assistant_reply:
        completed_turn_starts.append(current_turn_start)

    if len(completed_turn_starts) <= preserve_turns:
        return -1
    return completed_turn_starts[-preserve_turns]


def _prune_blocks(blocks: list[Any]) -> tuple[list[Any], bool]:
    """Replace prunable media blocks with text markers.

    Returns ``(new_blocks, changed)``. New list is returned only when
    at least one block was modified — same object otherwise so callers
    can identity-test for unchanged.
    """
    out: list[Any] = []
    changed = False
    for block in blocks:
        if not isinstance(block, dict):
            out.append(block)
            continue
        btype = block.get("type")
        if btype in _PRUNABLE_IMAGE_TYPES:
            out.append({"type": "text", "text": PRUNED_HISTORY_IMAGE_MARKER})
            changed = True
        elif btype in _PRUNABLE_AUDIO_TYPES:
            out.append({"type": "text", "text": PRUNED_HISTORY_AUDIO_MARKER})
            changed = True
        else:
            out.append(block)
    return (out, True) if changed else (blocks, False)


def prune_processed_history_images(
    messages: list[dict[str, Any]],
    *,
    preserve_turns: int | None = None,
) -> list[dict[str, Any]]:
    """Idempotent cleanup: replace media blocks in user / tool messages
    older than ``preserve_turns`` completed turns with text markers.

    Returns a new list when any block was pruned, the input list
    untouched otherwise (identity preserved). The original messages are
    not mutated.

    ``preserve_turns=None`` reads ``DURIN_HISTORY_IMAGE_PRESERVE_TURNS``
    (default 3).
    """
    if not messages:
        return messages
    if preserve_turns is None:
        preserve_turns = _preserve_turns_setting()
    # Always preserve at least the most-recent completed turn — pruning
    # absolutely everything would let the model lose context for the
    # immediately preceding exchange. Mirrors OpenClaw which doesn't
    # define the preserve=0 case cleanly either.
    preserve_turns = max(1, preserve_turns)
    prune_before = _resolve_prune_before_index(messages, preserve_turns)
    if prune_before <= 0:
        return messages

    pruned: list[dict[str, Any]] | None = None
    for i in range(prune_before):
        msg = messages[i]
        role = msg.get("role")
        if role not in ("user", "tool"):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_content, changed = _prune_blocks(content)
        if not changed:
            continue
        if pruned is None:
            pruned = [dict(m) for m in messages]
        pruned[i] = {**msg, "content": new_content}
    return pruned if pruned is not None else messages
