"""Tests for the restructured MemoryStore — pure file I/O layer."""

import json
from datetime import datetime

import pytest

from durin.agent.memory import _HISTORY_ENTRY_HARD_CAP, MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def _entries(store) -> list[dict]:
    """Parsed history.jsonl records. The store is write-only, so tests that
    assert on what was persisted read the file back themselves."""
    text = store.read_file(store.history_file)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class TestMemoryStoreBasicIO:
    def test_read_soul_returns_empty_when_missing(self, store):
        assert store.read_soul() == ""

    def test_write_and_read_soul(self, store):
        store.write_soul("soul content")
        assert store.read_soul() == "soul content"


class TestHistoryWithCursor:
    def test_append_history_returns_cursor(self, store):
        cursor = store.append_history("event 1")
        assert cursor == 1
        cursor2 = store.append_history("event 2")
        assert cursor2 == 2

    def test_append_history_includes_cursor_in_file(self, store):
        store.append_history("event 1")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == 1

    def test_cursor_persists_across_appends(self, store):
        store.append_history("event 1")
        store.append_history("event 2")
        cursor = store.append_history("event 3")
        assert cursor == 3

    def test_append_history_strips_thinking_content(self, store):
        """`strip_think` must run before persistence — well-formed thinking
        blocks shouldn't land in history."""
        cursor = store.append_history("<think>reasoning</think>final answer")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == "final answer"

    def test_append_history_drops_pure_leak_content(self, store):
        """Regression: entries that strip down to empty (pure template-token
        leak) must NOT fall back to the raw leak. Persisting the raw text
        would re-pollute context via consolidation / replay, undoing the
        protection `strip_think` provides."""
        cursor = store.append_history("<think>nothing user-facing</think>")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == ""

    def test_append_history_drops_malformed_leak_prefix(self, store):
        """Channel-marker / malformed opening leaks should not survive."""
        cursor = store.append_history("<channel|>")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == ""

    def test_next_cursor_falls_back_when_last_entry_has_no_cursor(self, store):
        """Regression: _next_cursor should not KeyError on entries without cursor."""
        store.history_file.write_text(
            '{"timestamp": "2026-04-01 10:01", "content": "no cursor"}\n',
            encoding="utf-8",
        )
        # Delete .cursor file so _next_cursor falls back to reading JSONL
        store._cursor_file.unlink(missing_ok=True)
        # Last entry has no cursor — should safely return 1, not KeyError
        cursor = store.append_history("new event")
        assert cursor == 1

    def test_write_entries_uses_atomic_write(self, tmp_path):
        """_write_entries uses temp file + os.replace for atomicity."""
        store = MemoryStore(tmp_path)
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        entries = _entries(store)

        # Monitor temp file existence
        tmp_path_obj = store.history_file.with_suffix(".jsonl.tmp")
        assert not tmp_path_obj.exists()  # Should not exist initially

        # Call _write_entries
        store._write_entries(entries)

        # Temp file should be cleaned up
        assert not tmp_path_obj.exists()
        # Original file should exist
        assert store.history_file.exists()

    def test_write_entries_cleans_up_tmp_on_exception(self, tmp_path, monkeypatch):
        """Exception during _write_entries cleans up the temp file."""
        store = MemoryStore(tmp_path)
        store.append_history("event 1")
        entries = _entries(store)

        tmp_path_obj = store.history_file.with_suffix(".jsonl.tmp")

        # Mock os.replace to raise an exception
        def failing_replace(*args, **kwargs):
            raise RuntimeError("Simulated failure")

        monkeypatch.setattr('os.replace', failing_replace)

        with pytest.raises(RuntimeError):
            store._write_entries(entries)

        # Temp file should be cleaned up
        assert not tmp_path_obj.exists()

        # Original file should still exist (because replace failed)
        assert store.history_file.exists()


class TestAppendHistoryHardCap:
    """append_history has a defensive cap that catches new callers who forgot
    to set their own tighter cap. The default is intentionally larger than
    any current caller's per-call cap, so normal operation never trips it."""

    def test_oversized_entry_is_truncated(self, store):
        """An entry above _HISTORY_ENTRY_HARD_CAP is truncated before being persisted."""
        huge = "x" * (_HISTORY_ENTRY_HARD_CAP + 10_000)
        store.append_history(huge)
        entry = _entries(store)[0]
        assert len(entry["content"]) <= _HISTORY_ENTRY_HARD_CAP + 50

    def test_oversize_warning_is_emitted_once(self, store, caplog):
        """Repeated oversized writes should warn only on the first occurrence."""
        from loguru import logger as loguru_logger

        records: list[str] = []
        handler_id = loguru_logger.add(lambda m: records.append(m), level="WARNING")
        try:
            huge = "x" * (_HISTORY_ENTRY_HARD_CAP + 1)
            store.append_history(huge)
            store.append_history(huge)
            store.append_history(huge)
        finally:
            loguru_logger.remove(handler_id)

        oversize_warnings = [r for r in records if "exceeds" in r and "chars" in r]
        assert len(oversize_warnings) == 1

    def test_custom_max_chars_overrides_default(self, store):
        """Callers that pass max_chars should get their tighter cap applied."""
        store.append_history("a" * 500, max_chars=100)
        entry = _entries(store)[0]
        assert len(entry["content"]) <= 150  # 100 + "\n... (truncated)"

    def test_normal_sized_entries_unaffected(self, store):
        """The hard cap must not alter entries that fit within it."""
        msg = "normal short entry"
        store.append_history(msg)
        entry = _entries(store)[0]
        assert entry["content"] == msg


class TestLegacyHistoryMigration:
    def test_migrates_legacy_history_md_preserving_partial_entries(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_content = (
            "[2026-04-01 10:00] User prefers dark mode.\n\n"
            "[2026-04-01 10:05] [RAW] 2 messages\n"
            "[2026-04-01 10:04] USER: hello\n"
            "[2026-04-01 10:04] ASSISTANT: hi\n\n"
            "Legacy chunk without timestamp.\n"
            "Keep whatever content we can recover.\n"
        )
        legacy_file.write_text(legacy_content, encoding="utf-8")

        store = MemoryStore(tmp_path)
        fallback_timestamp = datetime.fromtimestamp(
            (memory_dir / "HISTORY.md.bak").stat().st_mtime,
        ).strftime("%Y-%m-%d %H:%M")

        entries = _entries(store)
        assert [entry["cursor"] for entry in entries] == [1, 2, 3]
        assert entries[0]["timestamp"] == "2026-04-01 10:00"
        assert entries[0]["content"] == "User prefers dark mode."
        assert entries[1]["timestamp"] == "2026-04-01 10:05"
        assert entries[1]["content"].startswith("[RAW] 2 messages")
        assert "USER: hello" in entries[1]["content"]
        assert entries[2]["timestamp"] == fallback_timestamp
        assert entries[2]["content"].startswith("Legacy chunk without timestamp.")
        assert store.read_file(store._cursor_file).strip() == "3"
        assert not legacy_file.exists()
        assert (memory_dir / "HISTORY.md.bak").read_text(encoding="utf-8") == legacy_content

    def test_migrates_consecutive_entries_without_blank_lines(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_content = (
            "[2026-04-01 10:00] First event.\n"
            "[2026-04-01 10:01] Second event.\n"
            "[2026-04-01 10:02] Third event.\n"
        )
        legacy_file.write_text(legacy_content, encoding="utf-8")

        store = MemoryStore(tmp_path)

        entries = _entries(store)
        assert len(entries) == 3
        assert [entry["content"] for entry in entries] == [
            "First event.",
            "Second event.",
            "Third event.",
        ]

    def test_raw_archive_stays_single_entry_while_following_events_split(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_content = (
            "[2026-04-01 10:05] [RAW] 2 messages\n"
            "[2026-04-01 10:04] USER: hello\n"
            "[2026-04-01 10:04] ASSISTANT: hi\n"
            "[2026-04-01 10:06] Normal event after raw block.\n"
        )
        legacy_file.write_text(legacy_content, encoding="utf-8")

        store = MemoryStore(tmp_path)

        entries = _entries(store)
        assert len(entries) == 2
        assert entries[0]["content"].startswith("[RAW] 2 messages")
        assert "USER: hello" in entries[0]["content"]
        assert entries[1]["content"] == "Normal event after raw block."

    def test_nonstandard_date_headers_still_start_new_entries(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_content = (
            "[2026-03-25–2026-04-02] Multi-day summary.\n[2026-03-26/27] Cross-day summary.\n"
        )
        legacy_file.write_text(legacy_content, encoding="utf-8")

        store = MemoryStore(tmp_path)
        fallback_timestamp = datetime.fromtimestamp(
            (memory_dir / "HISTORY.md.bak").stat().st_mtime,
        ).strftime("%Y-%m-%d %H:%M")

        entries = _entries(store)
        assert len(entries) == 2
        assert entries[0]["timestamp"] == fallback_timestamp
        assert entries[0]["content"] == "[2026-03-25–2026-04-02] Multi-day summary."
        assert entries[1]["timestamp"] == fallback_timestamp
        assert entries[1]["content"] == "[2026-03-26/27] Cross-day summary."

    def test_existing_history_jsonl_skips_legacy_migration(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        history_file = memory_dir / "history.jsonl"
        history_file.write_text(
            '{"cursor": 7, "timestamp": "2026-04-01 12:00", "content": "existing"}\n',
            encoding="utf-8",
        )
        legacy_file = memory_dir / "HISTORY.md"
        legacy_file.write_text("[2026-04-01 10:00] legacy\n\n", encoding="utf-8")

        store = MemoryStore(tmp_path)

        entries = _entries(store)
        assert len(entries) == 1
        assert entries[0]["cursor"] == 7
        assert entries[0]["content"] == "existing"
        assert legacy_file.exists()
        assert not (memory_dir / "HISTORY.md.bak").exists()

    def test_empty_history_jsonl_still_allows_legacy_migration(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        history_file = memory_dir / "history.jsonl"
        history_file.write_text("", encoding="utf-8")
        legacy_file = memory_dir / "HISTORY.md"
        legacy_file.write_text("[2026-04-01 10:00] legacy\n\n", encoding="utf-8")

        store = MemoryStore(tmp_path)

        entries = _entries(store)
        assert len(entries) == 1
        assert entries[0]["cursor"] == 1
        assert entries[0]["timestamp"] == "2026-04-01 10:00"
        assert entries[0]["content"] == "legacy"
        assert not legacy_file.exists()
        assert (memory_dir / "HISTORY.md.bak").exists()

    def test_migrates_legacy_history_with_invalid_utf8_bytes(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_file.write_bytes(b"[2026-04-01 10:00] Broken \xff data still needs migration.\n\n")

        store = MemoryStore(tmp_path)

        entries = _entries(store)
        assert len(entries) == 1
        assert entries[0]["timestamp"] == "2026-04-01 10:00"
        assert "Broken" in entries[0]["content"]
        assert "migration." in entries[0]["content"]
