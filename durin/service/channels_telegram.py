"""TelegramService — Telegram channel service.

Provides:
- ``test``: validates a bot token via getMe, persists nothing.
- ``pairing`` / ``pairing_approve`` / ``pairing_deny`` / ``pairing_revoke``:
  manage the sender-approval list stored in ``durin.pairing.store``.
"""

from __future__ import annotations

from typing import Any

from telegram import Bot

from durin.pairing import store as pairing_store
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Query, Result


class TelegramTestCommand(Command):
    token: str


class TelegramTestResult(Result):
    ok: bool
    username: str | None = None
    id: int | None = None
    error: str | None = None


class PairingListQuery(Query):
    pass


class PairingListResult(Result):
    pending: list[dict[str, Any]]
    approved: list[str]


class PairingApproveCommand(Command):
    code: str


class PairingApproveResult(Result):
    ok: bool
    channel: str | None = None
    sender_id: str | None = None


class PairingDenyCommand(Command):
    code: str


class PairingDenyResult(Result):
    ok: bool


class PairingRevokeCommand(Command):
    sender_id: str


class PairingRevokeResult(Result):
    ok: bool


class TelegramService:
    """Service for Telegram channel operations."""

    @route(
        "POST",
        "/api/v1/channels/telegram/test",
        scope=Scope.CONFIG_WRITE.value,
        request_model=TelegramTestCommand,
        response_model=TelegramTestResult,
        summary="Validate a Telegram bot token via getMe (persists nothing)",
    )
    async def test(self, cmd: TelegramTestCommand, principal: Principal) -> TelegramTestResult:
        """Verify a bot token by calling getMe.  Persists nothing, logs nothing."""
        principal.require(Scope.CONFIG_WRITE)
        try:
            async with Bot(cmd.token) as bot:
                me = await bot.get_me()
            return TelegramTestResult(
                ok=True,
                username=getattr(me, "username", None),
                id=getattr(me, "id", None),
            )
        except Exception as e:  # noqa: BLE001 — never include the token in the message
            return TelegramTestResult(ok=False, error=type(e).__name__)

    @route(
        "GET",
        "/api/v1/channels/telegram/pairing",
        scope=Scope.CONFIG_READ.value,
        request_model=PairingListQuery,
        response_model=PairingListResult,
        summary="List pending and approved Telegram pairing entries",
    )
    async def pairing(self, query: PairingListQuery, principal: Principal) -> PairingListResult:
        """Return pending codes and approved sender IDs for the telegram channel."""
        principal.require(Scope.CONFIG_READ)
        pending = [p for p in pairing_store.list_pending() if p.get("channel") == "telegram"]
        approved = pairing_store.get_approved("telegram")
        return PairingListResult(pending=pending, approved=approved)

    @route(
        "POST",
        "/api/v1/channels/telegram/pairing/approve",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PairingApproveCommand,
        response_model=PairingApproveResult,
        summary="Approve a pending Telegram pairing code",
    )
    async def pairing_approve(
        self, cmd: PairingApproveCommand, principal: Principal
    ) -> PairingApproveResult:
        """Approve *code* and move the sender to the approved list."""
        principal.require(Scope.CONFIG_WRITE)
        result = pairing_store.approve_code(cmd.code)
        if result is None:
            return PairingApproveResult(ok=False)
        channel, sender_id = result
        return PairingApproveResult(ok=True, channel=channel, sender_id=sender_id)

    @route(
        "POST",
        "/api/v1/channels/telegram/pairing/deny",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PairingDenyCommand,
        response_model=PairingDenyResult,
        summary="Deny and discard a pending Telegram pairing code",
    )
    async def pairing_deny(
        self, cmd: PairingDenyCommand, principal: Principal
    ) -> PairingDenyResult:
        """Deny *code*, removing it from the pending list."""
        principal.require(Scope.CONFIG_WRITE)
        ok = pairing_store.deny_code(cmd.code)
        return PairingDenyResult(ok=ok)

    @route(
        "POST",
        "/api/v1/channels/telegram/pairing/revoke",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PairingRevokeCommand,
        response_model=PairingRevokeResult,
        summary="Revoke an approved Telegram sender",
    )
    async def pairing_revoke(
        self, cmd: PairingRevokeCommand, principal: Principal
    ) -> PairingRevokeResult:
        """Remove *sender_id* from the telegram approved list."""
        principal.require(Scope.CONFIG_WRITE)
        ok = pairing_store.revoke("telegram", cmd.sender_id)
        return PairingRevokeResult(ok=ok)
