"""AuthService — issue, list, and revoke API tokens; resolve bearer → Principal.

Commands/queries/results follow the SP0 DTO pattern.  ``resolve`` is NOT a
``@route``; it is called by the channel adapter to build a ``Principal`` from a
bearer token before dispatching to any service.
"""

from __future__ import annotations

from typing import Any

from durin.security.api_tokens import ApiTokenStore
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, NotFoundError, Query, Result

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
        token_id, plaintext = self._store.issue(
            cmd.scopes,
            label=cmd.label,
            ttl_s=cmd.ttl_s,
        )
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
        if not self._store.revoke(cmd.token_id):
            raise NotFoundError("token not found", details={"token_id": cmd.token_id})
        return RevokeTokenResult(ok=True)

    def resolve(self, plaintext: str) -> Principal | None:
        """Resolve a bearer token to a :class:`Principal`, or ``None``.

        Called by the channel adapter (not a route).  Delegates hash-checking
        to the store; builds a ``Principal.remote`` from the stored scopes so
        the service layer can enforce fine-grained scope checks.
        """
        entry = self._store.resolve(plaintext)
        if entry is None:
            return None
        return Principal.remote(entry["token_id"], frozenset(entry.get("scopes", [])))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_entry(self, token_id: str) -> dict[str, Any] | None:
        for t in self._store.list_tokens():
            if t["token_id"] == token_id:
                return t
        return None
