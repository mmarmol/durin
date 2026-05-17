"""Deliberation history session persistence."""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping

from durin.deliberation.history import DeliberationHistory

DELIBERATION_HISTORY_KEY = "deliberation_history"


def save_deliberation_history(
    metadata: MutableMapping[str, Any],
    history: DeliberationHistory,
) -> None:
    metadata[DELIBERATION_HISTORY_KEY] = history.serialize()


def restore_deliberation_history(
    metadata: Mapping[str, Any],
) -> DeliberationHistory | None:
    data = metadata.get(DELIBERATION_HISTORY_KEY)
    if not isinstance(data, list):
        return None
    return DeliberationHistory.deserialize(data)
