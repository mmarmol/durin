"""Session summaries accumulate across consolidations (bounded).

Overwrite semantics destroyed 23 of 24 span summaries in the
2026-07-17 incident. Blocks now append with oldest-block eviction.
"""
from __future__ import annotations

from pathlib import Path

from durin.memory.session_summary_store import (
    append_session_summary_block,
    get_session_summary,
    session_summary_path,
)
from durin.memory.storage import load_entry

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


def test_headline_derives_from_newest_block_after_eviction(tmp_path: Path) -> None:
    b1 = (
        "- old fact\n"
        "Files/paths examined in this span (read_file to reopen): /ws/a.md"
    )
    append_session_summary_block(tmp_path, KEY, b1, max_chars=300)
    append_session_summary_block(tmp_path, KEY, "- mid " + "y" * 150, max_chars=300)
    append_session_summary_block(
        tmp_path, KEY, "- newest important fact " + "z" * 150, max_chars=300,
    )
    entry = load_entry(session_summary_path(tmp_path, KEY))
    assert entry.headline.startswith("- newest important fact")
    assert "evicted" not in entry.headline


def test_carried_line_drops_whole_entries_not_mid_path(tmp_path: Path) -> None:
    # 30 spans, each evicting one path-bearing block, accumulates well
    # over _EVICTED_PATHS_MAX_CHARS (1_200) worth of raw carried paths —
    # enough to force the carried line to drop whole entries.
    for i in range(30):
        block = (
            f"- fact {i}\n"
            "Files/paths examined in this span (read_file to reopen): "
            f"/workspace/some/long/descriptive/path/segment_{i:03d}/file.md"
        )
        append_session_summary_block(tmp_path, KEY, block, max_chars=300)
    text, _ = get_session_summary(tmp_path, KEY)
    first_block = text.split("\n\n---\n", 1)[0]
    assert first_block.startswith("Files/paths from earlier spans (evicted): ")
    assert len(first_block) <= 1_200
    _, _, tail = first_block.partition(": ")
    paths = tail.split("; ")
    assert paths, "expected at least one carried path"
    for path in paths:
        assert path.startswith("/workspace/"), f"fragment: {path!r}"
        assert path.endswith("/file.md"), f"fragment: {path!r}"
    # oldest carried paths were dropped whole (not raw-sliced); the
    # newest evicted path survives.
    assert "segment_000" not in tail
    assert "segment_027" in tail
