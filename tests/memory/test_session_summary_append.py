"""Session summaries accumulate across consolidations (bounded).

Overwrite semantics destroyed 23 of 24 span summaries in the
2026-07-17 incident. Blocks now append with oldest-block eviction.
"""
from __future__ import annotations

from pathlib import Path

from durin.memory.session_summary_store import (
    append_session_summary_block,
    get_session_summary,
)

KEY = "websocket:abc"


def test_append_accumulates_blocks(tmp_path: Path) -> None:
    append_session_summary_block(tmp_path, KEY, "- span one fact")
    append_session_summary_block(tmp_path, KEY, "- span two fact")
    text, _ = get_session_summary(tmp_path, KEY)
    assert "- span one fact" in text
    assert "- span two fact" in text
    assert text.index("span one") < text.index("span two")


def test_append_evicts_oldest_block_over_cap(tmp_path: Path) -> None:
    append_session_summary_block(tmp_path, KEY, "- old " + "x" * 100, max_chars=300)
    append_session_summary_block(tmp_path, KEY, "- mid " + "y" * 100, max_chars=300)
    append_session_summary_block(tmp_path, KEY, "- new " + "z" * 100, max_chars=300)
    text, _ = get_session_summary(tmp_path, KEY)
    assert "- old" not in text
    assert "- mid" in text and "- new" in text


def test_append_keeps_newest_block_even_if_alone_over_cap(tmp_path: Path) -> None:
    append_session_summary_block(tmp_path, KEY, "- huge " + "w" * 500, max_chars=100)
    text, _ = get_session_summary(tmp_path, KEY)
    assert "- huge" in text


def test_append_ignores_empty_and_nothing(tmp_path: Path) -> None:
    assert append_session_summary_block(tmp_path, KEY, "") is None
    assert append_session_summary_block(tmp_path, KEY, "(nothing)") is None
    assert get_session_summary(tmp_path, KEY) == (None, None)


def test_consecutive_duplicate_block_not_reappended(tmp_path: Path) -> None:
    append_session_summary_block(tmp_path, KEY, "- same fact")
    append_session_summary_block(tmp_path, KEY, "- same fact")
    text, _ = get_session_summary(tmp_path, KEY)
    assert text.count("- same fact") == 1


def test_evicted_block_paths_are_carried_forward(tmp_path: Path) -> None:
    b1 = (
        "- old fact\n"
        "Files/paths examined in this span (read_file to reopen): "
        "/ws/skills/zendesk/SKILL.md"
    )
    append_session_summary_block(tmp_path, KEY, b1, max_chars=400)
    append_session_summary_block(tmp_path, KEY, "- mid " + "y" * 200, max_chars=400)
    append_session_summary_block(tmp_path, KEY, "- new " + "z" * 200, max_chars=400)
    text, _ = get_session_summary(tmp_path, KEY)
    assert "- old fact" not in text
    assert "/ws/skills/zendesk/SKILL.md" in text
    assert "Files/paths from earlier spans (evicted):" in text
