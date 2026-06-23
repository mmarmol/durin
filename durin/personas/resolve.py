"""Resolve the active persona NAME for a turn by precedence:
per-job (cron) > per-conversation (session metadata) > global default."""
from __future__ import annotations

from typing import Any, Mapping


def resolve_active_persona_name(
    config: Any,
    session_metadata: Mapping[str, Any] | None,
    cron_persona: str | None,
) -> str | None:
    if cron_persona:
        return cron_persona
    if session_metadata and session_metadata.get("persona"):
        return session_metadata["persona"]
    try:
        return config.agents.defaults.persona
    except AttributeError:
        return None
