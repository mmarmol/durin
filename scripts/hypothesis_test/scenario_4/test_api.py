"""Tests for the user API endpoints."""

from api import get_user_profile, update_user_profile


def _request(user_id: str, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return {
        "path_params": {"user_id": user_id},
        "headers": headers,
        "body": body or {},
    }


def test_get_profile_requires_auth():
    result = get_user_profile(_request("u_alice"))
    assert result["status"] == 401


def test_get_profile_authenticated_works():
    result = get_user_profile(_request("u_alice", token="tok_alice"))
    assert result["status"] == 200


def test_delete_user_requires_authentication():
    """An unauthenticated DELETE must return 401, not delete the user."""
    from api import delete_user  # endpoint to be added
    result = delete_user(_request("u_alice"))
    assert result["status"] == 401


def test_delete_user_forbids_non_owner():
    """A logged-in user cannot delete another user (unless admin)."""
    from api import delete_user
    # Bob tries to delete Alice
    result = delete_user(_request("u_alice", token="tok_bob"))
    assert result["status"] == 403


def test_delete_user_owner_succeeds():
    """A user can delete their own account."""
    from api import delete_user
    result = delete_user(_request("u_alice", token="tok_alice"))
    assert result["status"] in (200, 204)
