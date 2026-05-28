"""Recursive character splitter for `memory_ingest` (P5.3 / doc 04).

Splits long documents into ~chunk_size character chunks with
~overlap character overlap, preferring cuts at paragraph > line >
sentence > word > char boundaries.
"""

from __future__ import annotations

import pytest

from durin.memory.text_splitter import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    split_text,
)


def test_short_text_one_chunk() -> None:
    out = split_text("Just a short note.", chunk_size=1500, overlap=200)
    assert out == ["Just a short note."]


def test_empty_text_returns_empty_list() -> None:
    assert split_text("") == []


def test_default_chunk_size() -> None:
    assert DEFAULT_CHUNK_SIZE == 1500
    assert DEFAULT_OVERLAP == 200


def test_long_text_multiple_chunks() -> None:
    """3000 chars with chunk_size=1500 → roughly 2-3 chunks."""
    text = "x" * 3000
    out = split_text(text, chunk_size=1500, overlap=200)
    assert len(out) >= 2
    # Each chunk respects the budget (allowing some give for boundary
    # preference — sentences can stretch slightly past the cut).
    for chunk in out:
        assert len(chunk) <= 1500 + 200  # boundary tolerance


def test_overlap_present_between_chunks() -> None:
    """Consecutive chunks share `overlap` chars at the boundary so
    a fact straddling a cut still surfaces in both chunks."""
    text = "A" * 1000 + "B" * 1000 + "C" * 1000
    out = split_text(text, chunk_size=1500, overlap=200)
    assert len(out) >= 2
    # The end of chunk 0 should appear at the start of chunk 1
    # (within the overlap window).
    if len(out) >= 2:
        end_of_c0 = out[0][-200:]
        start_of_c1 = out[1][:400]
        # Overlap is approximate — find at least one substring.
        assert any(
            end_of_c0[i:i + 50] in start_of_c1
            for i in range(0, len(end_of_c0) - 50, 10)
        )


def test_prefers_paragraph_boundary() -> None:
    """When the cut point lands inside a paragraph but a `\\n\\n`
    boundary is nearby, the splitter prefers the paragraph."""
    text = (
        "First paragraph. " * 80          # ~1280 chars
        + "\n\n"
        + "Second paragraph. " * 80       # ~1440 chars
        + "\n\n"
        + "Third paragraph. " * 80        # ~1280 chars
    )
    out = split_text(text, chunk_size=1500, overlap=100)
    # At least one chunk should end at a paragraph boundary
    # (trailing whitespace allowed).
    assert any(
        chunk.rstrip().endswith("First paragraph.")
        or chunk.rstrip().endswith("Second paragraph.")
        for chunk in out
    )


def test_prefers_sentence_boundary_when_no_paragraph() -> None:
    text = "Sentence one. Sentence two. " * 80  # No paragraph breaks
    out = split_text(text, chunk_size=200, overlap=20)
    # Most cuts should fall at the `. ` boundary.
    sentence_cuts = sum(
        1 for c in out if c.rstrip().endswith(".") or c.rstrip().endswith("two")
    )
    assert sentence_cuts >= len(out) // 2


def test_word_boundary_fallback() -> None:
    """Long word-runs without sentence/paragraph markers still cut
    at word boundaries, not mid-word."""
    text = ("word " * 1000).strip()
    out = split_text(text, chunk_size=200, overlap=20)
    # No chunk ends mid-"word" — i.e. every chunk ends with a full
    # token followed by space or end-of-text.
    for chunk in out:
        # If the chunk isn't the last, it should not end with a
        # partial token. We check by ensuring the last char isn't
        # in the middle of a token-of-spaces-around: last token
        # should be either complete "word" or the empty string.
        assert chunk.endswith("word") or chunk.endswith("word ")


def test_no_chunk_exceeds_budget_plus_tolerance() -> None:
    text = "x" * 5000
    out = split_text(text, chunk_size=1000, overlap=100)
    for chunk in out:
        # Tolerance up to chunk_size + overlap (sentence stretch).
        assert len(chunk) <= 1000 + 100


def test_zero_overlap_no_repetition() -> None:
    text = "A" * 1500 + "B" * 1500
    out = split_text(text, chunk_size=1500, overlap=0)
    assert len(out) == 2
    assert out[0].startswith("A")
    assert out[1].startswith("B")
