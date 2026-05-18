"""Notification template rendering."""


_TEMPLATES = {
    "order_placed": "Your order #{order_id} has been placed successfully.",
    "order_shipped": "Great news! Order #{order_id} has shipped. Track: {tracking_url}",
    "password_changed": "Your password was changed. If this wasn't you, contact support.",
    "profile_updated": "Your profile has been updated successfully.",
}


def render_template(event_type: str, data: dict, channel: str) -> str:
    """Render a notification template with the given data."""
    template = _TEMPLATES.get(event_type, "You have a new notification.")
    try:
        return template.format(**data)
    except KeyError:
        return template
