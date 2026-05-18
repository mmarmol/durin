"""Authentication and authorization helpers.

All authenticated endpoints must call check_authenticated.
All endpoints that modify a specific user's data must call check_owner_or_admin.
"""

from models import find_user_by_session


def check_authenticated(request: dict) -> bool:
    """Returns True if the request has a valid session token."""
    token = request.get("headers", {}).get("authorization", "")
    if not token.startswith("Bearer "):
        return False
    session_token = token[len("Bearer "):]
    return find_user_by_session(session_token) is not None


def get_current_user(request: dict) -> dict | None:
    """Returns the current user from the request session, or None."""
    token = request.get("headers", {}).get("authorization", "")
    if not token.startswith("Bearer "):
        return None
    session_token = token[len("Bearer "):]
    return find_user_by_session(session_token)


def check_owner_or_admin(request: dict, target_user_id: str) -> bool:
    """Returns True if the requester is the target user OR is an admin."""
    current = get_current_user(request)
    if not current:
        return False
    return current["id"] == target_user_id or current.get("role") == "admin"
