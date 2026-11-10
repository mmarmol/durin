"""AuthService — issue, list, and revoke API tokens; resolve bearer → Principal.

Commands/queries/results follow the SP0 DTO pattern.  ``resolve`` is NOT a
``@route``; it is called by the channel adapter to build a ``Principal`` from a
bearer token before dispatching to any service.
"""

from __future__ import annotations

from typing import Any

from durin.security.api_tokens import ApiTokenStore, LifetimeExceedsBoundError
from durin.service.principal import (
    Principal,
    Scope,
    check_requested_lifetime,
    check_requested_scopes,
)
from durin.service.registry import route
from durin.service.types import (
    Command,
    ForbiddenError,
    NotFoundError,
    Query,
    Result,
)

# Scopes used by this service's own routes.
_SYSTEM_READ = Scope.SYSTEM_READ.value
_SYSTEM_WRITE = Scope.SYSTEM_WRITE.value


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class IssueTokenCommand(Command):
    scopes: list[str]
    label: str = ""
    ttl_s: float | None = None


class IssueTokenResult(Result):
    token_id: str
    token: str  # plaintext — returned ONCE, never stored
    scopes: list[str]
    expires_at: float | None


class ListTokensQuery(Query):
    """No inputs — list all token metadata."""


class TokenMetadata(Result):
    token_id: str
    label: str
    scopes: list[str]
    kind: str
    created_at: float | None
    expires_at: float | None
    last_used_at: float | None


class ListTokensResult(Result):
    tokens: list[TokenMetadata]


class RevokeTokenCommand(Command):
    token_id: str


class RevokeTokenResult(Result):
    ok: bool


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AuthService:
    """Manage API tokens backed by :class:`~durin.security.api_tokens.ApiTokenStore`."""

    def __init__(self, store: ApiTokenStore | None = None) -> None:
        self._store = store if store is not None else ApiTokenStore()

    @route(
        "POST",
        "/api/v1/auth/tokens",
        scope=_SYSTEM_WRITE,
        request_model=IssueTokenCommand,
        response_model=IssueTokenResult,
        summary="Issue a new API token (plaintext returned once)",
    )
    async def issue_token(
        self, cmd: IssueTokenCommand, principal: Principal
    ) -> IssueTokenResult:
        principal.require(Scope.SYSTEM_WRITE)
        check_requested_scopes(cmd.scopes)
        check_requested_lifetime(cmd.ttl_s)
        # A token may grant only what its issuer holds, judged by the same
        # check every route uses, so ``admin`` covers every scope and nothing
        # else implies anything; otherwise a ``system:write`` token could mint
        # an ``admin`` one.
        lacking = sorted(s for s in set(cmd.scopes) if not principal.has_scope(s))
        if lacking:
            raise ForbiddenError(
                f"cannot grant scope(s) the issuer does not hold: {', '.join(lacking)}",
                details={"scopes": lacking},
            )
        # Nor may it outlive its issuer: an expiring issuer that could mint a
        # non-expiring (or longer-lived) token would never lose its access.
        # The request is refused rather than shortened, so a caller never gets
        # a token that dies sooner than it asked for without being told.
        try:
            token_id, plaintext = self._store.issue(
                cmd.scopes,
                label=cmd.label,
                ttl_s=cmd.ttl_s,
                not_after=principal.expires_at,
            )
        except LifetimeExceedsBoundError as exc:
            raise ForbiddenError(
                "requested lifetime exceeds the issuer's remaining lifetime",
                details={"remaining_s": exc.remaining_s},
            ) from None
        # Retrieve expires_at from the stored metadata so it is consistent.
        entry = self._find_entry(token_id)
        expires_at = entry.get("expires_at") if entry else None
        return IssueTokenResult(
            token_id=token_id,
            token=plaintext,
            scopes=cmd.scopes,
            expires_at=expires_at,
        )

    @route(
        "GET",
        "/api/v1/auth/tokens",
        scope=_SYSTEM_READ,
        request_model=ListTokensQuery,
        response_model=ListTokensResult,
        summary="List API token metadata (no hashes or plaintexts)",
    )
    async def list_tokens(
        self, query: ListTokensQuery, principal: Principal
    ) -> ListTokensResult:
        principal.require(Scope.SYSTEM_READ)
        items = [
            TokenMetadata(
                token_id=t["token_id"],
                label=t.get("label", ""),
                scopes=t.get("scopes", []),
                kind=t.get("kind", "remote"),
                created_at=t.get("created_at"),
                expires_at=t.get("expires_at"),
                last_used_at=t.get("last_used_at"),
            )
            for t in self._store.list_tokens()
        ]
        return ListTokensResult(tokens=items)

    @route(
        "DELETE",
        "/api/v1/auth/tokens",
        scope=_SYSTEM_WRITE,
        request_model=RevokeTokenCommand,
        response_model=RevokeTokenResult,
        summary="Revoke an API token by id",
    )
    async def revoke_token(
        self, cmd: RevokeTokenCommand, principal: Principal
    ) -> RevokeTokenResult:
        principal.require(Scope.SYSTEM_WRITE)
        not_found = NotFoundError("token not found", details={"token_id": cmd.token_id})
        entry = self._find_entry(cmd.token_id)
        if entry is None:
            raise not_found
        # A caller may revoke only a token it covers, judged by the same check
        # issuing uses — so a token can always revoke itself or anything it
        # could have issued, ``admin`` can revoke anything, and a narrow token
        # cannot cut off a broader one.
        lacking = sorted(
            s for s in set(entry.get("scopes", [])) if not principal.has_scope(s)
        )
        if lacking:
            # A caller that cannot list tokens is not told this one exists.
            if not principal.has_scope(Scope.SYSTEM_READ):
                raise not_found
            raise ForbiddenError(
                f"cannot revoke a token holding scope(s) the caller does not hold: "
                f"{', '.join(lacking)}",
                details={"scopes": lacking},
            )
        if not self._store.revoke(cmd.token_id):
            raise not_found
        return RevokeTokenResult(ok=True)

    def resolve(self, plaintext: str) -> Principal | None:
        """Resolve a bearer token to a :class:`Principal`, or ``None``.

        Called by the channel adapter (not a route).  Delegates hash-checking
        to the store; builds a principal from the stored scopes and expiry so
        the service layer can enforce fine-grained scope checks and bound what
        the token issues by its own lifetime. A dashboard session token
        (stored ``kind: "webui"``) becomes a ``Principal.webui``; every other
        token a ``Principal.remote``.
        """
        entry = self._store.resolve(plaintext)
        if entry is None:
            return None
        scopes = frozenset(entry.get("scopes", []))
        expires_at = entry.get("expires_at")
        if entry.get("kind") == "webui":
            return Principal.webui(entry["token_id"], scopes, expires_at=expires_at)
        return Principal.remote(entry["token_id"], scopes, expires_at=expires_at)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_entry(self, token_id: str) -> dict[str, Any] | None:
        for t in self._store.list_tokens():
            if t["token_id"] == token_id:
                return t
        return None
