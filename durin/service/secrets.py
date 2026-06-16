"""SecretsService — read and delete secret-store metadata.

Wraps durin's ``SecretStore``. Returns metadata only: a secret's value never
leaves the store — callers get a masked hint. Creating/updating a secret rides a
WS frame (the value must not travel in a URL), so it is not modeled on this HTTP
surface.

Extracted from ``durin/channels/websocket.py`` (``_handle_secrets_list`` /
``_handle_secret_delete``) in SP1; the channel keeps wire-identical shims.
"""

from __future__ import annotations

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, NotFoundError, Query, Result


class SecretsListQuery(Query):
    """No inputs — lists all secret metadata."""


class SecretItem(Result):
    name: str
    service: str
    account: str
    description: str
    scope: list[str]
    origin: str
    created_at: str
    value_hint: str | None


class SecretsListResult(Result):
    secrets: list[SecretItem]


class SecretDeleteCommand(Command):
    name: str


class SecretDeleteResult(Result):
    ok: bool


class SecretsService:
    """Read/delete entries in the durin SecretStore (metadata only)."""

    @route(
        "GET",
        "/api/v1/secrets",
        scope=Scope.SECRETS_READ.value,
        request_model=SecretsListQuery,
        response_model=SecretsListResult,
        summary="List secret metadata (values never returned)",
    )
    async def list(self, query: SecretsListQuery, principal: Principal) -> SecretsListResult:
        principal.require(Scope.SECRETS_READ)
        from durin.security.secrets import SecretStore, mask_secret_hint

        store = SecretStore().load()
        items = [
            SecretItem(
                name=name,
                service=entry.service,
                account=entry.account or "",
                description=entry.description,
                scope=list(entry.scope),
                origin=entry.origin,
                created_at=entry.created_at,
                value_hint=mask_secret_hint(entry.value),
            )
            for name, entry in sorted(store.all().items())
        ]
        return SecretsListResult(secrets=items)

    @route(
        "GET",
        "/api/v1/secrets/delete",
        scope=Scope.SECRETS_WRITE.value,
        request_model=SecretDeleteCommand,
        response_model=SecretDeleteResult,
        summary="Delete a secret by name",
    )
    async def delete(self, cmd: SecretDeleteCommand, principal: Principal) -> SecretDeleteResult:
        principal.require(Scope.SECRETS_WRITE)
        from durin.security.secrets import SecretStore, get_secret_store

        store = SecretStore().load()
        if not store.remove(cmd.name):
            raise NotFoundError("no such secret", details={"name": cmd.name})
        store.save()
        get_secret_store(reload=True)
        return SecretDeleteResult(ok=True)
