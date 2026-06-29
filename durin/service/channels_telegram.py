"""TelegramService — Telegram channel service with a non-persisting token-test endpoint.

The ``test`` method validates a Telegram bot token by calling the Bot API's
``getMe`` method and returns the bot's username and id on success.  It persists
nothing, stores no secrets, and never logs the token.  On error, only the
exception type name is returned — never a message that might echo the token.
"""

from __future__ import annotations

from telegram import Bot

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Result


class TelegramTestCommand(Command):
    token: str


class TelegramTestResult(Result):
    ok: bool
    username: str | None = None
    id: int | None = None
    error: str | None = None


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
