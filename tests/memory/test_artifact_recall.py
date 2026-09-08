from datetime import date
from pathlib import Path

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
