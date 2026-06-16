"""OAuthService — Codex OAuth flow status and device-code / loopback endpoints.

Wraps ``durin.providers.codex_device_auth`` functions.  Pure: no HTTP/WS
imports, no request object.

Locality
--------
``is_local`` — whether the loopback OAuth flow can reach the caller's browser —
is carried on the Command/Query and enforced by the service, which raises
``ForbiddenError`` when a loopback start is attempted from a non-local context.

Scopes
------
oauth = provider configuration, so the existing settings scopes are reused:
- status / poll → ``SETTINGS_READ``
- start / start_loopback / disconnect → ``SETTINGS_WRITE``

``dict[str, Any]`` escape hatches
-----------------------------------
All Result models here carry only well-defined fields; no escape hatches needed.

Extracted from ``durin/channels/websocket.py`` (``_codex_status_payload`` /
``_handle_codex_oauth_*``) in SP1; the channel keeps wire-identical shims.
"""

from __future__ import annotations

import asyncio
from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ForbiddenError,
    Query,
    Result,
    UnavailableError,
    ValidationFailedError,
)

# ---------------------------------------------------------------------------
# DTOs — status
# ---------------------------------------------------------------------------


class OAuthStatusQuery(Query):
    """Query for ``GET /api/oauth/codex/status``.

    ``is_local`` — True when the HTTP request arrived via localhost — is used
    to populate ``can_loopback`` in the response.
    """

    is_local: bool = False


class OAuthStatusResult(Result):
    connected: bool
    email: str | None = None
    plan: str | None = None
    source: str | None = None
    can_loopback: bool = False


# ---------------------------------------------------------------------------
# DTOs — start loopback
# ---------------------------------------------------------------------------


class OAuthStartLoopbackCommand(Command):
    """Command for ``GET /api/oauth/codex/start-loopback``.

    ``is_local`` gates the loopback flow — the service raises ``ForbiddenError``
    if False.
    """

    is_local: bool = False


class OAuthStartLoopbackResult(Result):
    authorize_url: str


# ---------------------------------------------------------------------------
# DTOs — start (device code)
# ---------------------------------------------------------------------------


class OAuthStartCommand(Command):
    """Command for ``GET /api/oauth/codex/start`` (device-code flow)."""


class OAuthStartResult(Result):
    user_code: str
    verification_uri: str
    device_auth_id: str
    interval: int
    expires_in: int


# ---------------------------------------------------------------------------
# DTOs — poll
# ---------------------------------------------------------------------------


class OAuthPollQuery(Query):
    """Query for ``GET /api/oauth/codex/poll``."""

    device_auth_id: str
    user_code: str


class OAuthPollResult(Result):
    """Poll tick result.

    ``status`` is one of ``"pending"`` | ``"ok"`` | ``"error"``.
    When ``status == "ok"`` the connected-session fields are also present.
    """

    status: str
    error: str | None = None
    # Session fields — populated only when status == "ok"
    connected: bool | None = None
    email: str | None = None
    plan: str | None = None
    source: str | None = None


# ---------------------------------------------------------------------------
# DTOs — disconnect
# ---------------------------------------------------------------------------


class OAuthDisconnectCommand(Command):
    """Command for ``GET /api/oauth/codex/disconnect``."""


# Reuse OAuthStatusResult for the disconnect response (same shape, no can_loopback).


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class OAuthService:
    """Codex OAuth flow — status, device-code start, loopback start, poll, disconnect."""

    def _codex_status_payload(self) -> dict[str, Any]:
        """Build the connected/email/plan/source dict.

        Moved from ``WebSocketChannel._codex_status_payload``.
        """
        from durin.providers.codex_device_auth import existing_codex_session

        info = existing_codex_session()
        if info is None:
            return {"connected": False}
        return {
            "connected": True,
            "email": info.email,
            "plan": info.plan,
            "source": info.source,
        }

    @route(
        "GET",
        "/api/v1/oauth/codex/status",
        scope=Scope.SETTINGS_READ.value,
        request_model=OAuthStatusQuery,
        response_model=OAuthStatusResult,
        summary="Return Codex OAuth connection status",
    )
    async def status(self, query: OAuthStatusQuery, principal: Principal) -> OAuthStatusResult:
        principal.require(Scope.SETTINGS_READ)
        payload = self._codex_status_payload()
        return OAuthStatusResult(
            connected=payload.get("connected", False),
            email=payload.get("email"),
            plan=payload.get("plan"),
            source=payload.get("source"),
            can_loopback=query.is_local,
        )

    @route(
        "POST",
        "/api/v1/oauth/codex/start-loopback",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=OAuthStartLoopbackCommand,
        response_model=OAuthStartLoopbackResult,
        summary="Start the loopback PKCE login (localhost-only)",
    )
    async def start_loopback(
        self, cmd: OAuthStartLoopbackCommand, principal: Principal
    ) -> OAuthStartLoopbackResult:
        principal.require(Scope.SETTINGS_WRITE)
        if not cmd.is_local:
            raise ForbiddenError(
                "loopback unavailable on a remote gateway; use device code"
            )
        try:
            from durin.providers.codex_device_auth import start_loopback_login

            # Blocking httpx network call — offload to a thread so it never
            # stalls the single gateway event loop (spec's headline risk).
            url = await asyncio.to_thread(start_loopback_login)
        except Exception as exc:  # noqa: BLE001
            raise UnavailableError(f"loopback login failed to start: {exc}") from exc
        return OAuthStartLoopbackResult(authorize_url=url)

    @route(
        "POST",
        "/api/v1/oauth/codex/start",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=OAuthStartCommand,
        response_model=OAuthStartResult,
        summary="Request a device-code challenge for Codex OAuth",
    )
    async def start(self, cmd: OAuthStartCommand, principal: Principal) -> OAuthStartResult:
        principal.require(Scope.SETTINGS_WRITE)
        try:
            from durin.providers.codex_device_auth import request_device_code

            # Blocking httpx POST — offload off the gateway loop (see start_loopback).
            ch = await asyncio.to_thread(request_device_code)
        except Exception as exc:  # noqa: BLE001
            raise UnavailableError(f"device code request failed: {exc}") from exc
        return OAuthStartResult(
            user_code=ch.user_code,
            verification_uri=ch.verification_uri,
            device_auth_id=ch.device_auth_id,
            interval=ch.interval,
            expires_in=ch.expires_in,
        )

    @route(
        "GET",
        "/api/v1/oauth/codex/poll",
        scope=Scope.SETTINGS_READ.value,
        request_model=OAuthPollQuery,
        response_model=OAuthPollResult,
        summary="Poll the device-code flow for completion",
    )
    async def poll(self, query: OAuthPollQuery, principal: Principal) -> OAuthPollResult:
        principal.require(Scope.SETTINGS_READ)
        device_auth_id = query.device_auth_id.strip()
        user_code = query.user_code.strip()
        if not device_auth_id or not user_code:
            raise ValidationFailedError("device_auth_id and user_code are required")

        from durin.providers.codex_device_auth import poll_once as codex_poll_once

        # Blocking httpx POST — offload off the gateway loop (see start_loopback).
        res = await asyncio.to_thread(codex_poll_once, device_auth_id, user_code)
        if res.status == "ok":
            session = self._codex_status_payload()
            return OAuthPollResult(
                status=res.status,
                connected=session.get("connected"),
                email=session.get("email"),
                plan=session.get("plan"),
                source=session.get("source"),
            )
        if res.status == "error":
            return OAuthPollResult(status=res.status, error=res.error)
        return OAuthPollResult(status=res.status)

    @route(
        "DELETE",
        "/api/v1/oauth/codex",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=OAuthDisconnectCommand,
        response_model=OAuthStatusResult,
        summary="Disconnect the Codex OAuth session",
    )
    async def disconnect(
        self, cmd: OAuthDisconnectCommand, principal: Principal
    ) -> OAuthStatusResult:
        principal.require(Scope.SETTINGS_WRITE)
        from durin.providers.codex_device_auth import disconnect as codex_disconnect

        codex_disconnect()
        payload = self._codex_status_payload()
        return OAuthStatusResult(
            connected=payload.get("connected", False),
            email=payload.get("email"),
            plan=payload.get("plan"),
            source=payload.get("source"),
            can_loopback=False,
        )
