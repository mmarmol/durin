"""Tests for verdict history persistence — save/restore from session metadata."""

from __future__ import annotations

import time

from durin.deliberation.history import VerdictHistory
from durin.deliberation.persistence import (
    VERDICT_HISTORY_KEY,
    restore_verdict_history,
    save_verdict_history,
)
from durin.deliberation.types import GeneratorRole, TriggerReason, VerdictEntry


def _entry(role=GeneratorRole.PRAGMATICO, score=0.7) -> VerdictEntry:
    return VerdictEntry(
        timestamp=time.time(),
        trigger=TriggerReason.PLANNING_MOMENT,
        winner_role=role,
        winner_score=score,
        threshold=0.55,
        under_doubt=False,
        posture_snapshot={"cautela": 0.6},
        synthesis_brief="usar JWT",
    )


class TestSaveRestore:
    def test_save_writes_to_metadata(self):
        metadata: dict = {}
        history = VerdictHistory()
        history.append(_entry())
        save_verdict_history(metadata, history)
        assert VERDICT_HISTORY_KEY in metadata
        assert isinstance(metadata[VERDICT_HISTORY_KEY], list)
        assert len(metadata[VERDICT_HISTORY_KEY]) == 1

    def test_restore_from_metadata(self):
        metadata: dict = {}
        history = VerdictHistory()
        history.append(_entry(GeneratorRole.EXPLORADOR, 0.65))
        history.append(_entry(GeneratorRole.CRITICO, 0.58))
        save_verdict_history(metadata, history)

        restored = restore_verdict_history(metadata)
        assert restored is not None
        assert len(restored) == 2
        assert restored.last.winner_role == GeneratorRole.CRITICO
        assert restored.last.winner_score == 0.58

    def test_restore_returns_none_when_missing(self):
        assert restore_verdict_history({}) is None

    def test_restore_returns_none_for_invalid_data(self):
        assert restore_verdict_history({VERDICT_HISTORY_KEY: "not a list"}) is None
        assert restore_verdict_history({VERDICT_HISTORY_KEY: 42}) is None

    def test_roundtrip_preserves_entries(self):
        metadata: dict = {}
        history = VerdictHistory()
        for i in range(5):
            history.append(_entry(score=0.5 + i * 0.05))
        save_verdict_history(metadata, history)
        restored = restore_verdict_history(metadata)
        assert len(restored) == 5
        assert restored.entries[0].winner_score == 0.5
        assert restored.entries[4].winner_score == 0.7

    def test_save_empty_history(self):
        metadata: dict = {}
        save_verdict_history(metadata, VerdictHistory())
        assert metadata[VERDICT_HISTORY_KEY] == []
        assert restore_verdict_history(metadata) is not None
        assert len(restore_verdict_history(metadata)) == 0
