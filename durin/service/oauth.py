"""OAuthService — provider OAuth flows (Codex, OpenRouter).

Codex: status + device-code / loopback endpoints wrapping
``durin.providers.codex_device_auth``. OpenRouter: status + loopback +
disconnect wrapping ``durin.providers.openrouter_oauth`` — loopback only
(OpenRouter has no device-code flow; remote gateways paste the key
manually), and the outcome is a plain API key stored like a manual paste,
so the webui polls status until ``connected`` flips. Pure: no HTTP/WS
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
# DTOs — OpenRouter
# ---------------------------------------------------------------------------


class OpenRouterStatusQuery(Query):
    """Query for ``GET /api/v1/oauth/openrouter/status``."""

    is_local: bool = False


class OpenRouterStatusResult(Result):
    connected: bool
    api_key_hint: str | None = None
    can_loopback: bool = False


class OpenRouterStartLoopbackCommand(Command):
    """Command for ``POST /api/v1/oauth/openrouter/start-loopback``."""

    is_local: bool = False


class OpenRouterDisconnectCommand(Command):
    """Command for ``DELETE /api/v1/oauth/openrouter``."""


# ---------------------------------------------------------------------------
# DTOs — GitHub (device flow; one shared credential for skills + MCP)
# ---------------------------------------------------------------------------


class GithubStatusQuery(Query):
    """Query for ``GET /api/v1/oauth/github/status`` (live probe)."""


class GithubStatusResult(Result):
    connected: bool  # a token is configured (gh / env / shared secret)
    reachable: bool = False  # GitHub answered 200
    source: str | None = None  # gh | env | secret — which source provided the token
    login: str | None = None
    scopes: str | None = None
    rate_remaining: int | None = None
    rate_limit: int | None = None


class GithubStartCommand(Command):
    """Command for ``POST /api/v1/oauth/github/start``.

    ``private`` escalates the requested scope to private-repo access (``repo``);
    the default is minimal (``read:user``)."""

    private: bool = False


class GithubStartResult(Result):
    flow_id: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    expires_in: int


class GithubPollQuery(Query):
    """Query for ``GET /api/v1/oauth/github/poll``."""

    flow_id: str


class GithubPollResult(Result):
    """``status``: pending | slow_down | authorized | expired | denied | error."""

    status: str
    error: str | None = None
    connected: bool | None = None
    login: str | None = None


class GithubDisconnectCommand(Command):
    """Command for ``DELETE /api/v1/oauth/github``."""


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

    # ------------------------------------------------------------------
    # OpenRouter
    # ------------------------------------------------------------------

    def _openrouter_status(self, *, can_loopback: bool) -> OpenRouterStatusResult:
        from durin.providers.openrouter_oauth import key_status

        st = key_status()
        return OpenRouterStatusResult(
            connected=st.connected,
            api_key_hint=st.api_key_hint,
            can_loopback=can_loopback,
        )

    @route(
        "GET",
        "/api/v1/oauth/openrouter/status",
        scope=Scope.SETTINGS_READ.value,
        request_model=OpenRouterStatusQuery,
        response_model=OpenRouterStatusResult,
        summary="Return OpenRouter key status (manual or OAuth-obtained)",
    )
    async def openrouter_status(
        self, query: OpenRouterStatusQuery, principal: Principal
    ) -> OpenRouterStatusResult:
        principal.require(Scope.SETTINGS_READ)
        return self._openrouter_status(can_loopback=query.is_local)

    @route(
        "POST",
        "/api/v1/oauth/openrouter/start-loopback",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=OpenRouterStartLoopbackCommand,
        response_model=OAuthStartLoopbackResult,
        summary="Start the OpenRouter loopback PKCE login (localhost-only)",
    )
    async def openrouter_start_loopback(
        self, cmd: OpenRouterStartLoopbackCommand, principal: Principal
    ) -> OAuthStartLoopbackResult:
        principal.require(Scope.SETTINGS_WRITE)
        if not cmd.is_local:
            raise ForbiddenError(
                "loopback unavailable on a remote gateway; paste an API key instead"
            )
        try:
            from durin.providers.openrouter_oauth import start_loopback_login

            # Binds the callback server (blocking socket ops) — off the loop.
            url = await asyncio.to_thread(start_loopback_login)
        except Exception as exc:  # noqa: BLE001
            raise UnavailableError(f"loopback login failed to start: {exc}") from exc
        return OAuthStartLoopbackResult(authorize_url=url)

    @route(
        "DELETE",
        "/api/v1/oauth/openrouter",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=OpenRouterDisconnectCommand,
        response_model=OpenRouterStatusResult,
        summary="Forget the OpenRouter API key",
    )
    async def openrouter_disconnect(
        self, cmd: OpenRouterDisconnectCommand, principal: Principal
    ) -> OpenRouterStatusResult:
        principal.require(Scope.SETTINGS_WRITE)
        from durin.providers.openrouter_oauth import disconnect as or_disconnect

        or_disconnect()
        return self._openrouter_status(can_loopback=False)

    # ------------------------------------------------------------------
    # GitHub (device flow) — one shared credential for skills + MCP
    # ------------------------------------------------------------------

    def _github_status(self) -> GithubStatusResult:
        from durin.security.github_device_auth import github_status as _probe

        st = _probe()
        return GithubStatusResult(
            connected=st.connected,
            reachable=st.reachable,
            source=st.source or None,
            login=st.login or None,
            scopes=st.scopes or None,
            rate_remaining=st.rate_remaining,
            rate_limit=st.rate_limit,
        )

    @route(
        "GET",
        "/api/v1/oauth/github/status",
        scope=Scope.SETTINGS_READ.value,
        request_model=GithubStatusQuery,
        response_model=GithubStatusResult,
        summary="Return GitHub connection status (live probe: login + rate budget)",
    )
    async def github_status(
        self, query: GithubStatusQuery, principal: Principal
    ) -> GithubStatusResult:
        principal.require(Scope.SETTINGS_READ)
        # gh subprocess + GitHub HTTP GET -> off the single gateway loop.
        return await asyncio.to_thread(self._github_status)

    @route(
        "POST",
        "/api/v1/oauth/github/start",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=GithubStartCommand,
        response_model=GithubStartResult,
        summary="Start the GitHub device-flow connect (returns the user code + URL)",
    )
    async def github_start(
        self, cmd: GithubStartCommand, principal: Principal
    ) -> GithubStartResult:
        principal.require(Scope.SETTINGS_WRITE)
        from durin.security.github_device_auth import (
            DEFAULT_SCOPE,
            PRIVATE_REPO_SCOPE,
            request_device_code,
        )

        scope = PRIVATE_REPO_SCOPE if cmd.private else DEFAULT_SCOPE
        try:
            ch = await asyncio.to_thread(request_device_code, scope=scope)
        except Exception as exc:  # noqa: BLE001
            raise UnavailableError(f"device code request failed: {exc}") from exc
        return GithubStartResult(
            flow_id=ch.flow_id,
            user_code=ch.user_code,
            verification_uri=ch.verification_uri,
            verification_uri_complete=ch.verification_uri_complete,
            interval=ch.interval,
            expires_in=ch.expires_in,
        )

    @route(
        "GET",
        "/api/v1/oauth/github/poll",
        scope=Scope.SETTINGS_READ.value,
        request_model=GithubPollQuery,
        response_model=GithubPollResult,
        summary="Poll the GitHub device flow for completion",
    )
    async def github_poll(
        self, query: GithubPollQuery, principal: Principal
    ) -> GithubPollResult:
        principal.require(Scope.SETTINGS_READ)
        flow_id = query.flow_id.strip()
        if not flow_id:
            raise ValidationFailedError("flow_id is required")
        from durin.security.github_device_auth import poll_flow

        res = await asyncio.to_thread(poll_flow, flow_id)
        if res.status == "authorized":
            st = await asyncio.to_thread(self._github_status)
            return GithubPollResult(status="authorized", connected=st.connected, login=st.login)
        return GithubPollResult(status=res.status, error=res.error or None)

    @route(
        "DELETE",
        "/api/v1/oauth/github",
        scope=Scope.SETTINGS_WRITE.value,
        request_model=GithubDisconnectCommand,
        response_model=GithubStatusResult,
        summary="Forget durin's stored GitHub token, then re-probe (gh/env may remain)",
    )
    async def github_disconnect(
        self, cmd: GithubDisconnectCommand, principal: Principal
    ) -> GithubStatusResult:
        principal.require(Scope.SETTINGS_WRITE)
        from durin.security.github_device_auth import forget_github_token

        # Forget durin's stored token, then re-probe: an ambient `gh`/env token can
        # still provide access, so the honest post-disconnect state may be "still
        # connected via gh", not a blanket disconnected.
        await asyncio.to_thread(forget_github_token)
        return await asyncio.to_thread(self._github_status)
