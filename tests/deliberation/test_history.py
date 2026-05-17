"""Tests for deliberation history ring buffer."""

from durin.deliberation.history import DeliberationHistory, _MAX_ENTRIES
from durin.deliberation.types import HistoryEntry


def _entry(trigger: str = "test", cycle: int = 1) -> HistoryEntry:
    return HistoryEntry(
        timestamp=1.0,
        trigger=trigger,
        synthesis_brief="brief",
        perspectives_count=3,
        duration_ms=100.0,
        cycle=cycle,
    )


class TestDeliberationHistory:
    def test_append(self):
        h = DeliberationHistory()
        h.append(_entry())
        assert len(h) == 1
        assert h.last.trigger == "test"

    def test_ring_buffer(self):
        entries = [_entry(trigger=f"t{i}") for i in range(_MAX_ENTRIES + 5)]
        h = DeliberationHistory(entries)
        assert len(h) == _MAX_ENTRIES
        assert h.entries[0].trigger == "t5"

    def test_serialize_deserialize(self):
        h = DeliberationHistory()
        h.append(_entry(trigger="a", cycle=2))
        data = h.serialize()
        h2 = DeliberationHistory.deserialize(data)
        assert len(h2) == 1
        assert h2.last.trigger == "a"
        assert h2.last.cycle == 2

    def test_empty(self):
        h = DeliberationHistory()
        assert len(h) == 0
        assert h.last is None
        assert h.entries == []
