"""Tests for error card detection in chat_view."""

from __future__ import annotations

from durin.cli.tui.widgets.chat_view import looks_like_error


def test_error_colon_detected():
    assert looks_like_error("Error: something went wrong")


def test_error_paren_detected():
    assert looks_like_error("Error (api_timeout): request failed")


def test_error_calling_detected():
    assert looks_like_error("Error calling provider: rate limited")


def test_plain_text_not_error():
    assert not looks_like_error("Here's a summary of the changes.")


def test_empty_not_error():
    assert not looks_like_error("")


def test_error_in_multiline_detected():
    text = "Processing...\nError: API key invalid\nPlease check config."
    assert looks_like_error(text)


def test_error_word_in_sentence_not_detected():
    assert not looks_like_error("There was an error in the user's input")
