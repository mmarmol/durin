"""VerdictHistory — ring-buffer of past deliberation verdicts with serialization."""

from __future__ import annotations

from collections import Counter
from typing import Any

from durin.deliberation.types import GeneratorRole, TriggerReason, VerdictEntry

_MAX_ENTRIES = 20


class VerdictHistory:
    """In-memory ring buffer of deliberation verdicts.

    Keeps at most _MAX_ENTRIES entries; oldest dropped on overflow.
    Serializable to/from plain dicts for session metadata persistence.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries: list[VerdictEntry] | None = None) -> None:
        self._entries: list[VerdictEntry] = list(entries or [])
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]

    def append(self, entry: VerdictEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]

    @property
    def entries(self) -> list[VerdictEntry]:
        return list(self._entries)

    @property
    def last(self) -> VerdictEntry | None:
        return self._entries[-1] if self._entries else None

    def __len__(self) -> int:
        return len(self._entries)

    def dominant_role(self, window: int = 5) -> GeneratorRole | None:
        """Most frequently winning role in the last `window` entries.

        Returns None if fewer than 3 entries exist in the window.
        """
        recent = self._entries[-window:]
        if len(recent) < 3:
            return None
        counts = Counter(e.winner_role for e in recent)
        role, count = counts.most_common(1)[0]
        if count >= 2:
            return role
        return None

    def role_distribution(self) -> dict[GeneratorRole, int]:
        return dict(Counter(e.winner_role for e in self._entries))

    def serialize(self) -> list[dict[str, Any]]:
        return [
            {
                "timestamp": e.timestamp,
                "trigger": e.trigger.value,
                "winner_role": e.winner_role.value,
                "winner_score": round(e.winner_score, 4),
                "threshold": round(e.threshold, 4),
                "under_doubt": e.under_doubt,
                "posture_snapshot": e.posture_snapshot,
                "synthesis_brief": e.synthesis_brief,
            }
            for e in self._entries
        ]

    @classmethod
    def deserialize(cls, data: list[dict[str, Any]]) -> VerdictHistory:
        entries = []
        for item in data:
            try:
                entries.append(VerdictEntry(
                    timestamp=item["timestamp"],
                    trigger=TriggerReason(item["trigger"]),
                    winner_role=GeneratorRole(item["winner_role"]),
                    winner_score=item["winner_score"],
                    threshold=item["threshold"],
                    under_doubt=item["under_doubt"],
                    posture_snapshot=item.get("posture_snapshot", {}),
                    synthesis_brief=item.get("synthesis_brief", ""),
                ))
            except (KeyError, ValueError):
                continue
        return cls(entries)
