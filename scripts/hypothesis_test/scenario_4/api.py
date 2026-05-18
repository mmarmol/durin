"""User-facing REST API."""

from auth import check_authenticated, check_owner_or_admin
from models import find_user, save_user, delete_user_record


def get_user_profile(request: dict) -> dict:
    """GET /users/{id}/profile — returns the user's profile."""
    user_id = request["path_params"]["user_id"]

    if not check_authenticated(request):
        return {"status": 401, "body": {"error": "Not authenticated"}}

    user = find_user(user_id)
    if not user:
        return {"status": 404, "body": {"error": "Not found"}}

    return {"status": 200, "body": user}


def update_user_profile(request: dict) -> dict:
    """PATCH /users/{id}/profile — updates the user's profile."""
    user_id = request["path_params"]["user_id"]

    if not check_authenticated(request):
        return {"status": 401, "body": {"error": "Not authenticated"}}
    if not check_owner_or_admin(request, user_id):
        return {"status": 403, "body": {"error": "Forbidden"}}

    user = find_user(user_id)
    if not user:
        return {"status": 404, "body": {"error": "Not found"}}

    body = request.get("body", {})
    for k, v in body.items():
        if k in ("name", "email", "bio"):
            user[k] = v
    save_user(user)
    return {"status": 200, "body": user}
