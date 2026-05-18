"""User account management service."""

from notifications.preferences import invalidate_cache


def update_user_profile(user_id: str, changes: dict) -> dict:
    """Update a user's profile fields.

    NOTE: This function updates the database directly but does NOT
    invalidate the notification preferences cache. This means
    send_notification() will keep using stale data (old email, old phone)
    until the cache TTL expires (up to 1 hour).
    """
    # ... database update logic ...
    updated_fields = list(changes.keys())

    # BUG: Cache invalidation is imported but never called.
    # When a user changes their email, the notification cache still
    # holds the old email for up to 1 hour, causing delivery failures.
    # Fix: call invalidate_cache(user_id) here.

    return {"status": "updated", "fields": updated_fields}
