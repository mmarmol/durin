"""DiscordService — Discord channel service.

Provides:
- ``test``: validates a bot token and reports the bot's identity and whether it
  may read message content.  REST only — it never opens a gateway connection,
  and it persists nothing.
- ``pairing`` / ``pairing_approve`` / ``pairing_deny`` / ``pairing_revoke``:
  manage the sender-approval list stored in ``durin.pairing.store``.
- ``guilds``: the servers the bot is in and their messageable channels, so an
  operator picks channels by name instead of pasting snowflake IDs.
- ``invite``: a least-privilege OAuth invite URL.

Every call is REST-only against the configured token, like the Slack service.
Nothing here reaches into the running channel, so the panel keeps working while
the channel is stopped.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from durin.pairing import store as pairing_store
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Query, Result

# Exactly the permissions durin exercises.  Creating a forum post goes through
# ForumChannel.create_thread, which needs Send Messages rather than Create
# Public Threads, and durin never opens a thread in a text channel.  Removing
# its own reaction uses the unprivileged remove_own_reaction route, so Manage
# Messages is not needed either.  Administrator (8) is never requested.
INVITE_PERMISSIONS = (
    (1 << 10)  # View Channels
    | (1 << 11)  # Send Messages
    | (1 << 38)  # Send Messages in Threads
    | (1 << 14)  # Embed Links
    | (1 << 15)  # Attach Files
    | (1 << 16)  # Read Message History
    | (1 << 6)  # Add Reactions
)
INVITE_SCOPES = "bot+applications.commands"

# Channel types that can receive a message.  Voice, category and stage channels
# would render as allowlist checkboxes that can never match anything.
_MESSAGEABLE_CHANNEL_TYPES = frozenset({0, 5, 15})  # text, announcement, forum

_NAME_CACHE: dict[str, str] = {}


class DiscordTestCommand(Command):
    # Empty means "check the token already in the config", which is how the
    # connected panel re-verifies a stored credential without re-pasting it.
    token: str = ""


class DiscordTestResult(Result):
    ok: bool
    bot_user: str | None = None
    application_id: str | None = None
    # enabled | limited | disabled | unknown.  "limited" is the normal state of
    # an unverified app: it works today and Discord revokes it past 100 guilds.
    message_content_intent: str | None = None
    # Built here, before the token is saved, so the setup wizard never has to
    # rebuild it — a second copy of the permission bitfield would be free to
    # drift away from the one this module owns.
    invite_url: str | None = None
    error: str | None = None


class PairingListQuery(Query):
    pass


class PairingListResult(Result):
    pending: list[dict[str, Any]]
    approved: list[str]
    names: dict[str, str] = {}


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


class DiscordGuildsListQuery(Query):
    pass


class DiscordGuildsListResult(Result):
    ok: bool
    guilds: list[dict[str, Any]] = []
    error: str | None = None


class DiscordInviteQuery(Query):
    pass


class DiscordInviteResult(Result):
    ok: bool
    url: str | None = None
    permissions: str | None = None
    scopes: str | None = None
    error: str | None = None


class DiscordService:
    """Service for Discord channel operations."""

    @route(
        "POST",
        "/api/v1/channels/discord/test",
        scope=Scope.CONFIG_WRITE.value,
        request_model=DiscordTestCommand,
        response_model=DiscordTestResult,
        summary="Validate a Discord bot token and report read permission (persists nothing)",
    )
    async def test(self, cmd: DiscordTestCommand, principal: Principal) -> DiscordTestResult:
        """Log in over REST, then report identity and message-content access."""
        principal.require(Scope.CONFIG_WRITE)
        token = cmd.token or _configured_token()
        if not token:
            return DiscordTestResult(ok=False, error="not_configured")

        client = _rest_client()
        try:
            await _login(client, token)
            app = await client.application_info()
            app_id = str(app.id) if app and app.id else _application_id_from_token(token)
            return DiscordTestResult(
                ok=True,
                bot_user=str(client.user) if client.user else None,
                application_id=app_id,
                message_content_intent=_intent_state(getattr(app, "flags", None)),
                invite_url=_invite_url(app_id) if app_id else None,
            )
        except Exception as e:  # noqa: BLE001 — the message may contain the token
            return DiscordTestResult(ok=False, error=_error_code(e))
        finally:
            await _close(client)

    @route(
        "GET",
        "/api/v1/channels/discord/pairing",
        scope=Scope.CONFIG_READ.value,
        request_model=PairingListQuery,
        response_model=PairingListResult,
        summary="List pending and approved Discord pairing entries",
    )
    async def pairing(self, query: PairingListQuery, principal: Principal) -> PairingListResult:
        """Return pending codes and approved sender IDs for the discord channel."""
        principal.require(Scope.CONFIG_READ)
        pending = [p for p in pairing_store.list_pending() if p.get("channel") == "discord"]
        approved = pairing_store.get_approved("discord")
        ids = {str(p["sender_id"]) for p in pending} | {str(a) for a in approved}
        names = await _display_names(ids)
        return PairingListResult(pending=pending, approved=approved, names=names)

    @route(
        "POST",
        "/api/v1/channels/discord/pairing/approve",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PairingApproveCommand,
        response_model=PairingApproveResult,
        summary="Approve a pending Discord pairing code",
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
        "/api/v1/channels/discord/pairing/deny",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PairingDenyCommand,
        response_model=PairingDenyResult,
        summary="Deny and discard a pending Discord pairing code",
    )
    async def pairing_deny(
        self, cmd: PairingDenyCommand, principal: Principal
    ) -> PairingDenyResult:
        """Deny *code*, removing it from the pending list."""
        principal.require(Scope.CONFIG_WRITE)
        return PairingDenyResult(ok=pairing_store.deny_code(cmd.code))

    @route(
        "POST",
        "/api/v1/channels/discord/pairing/revoke",
        scope=Scope.CONFIG_WRITE.value,
        request_model=PairingRevokeCommand,
        response_model=PairingRevokeResult,
        summary="Revoke an approved Discord sender",
    )
    async def pairing_revoke(
        self, cmd: PairingRevokeCommand, principal: Principal
    ) -> PairingRevokeResult:
        """Remove *sender_id* from the discord approved list."""
        principal.require(Scope.CONFIG_WRITE)
        return PairingRevokeResult(ok=pairing_store.revoke("discord", cmd.sender_id))

    @route(
        "GET",
        "/api/v1/channels/discord/guilds",
        scope=Scope.CONFIG_READ.value,
        request_model=DiscordGuildsListQuery,
        response_model=DiscordGuildsListResult,
        summary="List the servers the bot is in and their messageable channels",
    )
    async def guilds(
        self, query: DiscordGuildsListQuery, principal: Principal
    ) -> DiscordGuildsListResult:
        """Return guilds and channels so the operator picks by name, not by ID."""
        principal.require(Scope.CONFIG_READ)
        token = _configured_token()
        if not token:
            return DiscordGuildsListResult(ok=False, error="not_configured")

        allowed = set(_configured_allow_channels())
        try:
            out: list[dict[str, Any]] = []
            for guild in await _rest_guilds(token):
                channels = [
                    {
                        "id": str(c["id"]),
                        "name": c.get("name") or "",
                        "type": c.get("type"),
                        "allowed": str(c["id"]) in allowed,
                    }
                    for c in await _rest_channels(token, str(guild["id"]))
                    if c.get("type") in _MESSAGEABLE_CHANNEL_TYPES
                ]
                out.append(
                    {"id": str(guild["id"]), "name": guild.get("name") or "", "channels": channels}
                )
            return DiscordGuildsListResult(ok=True, guilds=out)
        except Exception as e:  # noqa: BLE001
            return DiscordGuildsListResult(ok=False, error=_error_code(e))

    @route(
        "GET",
        "/api/v1/channels/discord/invite",
        scope=Scope.CONFIG_READ.value,
        request_model=DiscordInviteQuery,
        response_model=DiscordInviteResult,
        summary="Build a least-privilege OAuth invite URL for the configured bot",
    )
    async def invite(self, query: DiscordInviteQuery, principal: Principal) -> DiscordInviteResult:
        """Build the invite URL.  Never requests Administrator."""
        principal.require(Scope.CONFIG_READ)
        token = _configured_token()
        if not token:
            return DiscordInviteResult(ok=False, error="not_configured")
        app_id = _application_id_from_token(token)
        if not app_id:
            return DiscordInviteResult(ok=False, error="unknown")
        return DiscordInviteResult(
            ok=True,
            url=_invite_url(app_id),
            permissions=str(INVITE_PERMISSIONS),
            scopes=INVITE_SCOPES,
        )


# --------------------------------------------------------------------- helpers


def _rest_client() -> Any:
    """A discord.py client used only for REST calls, never for the gateway."""
    import discord

    return discord.Client(intents=discord.Intents.none())


async def _login(client: Any, token: str) -> None:
    """Authenticate over REST.  ``login`` populates ``user`` and ``application``
    without ever opening the gateway websocket."""
    await client.login(token)


async def _close(client: Any) -> None:
    try:
        await client.close()
    except Exception:  # noqa: BLE001 — teardown must never mask the real error
        pass


def _invite_url(application_id: str) -> str:
    """The one place the invite URL is built.  Never requests Administrator."""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope={INVITE_SCOPES}&permissions={INVITE_PERMISSIONS}"
    )


def _intent_state(flags: Any) -> str:
    """Map application flags to the read-permission state shown in the UI."""
    if flags is None:
        return "unknown"
    if getattr(flags, "gateway_message_content", False):
        return "enabled"
    if getattr(flags, "gateway_message_content_limited", False):
        return "limited"
    return "disabled"


def _error_code(e: Exception) -> str:
    """A curated code, never the exception text — it may contain the token."""
    import discord

    if isinstance(e, discord.LoginFailure):
        return "invalid_token"
    status = getattr(getattr(e, "response", None), "status", None) or getattr(e, "status", None)
    if status == 401:
        return "unauthorized"
    if status == 429:
        return "rate_limited"
    if status == 403:
        return "forbidden"
    if isinstance(e, OSError):
        return "network_error"
    return "unknown"


def _application_id_from_token(token: str) -> str | None:
    """Decode the application ID from the token's first segment.

    Undocumented Discord behaviour, so it is only a fast path: the authoritative
    value is the application info returned by ``test``.
    """
    segment = (token or "").split(".", 1)[0]
    if not segment:
        return None
    try:
        decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    return decoded if decoded.isdigit() else None


def _configured_discord_value(*keys: str) -> str | None:
    """Resolve a discord config value from the live config (``${secret:...}`` aware)."""
    try:
        from durin.config.loader import get_config_path, load_config
        from durin.security.secrets import resolve_secret

        cfg = load_config(get_config_path())
        raw = (cfg.channels.model_extra or {}).get("discord") or {}
        if not isinstance(raw, dict):
            return None
        for key in keys:
            if raw.get(key):
                value = resolve_secret(raw[key])
                return str(value) if value else None
        return None
    except Exception:  # noqa: BLE001
        return None


def _configured_token() -> str | None:
    return _configured_discord_value("token")


def _configured_allow_channels() -> list[str]:
    try:
        from durin.config.loader import get_config_path, load_config

        cfg = load_config(get_config_path())
        raw = (cfg.channels.model_extra or {}).get("discord") or {}
        values = raw.get("allow_channels") or raw.get("allowChannels") or []
        return [str(v) for v in values] if isinstance(values, list) else []
    except Exception:  # noqa: BLE001
        return []


async def _rest_guilds(token: str) -> list[dict[str, Any]]:
    """GET /users/@me/guilds — works with the channel stopped."""
    client = _rest_client()
    try:
        await _login(client, token)
        return await client.http.get_guilds(limit=200)
    finally:
        await _close(client)


async def _rest_channels(token: str, guild_id: str) -> list[dict[str, Any]]:
    """GET /guilds/{id}/channels."""
    client = _rest_client()
    try:
        await _login(client, token)
        return await client.http.get_all_guild_channels(int(guild_id))
    finally:
        await _close(client)


async def _display_names(user_ids: set[str]) -> dict[str, str]:
    """Best-effort Discord user id -> display name via GET /users/{id}.

    A global lookup: it does not need a mutual guild with the bot.
    """
    wanted = {uid for uid in user_ids if uid}
    names = {uid: _NAME_CACHE[uid] for uid in wanted if uid in _NAME_CACHE}
    missing = wanted - set(names)
    if not missing:
        return names
    token = _configured_token()
    if not token:
        return names
    client = _rest_client()
    try:
        await _login(client, token)
        for uid in missing:
            try:
                data = await client.http.get_user(int(uid))
            except Exception:  # noqa: BLE001 — a deleted user must not fail the list
                continue
            name = data.get("global_name") or data.get("username")
            if name:
                _NAME_CACHE[uid] = name
                names[uid] = name
    except Exception:  # noqa: BLE001
        return names
    finally:
        await _close(client)
    return names
