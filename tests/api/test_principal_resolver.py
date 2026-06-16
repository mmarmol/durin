"""Tests for resolve_principal_from_headers (SP4)."""

from __future__ import annotations

import pytest

from durin.api.asgi import resolve_principal_from_headers
from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import AuthService
from durin.service.principal import Scope


@pytest.fixture()
def store(tmp_path):
    """ApiTokenStore backed by a tmp dir."""
    return ApiTokenStore(path=tmp_path / "tokens.json")


@pytest.fixture()
def auth(store):
    return AuthService(store=store)


class _FakeHeaders(dict):
    """Minimal headers shim that lowercases key lookups like Starlette does."""

    def get(self, key, default=""):
        return super().get(key.lower(), default)


def _headers(**kw):
    return _FakeHeaders({k.lower(): v for k, v in kw.items()})


# ---------------------------------------------------------------------------
# No credential → None
# ---------------------------------------------------------------------------


def test_no_auth_header_returns_none(auth):
    assert resolve_principal_from_headers(_headers(), auth=auth) is None


def test_non_bearer_scheme_returns_none(auth):
    assert resolve_principal_from_headers(
        _headers(authorization="Basic dXNlcjpwYXNz"), auth=auth
    ) is None


# ---------------------------------------------------------------------------
# Persisted token
# ---------------------------------------------------------------------------


def test_valid_persisted_token_resolves(auth, store):
    _id, plaintext = store.issue([Scope.SECRETS_READ.value], label="test")
    principal = resolve_principal_from_headers(
        _headers(authorization=f"Bearer {plaintext}"), auth=auth
    )
    assert principal is not None
    assert principal.has_scope(Scope.SECRETS_READ)
    assert principal.kind == "remote"


def test_unknown_token_returns_none(auth):
    principal = resolve_principal_from_headers(
        _headers(authorization="Bearer not-a-real-token"), auth=auth
    )
    assert principal is None


# ---------------------------------------------------------------------------
# Static token fallback
# ---------------------------------------------------------------------------


def test_static_token_grants_admin(auth):
    principal = resolve_principal_from_headers(
        _headers(authorization="Bearer mystatictoken"),
        auth=auth,
        static_token="mystatictoken",
    )
    assert principal is not None
    assert principal.has_scope(Scope.ADMIN)
    assert principal.subject == "static"


def test_static_token_not_accepted_when_empty(auth):
    """An empty static_token must NOT be accepted (would be a security hole)."""
    principal = resolve_principal_from_headers(
        _headers(authorization="Bearer "), auth=auth, static_token=""
    )
    assert principal is None


def test_wrong_static_token_returns_none(auth):
    principal = resolve_principal_from_headers(
        _headers(authorization="Bearer wrongtoken"),
        auth=auth,
        static_token="correcttoken",
    )
    assert principal is None


def test_static_token_not_used_when_persisted_token_matches(auth, store):
    """Persisted token resolution takes priority over the static fallback."""
    _id, plaintext = store.issue([Scope.SECRETS_READ.value])
    principal = resolve_principal_from_headers(
        _headers(authorization=f"Bearer {plaintext}"),
        auth=auth,
        static_token=plaintext,  # same string — persisted path wins
    )
    assert principal is not None
    # A persisted token has the issued scopes, not ADMIN.
    assert Scope.SECRETS_READ.value in principal.scopes
