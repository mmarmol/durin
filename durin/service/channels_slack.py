"""SlackService — Slack channel service.

Provides:
- ``manifest``: returns the Slack app manifest that provisions everything the
  channel needs (scopes, events, Socket Mode) so the user can create the app
  with a single paste at api.slack.com/apps instead of clicking through every
  permission screen.
- ``test``: validates a bot token (auth.test) and/or an app-level token
  (apps.connections.open), persists nothing.
- ``pairing`` / ``pairing_approve`` / ``pairing_deny`` / ``pairing_revoke``:
  manage the sender-approval list stored in ``durin.pairing.store``.

slack_sdk is an optional extra, so it is imported lazily inside the handlers;
the routes themselves are always registered.
"""

from __future__ import annotations

from typing import Any

from durin.pairing import store as pairing_store
from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import Command, Query, Result

# Bot scopes and events are the union of what durin/channels/slack.py actually
# calls: chat_postMessage, files_upload_v2, private-file downloads,
# reactions_add/remove, conversations_list/replies/open, users_list, and the
# message/app_mention event streams for DMs, channels, private groups and
# group DMs.
SLACK_BOT_SCOPES = [
    "app_mentions:read",
    "channels:history",
    "channels:join",
    "channels:read",
    "chat:write",
    "files:read",
    "files:write",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "reactions:write",
    "users:read",
]

SLACK_BOT_EVENTS = [
    "app_mention",
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
]


class SlackManifestQuery(Query):
    name: str = "durin"


class SlackManifestResult(Result):
    manifest: dict[str, Any]


class SlackTestCommand(Command):
    bot_token: str = ""
    app_token: str = ""


class SlackTestResult(Result):
    ok: bool
    bot_user: str | None = None
    team: str | None = None
    bot_error: str | None = None
    app_error: str | None = None


class SlackPairingListQuery(Query):
    pass


class SlackPairingListResult(Result):
    pending: list[dict[str, Any]]
    approved: list[str]


class SlackPairingApproveCommand(Command):
    code: str


class SlackPairingApproveResult(Result):
    ok: bool
    channel: str | None = None
    sender_id: str | None = None


class SlackPairingDenyCommand(Command):
    code: str


class SlackPairingDenyResult(Result):
    ok: bool


class SlackPairingRevokeCommand(Command):
    sender_id: str


class SlackPairingRevokeResult(Result):
    ok: bool


class SlackChannelsListQuery(Query):
    pass


class SlackChannelsListResult(Result):
    ok: bool
    channels: list[dict[str, Any]]
    error: str | None = None


class SlackJoinChannelCommand(Command):
    channel_id: str


class SlackJoinChannelResult(Result):
    ok: bool
    error: str | None = None


def build_slack_manifest(name: str = "durin") -> dict[str, Any]:
    """Return a Slack app manifest for the durin channel (Socket Mode)."""
    return {
        "display_information": {
            "name": name,
            "description": "Personal AI agent",
        },
        "features": {
            # Without an enabled messages tab Slack blocks DMs to the bot
            # entirely ("Sending messages to this app has been turned off").
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": name,
                "always_online": True,
            },
        },
        "oauth_config": {
            "scopes": {"bot": list(SLACK_BOT_SCOPES)},
        },
        "settings": {
            "event_subscriptions": {"bot_events": list(SLACK_BOT_EVENTS)},
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


class SlackService:
    """Service for Slack channel operations."""

    @route(
        "GET",
        "/api/v1/channels/slack/manifest",
        scope=Scope.CONFIG_READ.value,
        request_model=SlackManifestQuery,
        response_model=SlackManifestResult,
        summary="Return the Slack app manifest for create-from-manifest setup",
    )
    async def manifest(
        self, query: SlackManifestQuery, principal: Principal
    ) -> SlackManifestResult:
        """Return the app manifest JSON to paste at api.slack.com/apps."""
        principal.require(Scope.CONFIG_READ)
        name = query.name.strip() or "durin"
        return SlackManifestResult(manifest=build_slack_manifest(name))

    @route(
        "POST",
        "/api/v1/channels/slack/test",
        scope=Scope.CONFIG_WRITE.value,
        request_model=SlackTestCommand,
        response_model=SlackTestResult,
        summary="Validate Slack bot/app tokens (persists nothing)",
    )
    async def test(self, cmd: SlackTestCommand, principal: Principal) -> SlackTestResult:
        """Validate whichever tokens were provided. Persists nothing, logs nothing."""
        principal.require(Scope.CONFIG_WRITE)
        bot_token = cmd.bot_token.strip()
        app_token = cmd.app_token.strip()
        if not bot_token and not app_token:
            # No tokens in the request → health-check the CONFIGURED ones, so
            # the connected panel can prove the stored secrets actually work.
            bot_token = _configured_bot_token() or ""
            app_token = _configured_app_token() or ""
            if not bot_token and not app_token:
                return SlackTestResult(ok=False, bot_error="not_configured")

        # Catch pasted-in-the-wrong-field mistakes before calling Slack: the
        # raw API error (not_allowed_token_type) reads as a mystery in the UI.
        if bot_token.startswith("xapp-") and app_token.startswith("xoxb-"):
            return SlackTestResult(
                ok=False, bot_error="swapped_tokens", app_error="swapped_tokens"
            )
        bot_error = "expected_bot_token" if bot_token and not bot_token.startswith("xoxb-") else None
        app_error = "expected_app_token" if app_token and not app_token.startswith("xapp-") else None

        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            return SlackTestResult(ok=False, bot_error="slack extra not installed")

        bot_user: str | None = None
        team: str | None = None

        if bot_token and bot_error is None:
            try:
                auth = await AsyncWebClient(token=bot_token).auth_test()
                bot_user = str(auth.get("user") or "") or None
                team = str(auth.get("team") or "") or None
            except Exception as e:  # noqa: BLE001 — never include the token in the message
                bot_error = _slack_error_code(e)

        if app_token and app_error is None:
            try:
                await AsyncWebClient(token=app_token).apps_connections_open()
            except Exception as e:  # noqa: BLE001
                app_error = _slack_error_code(e)

        return SlackTestResult(
            ok=bot_error is None and app_error is None,
            bot_user=bot_user,
            team=team,
            bot_error=bot_error,
            app_error=app_error,
        )

    @route(
        "GET",
        "/api/v1/channels/slack/channels",
        scope=Scope.CONFIG_READ.value,
        request_model=SlackChannelsListQuery,
        response_model=SlackChannelsListResult,
        summary="List workspace channels with the bot's membership status",
    )
    async def channels(
        self, query: SlackChannelsListQuery, principal: Principal
    ) -> SlackChannelsListResult:
        """List (up to ~800) channels using the configured bot token."""
        principal.require(Scope.CONFIG_READ)
        bot_token = _configured_bot_token()
        if not bot_token:
            return SlackChannelsListResult(ok=False, channels=[], error="not_configured")
        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            return SlackChannelsListResult(ok=False, channels=[], error="slack extra not installed")

        client = AsyncWebClient(token=bot_token)
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        try:
            for _ in range(4):  # 4 pages x 200 — plenty for a personal workspace
                response = await client.conversations_list(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                )
                for ch in response.get("channels", []):
                    found.append({
                        "id": str(ch.get("id") or ""),
                        "name": str(ch.get("name") or ""),
                        "is_member": bool(ch.get("is_member")),
                        "is_private": bool(ch.get("is_private")),
                    })
                cursor = ((response.get("response_metadata") or {}).get("next_cursor") or "").strip()
                if not cursor:
                    break
        except Exception as e:  # noqa: BLE001
            return SlackChannelsListResult(ok=False, channels=[], error=_slack_error_code(e))

        found.sort(key=lambda ch: (not ch["is_member"], ch["name"]))
        return SlackChannelsListResult(ok=True, channels=found)

    @route(
        "POST",
        "/api/v1/channels/slack/channels/join",
        scope=Scope.CONFIG_WRITE.value,
        request_model=SlackJoinChannelCommand,
        response_model=SlackJoinChannelResult,
        summary="Join the bot to a public channel (private ones need /invite)",
    )
    async def join_channel(
        self, cmd: SlackJoinChannelCommand, principal: Principal
    ) -> SlackJoinChannelResult:
        """conversations.join with the configured bot token (public channels only)."""
        principal.require(Scope.CONFIG_WRITE)
        bot_token = _configured_bot_token()
        if not bot_token:
            return SlackJoinChannelResult(ok=False, error="not_configured")
        try:
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError:
            return SlackJoinChannelResult(ok=False, error="slack extra not installed")
        try:
            await AsyncWebClient(token=bot_token).conversations_join(channel=cmd.channel_id)
        except Exception as e:  # noqa: BLE001
            return SlackJoinChannelResult(ok=False, error=_slack_error_code(e))
        return SlackJoinChannelResult(ok=True)

    @route(
        "GET",
        "/api/v1/channels/slack/pairing",
        scope=Scope.CONFIG_READ.value,
        request_model=SlackPairingListQuery,
        response_model=SlackPairingListResult,
        summary="List pending and approved Slack pairing entries",
    )
    async def pairing(
        self, query: SlackPairingListQuery, principal: Principal
    ) -> SlackPairingListResult:
        """Return pending codes and approved sender IDs for the slack channel."""
        principal.require(Scope.CONFIG_READ)
        pending = [p for p in pairing_store.list_pending() if p.get("channel") == "slack"]
        approved = pairing_store.get_approved("slack")
        return SlackPairingListResult(pending=pending, approved=approved)

    @route(
        "POST",
        "/api/v1/channels/slack/pairing/approve",
        scope=Scope.CONFIG_WRITE.value,
        request_model=SlackPairingApproveCommand,
        response_model=SlackPairingApproveResult,
        summary="Approve a pending Slack pairing code",
    )
    async def pairing_approve(
        self, cmd: SlackPairingApproveCommand, principal: Principal
    ) -> SlackPairingApproveResult:
        """Approve *code* and move the sender to the approved list."""
        principal.require(Scope.CONFIG_WRITE)
        result = pairing_store.approve_code(cmd.code)
        if result is None:
            return SlackPairingApproveResult(ok=False)
        channel, sender_id = result
        return SlackPairingApproveResult(ok=True, channel=channel, sender_id=sender_id)

    @route(
        "POST",
        "/api/v1/channels/slack/pairing/deny",
        scope=Scope.CONFIG_WRITE.value,
        request_model=SlackPairingDenyCommand,
        response_model=SlackPairingDenyResult,
        summary="Deny and discard a pending Slack pairing code",
    )
    async def pairing_deny(
        self, cmd: SlackPairingDenyCommand, principal: Principal
    ) -> SlackPairingDenyResult:
        """Deny *code*, removing it from the pending list."""
        principal.require(Scope.CONFIG_WRITE)
        ok = pairing_store.deny_code(cmd.code)
        return SlackPairingDenyResult(ok=ok)

    @route(
        "POST",
        "/api/v1/channels/slack/pairing/revoke",
        scope=Scope.CONFIG_WRITE.value,
        request_model=SlackPairingRevokeCommand,
        response_model=SlackPairingRevokeResult,
        summary="Revoke an approved Slack sender",
    )
    async def pairing_revoke(
        self, cmd: SlackPairingRevokeCommand, principal: Principal
    ) -> SlackPairingRevokeResult:
        """Remove *sender_id* from the slack approved list."""
        principal.require(Scope.CONFIG_WRITE)
        ok = pairing_store.revoke("slack", cmd.sender_id)
        return SlackPairingRevokeResult(ok=ok)


def _configured_slack_value(*keys: str) -> str | None:
    """Resolve a slack config value from the live config (``${secret:...}`` aware)."""
    try:
        from durin.config.loader import get_config_path, load_config
        from durin.security.secrets import resolve_secret

        cfg = load_config(get_config_path())
        raw = (cfg.channels.model_extra or {}).get("slack") or {}
        if not isinstance(raw, dict):
            return None
        for key in keys:
            if raw.get(key):
                value = resolve_secret(raw[key])
                return str(value) if value else None
        return None
    except Exception:  # noqa: BLE001
        return None


def _configured_bot_token() -> str | None:
    return _configured_slack_value("bot_token", "botToken")


def _configured_app_token() -> str | None:
    return _configured_slack_value("app_token", "appToken")


def _slack_error_code(e: Exception) -> str:
    """Extract the Slack API error code without ever echoing the token."""
    response = getattr(e, "response", None)
    if response is not None:
        try:
            code = str(response.get("error") or "")
            if code:
                return code
        except Exception:  # noqa: BLE001
            pass
    return type(e).__name__
