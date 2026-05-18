"""User model and persistence."""

_users: dict[str, dict] = {
    "u_alice": {"id": "u_alice", "name": "Alice", "email": "alice@example.com", "role": "user"},
    "u_bob": {"id": "u_bob", "name": "Bob", "email": "bob@example.com", "role": "user"},
    "u_admin": {"id": "u_admin", "name": "Admin", "email": "admin@example.com", "role": "admin"},
}
_sessions: dict[str, str] = {
    "tok_alice": "u_alice",
    "tok_bob": "u_bob",
    "tok_admin": "u_admin",
}


def find_user(user_id: str) -> dict | None:
    return _users.get(user_id)


def find_user_by_session(token: str) -> dict | None:
    user_id = _sessions.get(token)
    if not user_id:
        return None
    return _users.get(user_id)


def save_user(user: dict) -> None:
    _users[user["id"]] = user


def delete_user_record(user_id: str) -> bool:
    """Hard-delete a user from the database. Returns True if a row was deleted."""
    if user_id in _users:
        del _users[user_id]
        return True
    return False
