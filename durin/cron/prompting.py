"""Prompt framing for cron agent_turn jobs."""

from __future__ import annotations

import time
from datetime import datetime

_REMINDER_FRAMING = (
    "The scheduled time has arrived. Deliver this reminder to the user now, "
    "as a brief and natural message in their language. Speak directly to them — "
    "do not narrate progress, summarize, include user IDs, or add status reports "
    "like 'Done' or 'Reminded'.\n\n"
    "Reminder: {message}"
)

# A run starting this long after its scheduled time is told so. Below it the
# lag is ordinary timer slack (the tick sleeps up to five minutes) and saying
# "late" would be noise.
LATE_NOTE_THRESHOLD_MS = 2 * 60 * 1000


def build_cron_turn_prompt(
    mode: str,
    message: str,
    *,
    scheduled_at_ms: int | None = None,
    now_ms: int | None = None,
) -> str:
    """Frame a cron job's message by mode.

    ``reminder`` wraps the message in user-facing delivery framing; ``task``
    passes the raw prompt so the agent does the work with full tools. When
    ``scheduled_at_ms`` is well in the past (a one-shot that came due while
    the gateway was not running and fires late), a note says how late the
    run is, so a reminder can say so instead of pretending it is on time.
    """
    body = message if mode == "task" else _REMINDER_FRAMING.format(message=message)
    note = _late_note(scheduled_at_ms, now_ms)
    return f"{body}\n\n{note}" if note else body


def _late_note(scheduled_at_ms: int | None, now_ms: int | None) -> str:
    if not scheduled_at_ms:
        return ""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    late_ms = now - scheduled_at_ms
    if late_ms < LATE_NOTE_THRESHOLD_MS:
        return ""
    minutes = late_ms // 60_000
    when = datetime.fromtimestamp(scheduled_at_ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M")
    return (
        f"Note: this run was scheduled for {when} and is starting {minutes} min late. "
        "If you deliver a reminder, say that it is late."
    )
