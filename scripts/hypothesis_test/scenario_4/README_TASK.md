# Task: Add user delete endpoint

## Requirement
Add a `DELETE /users/{id}` endpoint to `api.py`. The endpoint should delete
the user record identified by the path parameter.

## Files in the user system
- api.py — REST API endpoints (currently has GET and PATCH for profiles)
- auth.py — authentication and authorization helpers
- models.py — user model and persistence (includes delete_user_record function)
