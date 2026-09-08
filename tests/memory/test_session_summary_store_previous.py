from datetime import date, datetime
from pathlib import Path

from durin.memory.paths import memory_class_dir
from durin.memory.schema import MemoryEntry
from durin.memory.session_summary_store import (
    SESSION_SUMMARY_CLASS,
    closed_record_key,
    find_previous_session_summary,
    write_session_summary,
)
from durin.memory.storage import save_entry


def test_previous_summary_is_the_newest_other_session_on_the_channel(tmp_path: Path) -> None:
    write_session_summary(tmp_path, "websocket:old", "- decided to use X", last_active=date(2026, 9, 1))
    write_session_summary(tmp_path, "websocket:older", "- ancient", last_active=date(2026, 8, 1))
    write_session_summary(tmp_path, "slack:c1", "- slack stuff", last_active=date(2026, 9, 5))

    found = find_previous_session_summary(tmp_path, "websocket:new", channels={"websocket", "cli"})

    assert found is not None
    stem, text, last_active = found
    assert stem == "websocket_old"
    assert "decided to use X" in text
    assert last_active == date(2026, 9, 1)


def test_previous_summary_skips_own_key_and_other_channels(tmp_path: Path) -> None:
    write_session_summary(tmp_path, "websocket:new", "- mine", last_active=date(2026, 9, 6))
    write_session_summary(tmp_path, "slack:c1", "- slack", last_active=date(2026, 9, 5))

    assert find_previous_session_summary(tmp_path, "websocket:new", channels={"websocket"}) is None
    assert find_previous_session_summary(tmp_path, "slack:c2", channels={"websocket"}) is None


def test_channels_with_a_shared_sanitized_prefix_never_cross(tmp_path: Path) -> None:
    """`cli` and `cli_test` sanitize to stems that share the `cli_` prefix
    (`sanitize_session_key("cli_test:s1")` starts with the same `cli_` a
    prefix glob for channel `cli` would use). The exact match on the
    entry's own `source_refs` channel must keep them apart regardless."""
    write_session_summary(
        tmp_path, "cli_test:s1", "- cli_test stuff", last_active=date(2026, 9, 5),
        source_key="cli_test:s1",
    )

    assert find_previous_session_summary(tmp_path, "cli:new", channels={"cli", "cli_test"}) is None

    write_session_summary(
        tmp_path, "cli:old", "- cli stuff", last_active=date(2026, 9, 1),
        source_key="cli:old",
    )

    found = find_previous_session_summary(tmp_path, "cli:new", channels={"cli", "cli_test"})
    assert found is not None
    assert found[0] == "cli_old"

    found_other = find_previous_session_summary(tmp_path, "cli_test:new", channels={"cli", "cli_test"})
    assert found_other is not None
    assert found_other[0] == "cli_test_s1"


def test_glob_never_opens_files_outside_the_channel_prefix(
    tmp_path: Path, monkeypatch,
) -> None:
    """Important 3: the candidate glob must narrow to the sanitized channel
    prefix BEFORE any file is opened — a summary on an unrelated channel
    (a sanitized stem that does not start with the prefix) must never
    reach `load_entry`, not just be filtered out after being parsed. This
    is what keeps continuity off the O(every summary file) cost the
    review found on the prompt-build path."""
    write_session_summary(tmp_path, "cli:old", "- cli stuff", last_active=date(2026, 9, 1))
    write_session_summary(tmp_path, "slack:c1", "- slack stuff", last_active=date(2026, 9, 5))
    write_session_summary(tmp_path, "websocket:w1", "- ws stuff", last_active=date(2026, 9, 5))

    import durin.memory.session_summary_store as store_mod

    opened: list[Path] = []
    real_load_entry = store_mod.load_entry

    def _counting_load_entry(path: Path):
        opened.append(path)
        return real_load_entry(path)

    monkeypatch.setattr(store_mod, "load_entry", _counting_load_entry)

    found = find_previous_session_summary(
        tmp_path, "cli:new", channels={"cli", "slack", "websocket"},
    )

    assert found is not None
    assert found[0] == "cli_old"
    assert {p.stem for p in opened} == {"cli_old"}


def test_closed_record_of_another_key_on_the_channel_qualifies(tmp_path: Path) -> None:
    """`_archive_closed_session` writes the closed conversation under
    `closed_record_key` (a different file id than the original key), but
    tags it with the original key's `source_refs` so it still surfaces as
    the channel's previous session."""
    when = datetime(2026, 9, 5, 10, 0, 0)
    closed_key = closed_record_key("cli:old", when)
    write_session_summary(
        tmp_path, closed_key, "- closed conversation", last_active=when,
        source_key="cli:old",
    )

    found = find_previous_session_summary(tmp_path, "cli:new", channels={"cli"})

    assert found is not None
    assert found[0] == closed_key
    assert "closed conversation" in found[1]


def test_legacy_entry_without_source_refs_still_matches_by_prefix(tmp_path: Path) -> None:
    """An entry written without `source_key` (or by any path that predates
    `source_refs`) carries no `session:` ref; it falls back to the
    sanitized-prefix match on the file stem instead of being excluded."""
    write_session_summary(tmp_path, "cli:legacy", "- legacy summary", last_active=date(2026, 9, 1))

    found = find_previous_session_summary(tmp_path, "cli:new", channels={"cli"})

    assert found is not None
    assert found[0] == "cli_legacy"
    assert "legacy summary" in found[1]


def test_previous_summary_empty_directory_returns_none(tmp_path: Path) -> None:
    memory_class_dir(tmp_path, SESSION_SUMMARY_CLASS).mkdir(parents=True, exist_ok=True)

    assert find_previous_session_summary(tmp_path, "cli:new", channels={"cli"}) is None


def test_previous_summary_skips_an_unparsable_candidate(tmp_path: Path) -> None:
    directory = memory_class_dir(tmp_path, SESSION_SUMMARY_CLASS)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cli_broken.md").write_text("not a memory entry at all", encoding="utf-8")
    write_session_summary(
        tmp_path, "cli:old", "- good summary", last_active=date(2026, 9, 1),
        source_key="cli:old",
    )

    found = find_previous_session_summary(tmp_path, "cli:new", channels={"cli"})

    assert found is not None
    assert found[0] == "cli_old"


def test_previous_summary_prefers_a_dated_candidate_over_an_undated_one(tmp_path: Path) -> None:
    """`entry.valid_from or date.min` is the ranking fallback for a
    candidate with no `valid_from` at all — rare (hand-authored, or
    written outside `write_session_summary`'s always-defaulted date), but
    it must never outrank a candidate that actually has a date."""
    write_session_summary(
        tmp_path, "cli:dated", "- dated summary", last_active=date(2026, 9, 1),
        source_key="cli:dated",
    )
    undated_path = memory_class_dir(tmp_path, SESSION_SUMMARY_CLASS) / "cli_undated.md"
    save_entry(
        MemoryEntry(
            id="cli_undated", headline="undated", summary="- undated summary",
            body="- undated summary", source_refs=["session:cli:undated"], valid_from=None,
        ),
        undated_path,
    )

    found = find_previous_session_summary(tmp_path, "cli:new", channels={"cli"})

    assert found is not None
    assert found[0] == "cli_dated"
