"""Notification sender module."""

from notifications.templates import render_template
from notifications.preferences import get_user_preferences


async def send_notification(user_id: str, event_type: str, data: dict) -> dict:
    """Send a notification to a user based on their preferences."""
    prefs = get_user_preferences(user_id)

    if not prefs.get("notifications_enabled", True):
        return {"status": "skipped", "reason": "disabled"}

    channel = prefs.get("preferred_channel", "email")
    template = render_template(event_type, data, channel)

    if channel == "email":
        return await _send_email(prefs["email"], template)
    elif channel == "sms":
        return await _send_sms(prefs["phone"], template)
    elif channel == "push":
        return await _send_push(prefs["device_token"], template)

    return {"status": "error", "reason": f"Unknown channel: {channel}"}


async def _send_email(to: str, body: str) -> dict:
    """Send email notification. Raises ValueError if recipient is invalid."""
    if not to or "@" not in to:
        raise ValueError(f"Invalid email recipient: {to}")
    # ... actual sending logic ...
    return {"status": "sent", "channel": "email", "to": to}


async def _send_sms(to: str, body: str) -> dict:
    if not to:
        raise ValueError("Missing phone number")
    return {"status": "sent", "channel": "sms", "to": to}


async def _send_push(token: str, body: str) -> dict:
    if not token:
        raise ValueError("Missing device token")
    return {"status": "sent", "channel": "push", "token": token}
