from datetime import date
from pathlib import Path
from types import SimpleNamespace

from durin.memory.artifact_recall import entities_derived_from, memory_notes_for_path
from durin.memory.field_patch import FieldPatch
from durin.memory.indexer import rebuild_fts_index
from durin.memory.memory_writer import write_entity
from durin.memory.session_summary_store import write_session_summary


def test_notes_list_entries_that_mention_the_path(tmp_path: Path) -> None:
    write_session_summary(
        tmp_path, "websocket:old",
        "- fixed the retry loop\nFiles/paths examined in this span (read_file to reopen): durin/agent/loop.py",
        last_active=date(2026, 9, 1),
    )
    write_session_summary(tmp_path, "websocket:other", "- unrelated work on the webui", last_active=date(2026, 9, 2))
    rebuild_fts_index(tmp_path)

    notes = memory_notes_for_path(tmp_path, "durin/agent/loop.py")

    assert len(notes) == 1
    assert notes[0].startswith("- memory/session_summary/websocket_old")
    assert "fixed the retry loop" in notes[0]


def test_notes_are_empty_without_an_index_or_a_mention(tmp_path: Path) -> None:
    assert memory_notes_for_path(tmp_path, "durin/agent/loop.py") == []
    write_session_summary(tmp_path, "websocket:old", "- nothing about files", last_active=date(2026, 9, 1))
    rebuild_fts_index(tmp_path)
    assert memory_notes_for_path(tmp_path, "durin/agent/loop.py") == []


def test_entities_derived_from_a_reference(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    write_entity(tmp_path, "topic:two-systems",
                 [FieldPatch(kind="derived_from", value="reference:thinking-fast-and-slow",
                             author="dream", source_ref="s", at=now)],
                 create=True, name="Two systems")
    write_entity(tmp_path, "topic:other",
                 [FieldPatch(kind="body_append", value="x", author="agent", source_ref="s", at=now)],
                 create=True, name="Other")

    assert entities_derived_from(tmp_path, "reference:thinking-fast-and-slow") == ["topic:two-systems"]


def test_notes_false_positive_common_filename_suffix(tmp_path: Path) -> None:
    """Summary mentioning src/app.py should not match a read of app.py (substring false positive)."""
    write_session_summary(
        tmp_path, "websocket:old",
        "- reviewed the app entry\nFiles/paths examined in this span (read_file to reopen): src/app.py",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)

    # Reading app.py (without src/) should not return the note about src/app.py.
    notes = memory_notes_for_path(tmp_path, "app.py")
    assert len(notes) == 0, f"Expected no notes for app.py, but got: {notes}"


def test_notes_match_with_dotslash_prefix(tmp_path: Path) -> None:
    """Summary mentioning ./src/app.py should match a read of src/app.py (prefix-aware)."""
    write_session_summary(
        tmp_path, "websocket:old",
        "- reviewed the app\nFiles/paths examined in this span (read_file to reopen): ./src/app.py",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)

    # Reading src/app.py should match because ./ is optional in the pattern.
    notes = memory_notes_for_path(tmp_path, "src/app.py")
    assert len(notes) == 1
    assert "reviewed the app" in notes[0]


def test_notes_match_a_path_that_ends_a_sentence(tmp_path: Path) -> None:
    """Prose ends sentences with a period; the path still has to match."""
    write_session_summary(
        tmp_path, "websocket:old",
        "- retry work\nWe reviewed durin/agent/loop.py.",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)

    notes = memory_notes_for_path(tmp_path, "durin/agent/loop.py")

    assert len(notes) == 1


def test_notes_do_not_match_an_extension_continuation(tmp_path: Path) -> None:
    """A period that continues an extension still blocks: loop.py != loop.py.bak."""
    write_session_summary(
        tmp_path, "websocket:old",
        "- backup work\nloop.py.bak was created",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)

    assert memory_notes_for_path(tmp_path, "loop.py") == []


def test_notes_do_not_match_a_longer_path_prefix(tmp_path: Path) -> None:
    """A longer path that ends with the read path is a different file."""
    write_session_summary(
        tmp_path, "websocket:old",
        "- entry point work\nWe reviewed durin/src/app.py today",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)

    assert memory_notes_for_path(tmp_path, "src/app.py") == []


def test_notes_render_the_line_that_mentions_the_path(tmp_path: Path) -> None:
    """A prose mention renders that sentence, not the entry headline."""
    write_session_summary(
        tmp_path, "websocket:old",
        "- session recap\nThe retry backoff lives in durin/agent/loop.py and is off by one.",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)

    notes = memory_notes_for_path(tmp_path, "durin/agent/loop.py")

    assert len(notes) == 1
    assert notes[0].endswith(
        "— The retry backoff lives in durin/agent/loop.py and is off by one."
    )


def test_notes_fall_back_to_the_headline_for_a_mechanical_trailer(tmp_path: Path) -> None:
    """The `; `-joined path trailer says nothing — render the headline instead."""
    write_session_summary(
        tmp_path, "websocket:old",
        "- fixed the retry loop after a long look at the backoff\n"
        "Files/paths examined in this span (read_file to reopen): durin/agent/loop.py",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)

    notes = memory_notes_for_path(tmp_path, "durin/agent/loop.py")

    assert len(notes) == 1
    assert "Files/paths examined" not in notes[0]
    assert notes[0].endswith("— - fixed the retry loop after a long look at")


def test_notes_cap_a_long_matched_line(tmp_path: Path) -> None:
    """One very long line cannot dominate the read result."""
    long_line = "we looked at durin/agent/loop.py " + ("and kept reading " * 40)
    write_session_summary(
        tmp_path, "websocket:old", f"- long recap\n{long_line}",
        last_active=date(2026, 9, 1),
    )
    rebuild_fts_index(tmp_path)

    notes = memory_notes_for_path(tmp_path, "durin/agent/loop.py")

    assert len(notes) == 1
    rendered = notes[0].split(" — ", 1)[1]
    assert len(rendered) == 200
    assert rendered.endswith("…")


def _config_with(monkeypatch, *, enabled: bool = True, max_notes: int = 3) -> None:
    """Point `load_config` at an artifact_recall section with these values."""
    cfg = SimpleNamespace(
        memory=SimpleNamespace(
            artifact_recall=SimpleNamespace(enabled=enabled, max_notes=max_notes),
        ),
    )
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)


def _two_matching_summaries(tmp_path: Path) -> None:
    write_session_summary(
        tmp_path, "websocket:old",
        "- first pass\nWe reviewed durin/agent/loop.py.",
        last_active=date(2026, 9, 1),
    )
    write_session_summary(
        tmp_path, "websocket:newer",
        "- second pass\nWe reviewed durin/agent/loop.py again.",
        last_active=date(2026, 9, 2),
    )
    rebuild_fts_index(tmp_path)


def test_disabled_config_returns_no_notes(tmp_path: Path, monkeypatch) -> None:
    _two_matching_summaries(tmp_path)
    _config_with(monkeypatch, enabled=False)

    assert memory_notes_for_path(tmp_path, "durin/agent/loop.py") == []


def test_max_notes_caps_the_result(tmp_path: Path, monkeypatch) -> None:
    _two_matching_summaries(tmp_path)
    _config_with(monkeypatch, max_notes=1)

    assert len(memory_notes_for_path(tmp_path, "durin/agent/loop.py")) == 1


def test_explicit_limit_overrides_max_notes(tmp_path: Path, monkeypatch) -> None:
    _two_matching_summaries(tmp_path)
    _config_with(monkeypatch, max_notes=3)

    assert len(memory_notes_for_path(tmp_path, "durin/agent/loop.py", limit=1)) == 1
    assert len(memory_notes_for_path(tmp_path, "durin/agent/loop.py")) == 2


def test_an_unreadable_entity_page_does_not_drop_the_others(tmp_path: Path) -> None:
    """One page that cannot be read is skipped; the scan keeps going."""
    from datetime import datetime, timezone

    write_entity(tmp_path, "topic:two-systems",
                 [FieldPatch(kind="derived_from", value="reference:thinking-fast-and-slow",
                             author="dream", source_ref="s",
                             at=datetime.now(timezone.utc))],
                 create=True, name="Two systems")
    # Sorts before "two-systems", so the scan hits it first.
    broken = tmp_path / "memory" / "entities" / "topic" / "broken.md"
    broken.write_bytes(b"---\ntype: topic\nname: \xff\xfe broken\n---\n")

    assert entities_derived_from(tmp_path, "reference:thinking-fast-and-slow") == [
        "topic:two-systems",
    ]
