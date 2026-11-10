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
from durin.service.types import ForbiddenError, NotFoundError, ValidationFailedError


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


# ---------------------------------------------------------------------------
# issue_token — an issuer cannot grant more than it holds
# ---------------------------------------------------------------------------


@pytest.fixture()
def system_writer():
    return Principal.remote("issuer", frozenset({Scope.SYSTEM_WRITE.value, Scope.SKILLS_READ.value}))


async def test_issue_refuses_a_scope_the_issuer_lacks(svc, system_writer):
    cmd = IssueTokenCommand(scopes=[Scope.SKILLS_READ.value, Scope.ADMIN.value])
    with pytest.raises(ForbiddenError) as exc:
        await svc.issue_token(cmd, system_writer)
    assert "admin" in exc.value.message
    assert exc.value.details == {"scopes": ["admin"]}
    assert svc._store.list_tokens() == []


async def test_issue_grants_exactly_a_subset_of_the_issuers_scopes(svc, system_writer):
    cmd = IssueTokenCommand(scopes=[Scope.SKILLS_READ.value])
    result = await svc.issue_token(cmd, system_writer)
    assert result.scopes == [Scope.SKILLS_READ.value]
    issued = svc.resolve(result.token)
    assert issued is not None
    assert issued.scopes == frozenset({Scope.SKILLS_READ.value})


async def test_an_admin_issuer_can_grant_the_scopes_admin_implies(svc):
    remote_admin = Principal.remote("root", frozenset({Scope.ADMIN.value}))
    wanted = [Scope.SECRETS_WRITE.value, Scope.SYSTEM_WRITE.value, Scope.ADMIN.value]
    result = await svc.issue_token(IssueTokenCommand(scopes=wanted), remote_admin)
    assert result.scopes == wanted


async def test_issue_rejects_an_unknown_scope_name(svc, admin):
    cmd = IssueTokenCommand(scopes=[Scope.SKILLS_READ.value, "not-a-scope"])
    with pytest.raises(ValidationFailedError) as exc:
        await svc.issue_token(cmd, admin)
    assert "not-a-scope" in exc.value.message
    assert svc._store.list_tokens() == []


# ---------------------------------------------------------------------------
# A token cannot create or destroy something more powerful than itself
# ---------------------------------------------------------------------------

NOW = 1_800_000_000.0


@pytest.fixture()
def clock(monkeypatch):
    """Pin the token store's clock; tests move it by reassigning ``clock.now``."""

    class _Clock:
        now = NOW

    monkeypatch.setattr("durin.security.api_tokens._now", lambda: _Clock.now)
    return _Clock


def _issuer(svc, scopes, *, ttl_s=None):
    """Issue a token in the store and resolve it the way a request does."""
    _id, plaintext = svc._store.issue(list(scopes), label="issuer", ttl_s=ttl_s)
    principal = svc.resolve(plaintext)
    assert principal is not None
    return principal, plaintext


def _ids(svc):
    return sorted(t["token_id"] for t in svc._store.list_tokens())


@pytest.mark.parametrize("ttl_s", [None, 3600.0 + 1.0, 10 * 365 * 86400.0])
async def test_an_expiring_issuer_cannot_issue_a_token_that_outlives_it(svc, clock, ttl_s):
    issuer, plaintext = _issuer(
        svc, [Scope.SYSTEM_WRITE.value, Scope.SKILLS_READ.value], ttl_s=3600.0)
    before = _ids(svc)
    with pytest.raises(ForbiddenError) as exc:
        await svc.issue_token(
            IssueTokenCommand(scopes=[Scope.SKILLS_READ.value], ttl_s=ttl_s), issuer)
    assert "remaining lifetime" in exc.value.message
    assert exc.value.details == {"remaining_s": 3600.0}
    assert plaintext not in exc.value.message
    assert "nbwt_" not in exc.value.message
    assert _ids(svc) == before


async def test_an_expiring_issuer_issues_within_its_remaining_lifetime(svc, clock):
    issuer, _ = _issuer(svc, [Scope.SYSTEM_WRITE.value, Scope.SKILLS_READ.value], ttl_s=3600.0)
    clock.now = NOW + 600.0  # 3000 s of the issuer's lifetime remain
    for ttl_s in (60.0, 3000.0):
        result = await svc.issue_token(
            IssueTokenCommand(scopes=[Scope.SKILLS_READ.value], ttl_s=ttl_s), issuer)
        assert result.expires_at == clock.now + ttl_s
        assert svc.resolve(result.token) is not None


async def test_the_remaining_lifetime_shrinks_as_the_issuer_ages(svc, clock):
    issuer, _ = _issuer(svc, [Scope.SYSTEM_WRITE.value, Scope.SKILLS_READ.value], ttl_s=3600.0)
    clock.now = NOW + 3000.0
    with pytest.raises(ForbiddenError):
        await svc.issue_token(
            IssueTokenCommand(scopes=[Scope.SKILLS_READ.value], ttl_s=601.0), issuer)


@pytest.mark.parametrize("ttl_s", [None, 60.0, 10 * 365 * 86400.0])
async def test_a_non_expiring_issuer_may_issue_any_lifetime(svc, clock, ttl_s):
    issuer, _ = _issuer(svc, [Scope.SYSTEM_WRITE.value, Scope.SKILLS_READ.value])
    result = await svc.issue_token(
        IssueTokenCommand(scopes=[Scope.SKILLS_READ.value], ttl_s=ttl_s), issuer)
    assert result.expires_at == (None if ttl_s is None else NOW + ttl_s)


async def test_the_local_principal_may_issue_any_lifetime(svc, clock, admin):
    result = await svc.issue_token(IssueTokenCommand(scopes=[Scope.ADMIN.value]), admin)
    assert result.expires_at is None


async def test_issue_rejects_an_empty_scope_list(svc, admin):
    with pytest.raises(ValidationFailedError):
        await svc.issue_token(IssueTokenCommand(scopes=[]), admin)
    assert svc._store.list_tokens() == []


async def test_a_narrower_caller_cannot_revoke_a_broader_token(svc):
    caller, _ = _issuer(svc, [Scope.SYSTEM_WRITE.value, Scope.SYSTEM_READ.value])
    target_id, target = svc._store.issue([Scope.ADMIN.value], label="root")
    with pytest.raises(ForbiddenError) as exc:
        await svc.revoke_token(RevokeTokenCommand(token_id=target_id), caller)
    assert exc.value.details == {"scopes": ["admin"]}
    assert "admin" in exc.value.message
    assert "nbwt_" not in exc.value.message
    assert svc.resolve(target) is not None


async def test_a_caller_that_cannot_list_learns_nothing_from_a_refused_revoke(svc):
    caller, _ = _issuer(svc, [Scope.SYSTEM_WRITE.value])
    target_id, target = svc._store.issue([Scope.SECRETS_WRITE.value], label="other")
    with pytest.raises(NotFoundError) as refused:
        await svc.revoke_token(RevokeTokenCommand(token_id=target_id), caller)
    with pytest.raises(NotFoundError) as missing:
        await svc.revoke_token(RevokeTokenCommand(token_id="0123456789abcdef"), caller)
    assert refused.value.message == missing.value.message
    assert refused.value.details.keys() == missing.value.details.keys()
    assert svc.resolve(target) is not None


async def test_a_caller_covering_the_token_revokes_it(svc):
    caller, _ = _issuer(svc, [Scope.SYSTEM_WRITE.value, Scope.SKILLS_READ.value])
    target_id, target = svc._store.issue([Scope.SKILLS_READ.value], label="child")
    result = await svc.revoke_token(RevokeTokenCommand(token_id=target_id), caller)
    assert result.ok is True
    assert svc.resolve(target) is None


async def test_a_token_may_revoke_itself(svc):
    caller, plaintext = _issuer(svc, [Scope.SYSTEM_WRITE.value, Scope.MEMORY_READ.value])
    result = await svc.revoke_token(RevokeTokenCommand(token_id=caller.subject), caller)
    assert result.ok is True
    assert svc.resolve(plaintext) is None


async def test_admin_revokes_any_token(svc, admin):
    remote_admin, _ = _issuer(svc, [Scope.ADMIN.value])
    session_id, session = svc._store.issue([Scope.ADMIN.value], label="bootstrap", kind="webui")
    await svc.revoke_token(RevokeTokenCommand(token_id=session_id), remote_admin)
    assert svc.resolve(session) is None
    other_id, other = svc._store.issue([Scope.ADMIN.value], label="root")
    await svc.revoke_token(RevokeTokenCommand(token_id=other_id), admin)
    assert svc.resolve(other) is None


async def test_an_expiring_issuer_cannot_issue_a_lifetime_that_is_not_a_number(svc, clock):
    issuer, _ = _issuer(svc, [Scope.SYSTEM_WRITE.value, Scope.SKILLS_READ.value], ttl_s=3600.0)
    before = _ids(svc)
    with pytest.raises(ForbiddenError):
        await svc.issue_token(
            IssueTokenCommand(scopes=[Scope.SKILLS_READ.value], ttl_s=float("nan")), issuer)
    assert _ids(svc) == before
