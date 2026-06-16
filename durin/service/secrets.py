"""SecretsService — read, delete, and write secret-store entries.

Wraps durin's ``SecretStore``. Returns metadata only: a secret's value never
leaves the store — callers get a masked hint.

Extracted from ``durin/channels/websocket.py`` (``_handle_secrets_list`` /
``_handle_secret_delete``) in SP1; the channel keeps wire-identical shims.
``store_entry`` (sync core) + ``store`` (async HTTP route) added in SP5.
"""

from __future__ import annotations

from pydantic import Field

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, NotFoundError, Query, Result, ValidationFailedError


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


class SecretStoreCommand(Command):
    """Create or update a secret. An empty ``value`` is allowed ONLY as a
    metadata-only edit of an existing secret (the stored credential is kept)."""

    name: str
    value: str = ""
    service: str
    account: str = ""
    description: str = ""
    scope: list[str] = Field(default_factory=list)
    origin: str = "user"


class SecretsService:
    """Read, write, and delete entries in the durin SecretStore (values never returned)."""

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
        "DELETE",
        "/api/v1/secrets",
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

    def store_entry(
        self,
        *,
        name: str,
        value: str,
        service: str,
        account: str = "",
        description: str = "",
        scope: list[str] | None = None,
        origin: str = "user",
    ) -> SecretItem:
        """Single source of truth for a secret-store write.

        Called by the HTTP route (:meth:`store`), the websocket
        ``secret_store`` frame handler, and the TUI secret prompt — so the
        put/save/reload sequence and the validation rules live in exactly one
        place. Synchronous on purpose: in-process callers (the TUI) must not be
        forced onto the event loop just to write a credential.

        An empty ``value`` is a metadata-only edit of an existing secret (keeps
        the stored credential); for a new secret it raises
        :class:`ValidationFailedError`. ``created_at`` and ``origin`` survive an
        edit (``SecretStore.put`` preserves ``created_at``; ``origin`` is taken
        from the existing entry here).
        """
        from durin.security.secrets import (
            SecretError,
            SecretStore,
            get_secret_store,
            is_valid_secret_name,
            mask_secret_hint,
        )

        if not is_valid_secret_name(name):
            raise ValidationFailedError(
                "invalid secret name (use UPPER_SNAKE)", details={"name": name}
            )
        if not service.strip():
            raise ValidationFailedError("service is required")

        store = SecretStore().load()
        existing = store.get(name)
        if not value:
            if existing is None:
                raise ValidationFailedError("value is required for a new secret")
            value = existing.value

        try:
            store.put(
                name,
                value=value,
                service=service.strip(),
                account=(account.strip() or None),
                description=description.strip(),
                scope=[s.strip() for s in (scope or []) if s.strip()],
                origin=existing.origin if existing else origin,
            )
            store.save()
        except SecretError as exc:
            # Defensive: the name is pre-validated above, so put() should not
            # raise today — but any future SecretError on a write is a 422, not
            # a 500, so keep the mapping here rather than letting it escape.
            raise ValidationFailedError(str(exc)) from exc
        get_secret_store(reload=True)

        entry = store.get(name)
        return SecretItem(
            name=name,
            service=entry.service,
            account=entry.account or "",
            description=entry.description,
            scope=list(entry.scope),
            origin=entry.origin,
            created_at=entry.created_at,
            value_hint=mask_secret_hint(entry.value),
        )

    @route(
        "POST",
        "/api/v1/secrets",
        scope=Scope.SECRETS_WRITE.value,
        request_model=SecretStoreCommand,
        response_model=SecretItem,
        summary="Create or update a secret (value never returned)",
    )
    async def store(self, cmd: SecretStoreCommand, principal: Principal) -> SecretItem:
        principal.require(Scope.SECRETS_WRITE)
        return self.store_entry(
            name=cmd.name,
            value=cmd.value,
            service=cmd.service,
            account=cmd.account,
            description=cmd.description,
            scope=cmd.scope,
            origin=cmd.origin,
        )
