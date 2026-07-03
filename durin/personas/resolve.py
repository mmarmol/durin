"""Resolve the active persona NAME for a turn.

Effective precedence: cron-job override > per-conversation
(session.metadata["persona"]) > per-chat channel config
(channels.<name>.chat_personas[chat_id]) > per-channel default
(channels.<name>.persona) > global default (agents.defaults.persona).

The channel arms let each transport carry its own identity (a work persona on
Slack, a personal one on Telegram) and let multi-room channels refine it per
conversation (e.g. a Slack workspace channel mapped to an ops persona)."""
from __future__ import annotations

from typing import Any, Mapping


def resolve_active_persona_name(
    config: Any,
    session_metadata: Mapping[str, Any] | None,
    cron_persona: str | None,
    channel: str | None = None,
    chat_id: str | None = None,
) -> str | None:
    if cron_persona:
        return cron_persona
    if session_metadata and session_metadata.get("persona"):
        return session_metadata["persona"]
    channel_persona = _channel_persona(config, channel, chat_id)
    if channel_persona:
        return channel_persona
    try:
        return config.agents.defaults.persona
    except AttributeError:
        return None


def _channel_persona(config: Any, channel: str | None, chat_id: str | None) -> str | None:
    """Per-chat mapping first, then the channel-wide default.

    Channel sections are stored as extra fields on ``ChannelsConfig`` (plain
    dicts) but plugins may model them, so both access styles are handled.
    """
    if not channel:
        return None
    try:
        section = getattr(config.channels, channel, None)
    except AttributeError:
        return None
    if section is None:
        return None

    def get(key: str) -> Any:
        if isinstance(section, dict):
            return section.get(key)
        return getattr(section, key, None)

    chat_map = get("chat_personas")
    if chat_id and isinstance(chat_map, Mapping):
        mapped = chat_map.get(str(chat_id))
        if mapped:
            return str(mapped)
    persona = get("persona")
    return str(persona) if persona else None
