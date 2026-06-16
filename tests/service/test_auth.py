"""Tests for AuthService (durin/service/auth.py)."""

from __future__ import annotations

import pytest

from durin.security.api_tokens import ApiTokenStore
from durin.service.auth import (
    AuthService,
    IssueTokenCommand,
    ListTokensQuery,
    RevokeTokenCommand,
)
from durin.service.principal import Principal, Scope
from durin.service.types import ForbiddenError, NotFoundError


@pytest.fixture()
def svc(tmp_path):
    store = ApiTokenStore(path=tmp_path / "api_tokens.json")
    return AuthService(store=store)


@pytest.fixture()
def admin():
    return Principal.local()


@pytest.fixture()
def read_only():
    return Principal.remote("t", frozenset({Scope.SYSTEM_READ.value}))


@pytest.fixture()
def no_scope():
    return Principal.remote("t", frozenset())


# ---------------------------------------------------------------------------
# issue_token
# ---------------------------------------------------------------------------


async def test_issue_returns_token_id_and_plaintext(svc, admin):
    cmd = IssueTokenCommand(scopes=["secrets:read"], label="test")
    result = await svc.issue_token(cmd, admin)
    assert result.token_id
    assert result.token.startswith("nbwt_")
    assert result.scopes == ["secrets:read"]


async def test_issue_requires_system_write(svc, read_only, no_scope):
    cmd = IssueTokenCommand(scopes=["admin"])
    for principal in (read_only, no_scope):
        with pytest.raises(ForbiddenError):
            await svc.issue_token(cmd, principal)


async def test_issue_sets_expires_at_when_ttl_given(svc, admin):
    cmd = IssueTokenCommand(scopes=["admin"], ttl_s=3600.0)
    result = await svc.issue_token(cmd, admin)
    assert result.expires_at is not None


async def test_issue_no_ttl_expires_at_is_none(svc, admin):
    cmd = IssueTokenCommand(scopes=["admin"])
    result = await svc.issue_token(cmd, admin)
    assert result.expires_at is None


# ---------------------------------------------------------------------------
# list_tokens
# ---------------------------------------------------------------------------


async def test_list_empty_initially(svc, admin):
    result = await svc.list_tokens(ListTokensQuery(), admin)
    assert result.tokens == []


async def test_list_shows_issued_token_metadata(svc, admin):
    await svc.issue_token(IssueTokenCommand(scopes=["settings:read"], label="bot"), admin)
    result = await svc.list_tokens(ListTokensQuery(), admin)
    assert len(result.tokens) == 1
    t = result.tokens[0]
    assert t.label == "bot"
    assert "settings:read" in t.scopes
    # hash and plaintext must not appear
    blob = result.model_dump_json()
    assert "hash" not in blob
    assert "nbwt_" not in blob


async def test_list_requires_system_read(svc, no_scope):
    with pytest.raises(ForbiddenError):
        await svc.list_tokens(ListTokensQuery(), no_scope)


async def test_list_allowed_with_system_read_scope(svc, read_only):
    result = await svc.list_tokens(ListTokensQuery(), read_only)
    assert isinstance(result.tokens, list)


# ---------------------------------------------------------------------------
# revoke_token
# ---------------------------------------------------------------------------


async def test_revoke_removes_token(svc, admin):
    issue_result = await svc.issue_token(IssueTokenCommand(scopes=["admin"]), admin)
    revoke_result = await svc.revoke_token(
        RevokeTokenCommand(token_id=issue_result.token_id), admin
    )
    assert revoke_result.ok is True
    listed = await svc.list_tokens(ListTokensQuery(), admin)
    assert listed.tokens == []


async def test_revoke_nonexistent_raises_not_found(svc, admin):
    with pytest.raises(NotFoundError):
        await svc.revoke_token(RevokeTokenCommand(token_id="deadbeef"), admin)


async def test_revoke_requires_system_write(svc, admin, read_only):
    issue_result = await svc.issue_token(IssueTokenCommand(scopes=["admin"]), admin)
    with pytest.raises(ForbiddenError):
        await svc.revoke_token(
            RevokeTokenCommand(token_id=issue_result.token_id), read_only
        )


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


async def test_resolve_valid_token_builds_principal(svc, admin):
    issue_result = await svc.issue_token(
        IssueTokenCommand(scopes=["secrets:read", "settings:read"]), admin
    )
    principal = svc.resolve(issue_result.token)
    assert principal is not None
    assert principal.kind == "remote"
    assert principal.subject == issue_result.token_id
    assert "secrets:read" in principal.scopes
    assert "settings:read" in principal.scopes


async def test_resolve_bad_token_returns_none(svc):
    assert svc.resolve("nbwt_notavalidtoken") is None


async def test_resolve_scoped_principal_enforces_scope(svc, admin):
    issue_result = await svc.issue_token(
        IssueTokenCommand(scopes=["secrets:read"]), admin
    )
    principal = svc.resolve(issue_result.token)
    assert principal is not None
    assert principal.has_scope(Scope.SECRETS_READ)
    assert not principal.has_scope(Scope.SECRETS_WRITE)
