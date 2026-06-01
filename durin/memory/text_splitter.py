"""Recursive character text splitter (doc 04 / P5.3).

Splits long documents into ~chunk_size character chunks with
~overlap character overlap, preferring cuts at:

  paragraph (``\\n\\n``) → line (``\\n``) → sentence (``. ``) →
  word (`` ``) → char

The recursion is intentional: when the chunk reaches `chunk_size`,
we look backwards within the last `overlap`-sized window for the
preferred separator. If found, cut there. Else fall through to the
next-coarser boundary.

This is the same conceptual pattern as LangChain's
`RecursiveCharacterTextSplitter` but implemented from scratch to
avoid the heavy dep + match doc 04's specific preference order.
"""

from __future__ import annotations

from typing import Sequence

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_OVERLAP",
    "split_text",
]


DEFAULT_CHUNK_SIZE: int = 1500
DEFAULT_OVERLAP: int = 200

# Preferred boundaries in descending order of "logical-ness".
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", " ", "")


def split_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    separators: Sequence[str] = _SEPARATORS,
) -> list[str]:
    """Split *text* into chunks of ~``chunk_size`` chars with
    ~``overlap`` overlap.

    Empty input returns ``[]``. Input shorter than ``chunk_size``
    returns a single-element list (the input verbatim).
    """
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    cursor = 0
    text_len = len(text)
    while cursor < text_len:
        end_target = cursor + chunk_size
        if end_target >= text_len:
            chunks.append(text[cursor:].rstrip())
            break

        # Look for the best separator within the preferred window:
        # [end_target - overlap, end_target + overlap]. We prefer
        # cuts at LATER positions inside this window (closer to the
        # target) so the chunk size is closer to chunk_size, not
        # half of it.
        cut_at = _find_best_cut(
            text, cursor=cursor, end_target=end_target,
            overlap=overlap, separators=separators,
        )
        chunk = text[cursor:cut_at].rstrip()
        if chunk:
            chunks.append(chunk)
        # Next cursor: cut_at minus overlap so the next chunk
        # carries some context. Never go backwards.
        next_cursor = max(cut_at - overlap, cursor + 1)
        cursor = next_cursor

    return chunks


def _find_best_cut(
    text: str,
    *,
    cursor: int,
    end_target: int,
    overlap: int,
    separators: Sequence[str],
) -> int:
    """Find the highest-priority separator near ``end_target``.

    Search window: ``[end_target - overlap, end_target + overlap]``,
    clipped to text bounds. Returns the index AFTER the separator
    so ``text[cursor:cut_at]`` is the chunk content.

    Falls back to hard-cut at ``end_target`` when no separator is
    found (e.g. a token longer than the window).
    """
    lo = max(cursor + 1, end_target - overlap)
    hi = min(len(text), end_target + overlap)
    # Try each separator in priority order.
    for sep in separators:
        if not sep:
            continue  # the "" sentinel triggers the hard-cut below
        # Find LAST occurrence of sep within [lo, hi].
        # We search backwards so the cut sits closer to end_target.
        search_region = text[lo:hi]
        idx = search_region.rfind(sep)
        if idx >= 0:
            return lo + idx + len(sep)
    # No separator found in window — hard cut at end_target.
    return min(end_target, len(text))
