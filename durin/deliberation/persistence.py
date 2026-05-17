"""Verdict history session persistence."""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping

from durin.deliberation.history import VerdictHistory

VERDICT_HISTORY_KEY = "verdict_history"


def save_verdict_history(
    metadata: MutableMapping[str, Any],
    history: VerdictHistory,
) -> None:
    metadata[VERDICT_HISTORY_KEY] = history.serialize()


def restore_verdict_history(
    metadata: Mapping[str, Any],
) -> VerdictHistory | None:
    data = metadata.get(VERDICT_HISTORY_KEY)
    if not isinstance(data, list):
        return None
    return VerdictHistory.deserialize(data)
