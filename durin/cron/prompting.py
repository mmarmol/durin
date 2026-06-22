"""Prompt framing for cron agent_turn jobs."""

_REMINDER_FRAMING = (
    "The scheduled time has arrived. Deliver this reminder to the user now, "
    "as a brief and natural message in their language. Speak directly to them — "
    "do not narrate progress, summarize, include user IDs, or add status reports "
    "like 'Done' or 'Reminded'.\n\n"
    "Reminder: {message}"
)


def build_cron_turn_prompt(mode: str, message: str) -> str:
    """Frame a cron job's message by mode.

    ``reminder`` wraps the message in user-facing delivery framing; ``task``
    passes the raw prompt so the agent does the work with full tools.
    """
    if mode == "task":
        return message
    return _REMINDER_FRAMING.format(message=message)
