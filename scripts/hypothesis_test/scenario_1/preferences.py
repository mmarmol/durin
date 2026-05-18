"""User notification preferences with caching layer."""

import time

_cache: dict[str, dict] = {}
_CACHE_TTL = 3600  # 1 hour


def get_user_preferences(user_id: str) -> dict:
    """Get user notification preferences. Uses in-memory cache with TTL."""
    cached = _cache.get(user_id)
    if cached and time.time() - cached["ts"] < _CACHE_TTL:
        return cached["prefs"]

    prefs = _fetch_from_db(user_id)
    _cache[user_id] = {"prefs": prefs, "ts": time.time()}
    return prefs


def invalidate_cache(user_id: str) -> None:
    """Remove a user's cached preferences."""
    _cache.pop(user_id, None)


def _fetch_from_db(user_id: str) -> dict:
    """Fetch preferences from database (simulated)."""
    # In real code, this queries the users table
    return {
        "notifications_enabled": True,
        "preferred_channel": "email",
        "email": f"user_{user_id}@example.com",
        "phone": "+1555000" + user_id[-4:],
        "device_token": f"tok_{user_id}",
    }
