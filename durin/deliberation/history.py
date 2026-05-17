"""Deliberation history — ring-buffer of past deliberations."""

from __future__ import annotations

from typing import Any

from durin.deliberation.types import HistoryEntry

_MAX_ENTRIES = 20


class DeliberationHistory:
    """In-memory ring buffer of deliberation entries."""

    __slots__ = ("_entries",)

    def __init__(self, entries: list[HistoryEntry] | None = None) -> None:
        self._entries: list[HistoryEntry] = list(entries or [])
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]

    def append(self, entry: HistoryEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]

    @property
    def entries(self) -> list[HistoryEntry]:
        return list(self._entries)

    @property
    def last(self) -> HistoryEntry | None:
        return self._entries[-1] if self._entries else None

    def __len__(self) -> int:
        return len(self._entries)

    def serialize(self) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": e.timestamp,
                "trigger": e.trigger,
                "synthesis_brief": e.synthesis_brief,
                "perspectives_count": e.perspectives_count,
                "duration_ms": round(e.duration_ms, 1),
                "cycle": e.cycle,
            }
            for e in self._entries
        ]

    @classmethod
    def deserialize(cls, data: list[dict[str, Any]]) -> "DeliberationHistory":
        entries = []
        for item in data:
            try:
                entries.append(HistoryEntry(
                    timestamp=item["timestamp"],
                    trigger=item["trigger"],
                    synthesis_brief=item.get("synthesis_brief", ""),
                    perspectives_count=item.get("perspectives_count", 3),
                    duration_ms=item.get("duration_ms", 0.0),
                    cycle=item.get("cycle", 1),
                ))
            except (KeyError, ValueError):
                continue
        return cls(entries)
