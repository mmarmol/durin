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


def test_append_unions_entities_and_topics_across_blocks(tmp_path: Path) -> None:
    """Each span contributes its own tags; the entry accumulates the union
    so a summary stays searchable by anything any of its spans mentioned.
    Order is recency, not alphabetical: a tag moves to the end whether it's
    new or a reconfirmation of one already there — "search" repeats in span
    two, so it ends up after "dream", not before it — the ordering the cap
    (see the next test) truncates from the front of."""
    append_session_summary_block(
        tmp_path, KEY, "- span one",
        entities=["project:durin"], topics=["search"],
    )
    append_session_summary_block(
        tmp_path, KEY, "- span two",
        entities=["person:marcelo"], topics=["dream", "search"],
    )
    entry = load_entry(session_summary_path(tmp_path, KEY))
    assert entry.entities == ["project:durin", "person:marcelo"]
    assert entry.topics == ["dream", "search"]


def test_append_records_new_tags_when_the_block_repeats(tmp_path: Path) -> None:
    """A degraded LLM repeating the newest block still contributes its tags —
    the duplicate-block short circuit must not swallow them."""
    append_session_summary_block(tmp_path, KEY, "- same fact", topics=["search"])
    append_session_summary_block(tmp_path, KEY, "- same fact", topics=["dream"])
    text, _ = get_session_summary(tmp_path, KEY)
    entry = load_entry(session_summary_path(tmp_path, KEY))
    assert text.count("- same fact") == 1
    assert entry.topics == ["search", "dream"]


def test_append_caps_entities_keeping_the_most_recent(tmp_path: Path) -> None:
    """The union accumulates for the life of the key (see the test above) —
    left unbounded it would eventually starve `_embed_text`'s tag-line
    budget and flood the rendered `Entities:` tail. 40 spans, one new
    entity ref each, push well past the 24-entity cap: only the 24 most
    recently seen survive, oldest dropped from the front."""
    for i in range(40):
        append_session_summary_block(
            tmp_path, KEY, f"- span {i}", entities=[f"person:p{i:02d}"],
        )
    entry = load_entry(session_summary_path(tmp_path, KEY))
    assert len(entry.entities) == 24
    assert entry.entities == [f"person:p{i:02d}" for i in range(16, 40)]


def test_append_caps_topics_keeping_the_most_recent(tmp_path: Path) -> None:
    """Same cap, the topics side: bound is 12, tighter than entities'
    24 because topic labels are meant to stay a short, high-signal set."""
    for i in range(20):
        append_session_summary_block(
            tmp_path, KEY, f"- span {i}", topics=[f"topic-{i:02d}"],
        )
    entry = load_entry(session_summary_path(tmp_path, KEY))
    assert len(entry.topics) == 12
    assert entry.topics == [f"topic-{i:02d}" for i in range(8, 20)]


def test_reconfirmed_tag_moves_to_the_newest_position_and_survives_the_cap(tmp_path: Path) -> None:
    """A tag a later span keeps mentioning is truly the most recent one —
    it must move to the end, not stay pinned at its first-seen position, or
    a long-lived key could evict a tag every span reconfirms while keeping
    ones nothing has mentioned since. "old" is first seen in span 0 and
    reconfirmed right before the cap (12 topics) forces an eviction; only
    "t00", never reconfirmed, is old enough to be the one dropped."""
    append_session_summary_block(tmp_path, KEY, "- span 0", topics=["old"])
    for i in range(11):
        append_session_summary_block(tmp_path, KEY, f"- span {i + 1}", topics=[f"t{i:02d}"])
    entry = load_entry(session_summary_path(tmp_path, KEY))
    assert len(entry.topics) == 12
    assert "old" in entry.topics

    append_session_summary_block(tmp_path, KEY, "- reconfirm", topics=["old", "t11"])

    entry = load_entry(session_summary_path(tmp_path, KEY))
    assert len(entry.topics) == 12
    assert "old" in entry.topics       # reconfirmed just now — survives
    assert "t00" not in entry.topics   # oldest, never reconfirmed — evicted
    assert entry.topics[-2:] == ["old", "t11"]


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


def test_append_holds_the_file_lock(tmp_path: Path, monkeypatch) -> None:
    """The read-rebuild-rewrite has two cross-process writers — the compactor
    in the gateway and the nightly pass in the dream worker — so it runs under
    the summary file's lock or one of them loses a block."""
    import contextlib

    import durin.memory.session_summary_store as store

    entered: list[Path] = []

    @contextlib.contextmanager
    def _recording_lock(target, **kwargs):
        entered.append(target)
        yield

    monkeypatch.setattr(store, "cross_process_lock", _recording_lock)

    store.append_session_summary_block(tmp_path, KEY, "- span one fact")

    assert entered == [session_summary_path(tmp_path, KEY)]
