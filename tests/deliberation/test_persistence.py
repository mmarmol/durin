"""Tests for deliberation persistence."""

from durin.deliberation.history import DeliberationHistory
from durin.deliberation.persistence import (
    DELIBERATION_HISTORY_KEY,
    restore_deliberation_history,
    save_deliberation_history,
)
from durin.deliberation.types import HistoryEntry


class TestPersistence:
    def test_save_and_restore(self):
        h = DeliberationHistory()
        h.append(HistoryEntry(
            timestamp=1.0,
            trigger="test",
            synthesis_brief="brief",
            perspectives_count=3,
            duration_ms=50.0,
            cycle=1,
        ))
        metadata: dict = {}
        save_deliberation_history(metadata, h)
        assert DELIBERATION_HISTORY_KEY in metadata

        restored = restore_deliberation_history(metadata)
        assert restored is not None
        assert len(restored) == 1
        assert restored.last.trigger == "test"

    def test_restore_missing(self):
        assert restore_deliberation_history({}) is None

    def test_restore_invalid(self):
        assert restore_deliberation_history({DELIBERATION_HISTORY_KEY: "bad"}) is None
