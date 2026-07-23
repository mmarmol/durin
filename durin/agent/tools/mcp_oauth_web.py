"""Gateway-driven interactive OAuth for remote MCP servers.

``durin mcp login`` runs the OAuth handshake from the CLI, opening a browser
server-side. The webui can't do that — the browser is the *user's*. This module
runs the same SDK handshake from the gateway but **surfaces the authorization
URL** to the caller (the webui opens it). The existing ``LoopbackCallback``
still captures the redirect on ``127.0.0.1``, so it works whenever the browser
and the gateway share a host (durin's normal webui deployment).

A sign-in is a background task: ``start()`` kicks it off and returns the URL
immediately; the task blocks on the loopback until the user authorizes, then the
SDK stores the tokens. At most one flow per server is in flight — a new
``start()`` for the same server aborts the stale flow and begins a fresh one
(retry is idempotent: an abandoned popup must never lock the server out), and
every flow carries an overall deadline so a wedged handshake cannot hold the
slot until a gateway restart.
"""
from __future__ import annotations

import asyncio
import base64
import os
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from durin.service.types import UnavailableError


async def _drive_auth(provider: Any, cfg: Any) -> None:
    """Force one authenticated request so the SDK runs the full OAuth handshake.

    Delegates to the transport-aware handshake driver so SSE servers complete
    cleanly (using the wrong transport stores the token but fails the post-token
    init with an opaque ExceptionGroup)."""
    from durin.agent.tools.mcp_oauth import drive_oauth_handshake

    await drive_oauth_handshake(provider, cfg)


def leaf_errors(exc: BaseException) -> list[BaseException]:
    """Flatten (possibly nested) ExceptionGroups to their leaf exceptions.

    The MCP session layers run in anyio task groups, so a failed handshake
    surfaces as ``ExceptionGroup: unhandled errors in a TaskGroup (1
    sub-exception)`` — a log line that hides the actual cause (e.g. the token
    endpoint's invalid_grant body) unless the leaves are named."""
    subs = getattr(exc, "exceptions", None)
    if not subs:
        return [exc]
    leaves: list[BaseException] = []
    for sub in subs:
        leaves.extend(leaf_errors(sub))
    return leaves


class PendingFlow:
    def __init__(self, task: asyncio.Task, callback: Any) -> None:
        self.task = task
        self.callback = callback


# state → GatewayCallback for sign-ins using the gateway-served callback
# route. In-process by design: the flow and the HTTP route live in the same
# gateway process, and states are single-use.
_gateway_callbacks: dict[str, "GatewayCallback"] = {}


class GatewayCallback:
    """OAuth callback captured by the gateway's own public HTTP route.

    Same seam as ``LoopbackCallback`` (state / start / stop / wait) so
    ``McpOauthFlows`` swaps implementations without restructuring. ``start``
    registers the state; the route resolves it exactly once.
    """

    def __init__(self) -> None:
        self.state = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
        self._future: asyncio.Future[tuple[str, str | None]] = (
            asyncio.get_running_loop().create_future()
        )

    def start(self) -> None:
        _gateway_callbacks[self.state] = self

    def stop(self) -> None:
        _gateway_callbacks.pop(self.state, None)

    def rekey(self, new_state: str) -> None:
        """Re-register under ``new_state`` — the mcp SDK's own OAuth state.

        ``GatewayCallback`` starts out registered under a state *durin*
        picked, but the SDK's ``OAuthClientProvider`` generates its own
        ``state`` and bakes it into the authorization URL; its
        ``callback_handler`` must return that exact state back (the SDK
        verifies it with ``secrets.compare_digest``). Once the SDK's state is
        known (parsed from the authorization URL), this moves the registry
        entry to that key so the real provider redirect — which can only
        arrive after the URL has been surfaced — resolves correctly instead
        of missing the registry and hanging until the callback timeout.
        No-op if the future is already resolved (nothing left to re-key).
        """
        if self._future.done():
            return
        _gateway_callbacks.pop(self.state, None)
        self.state = new_state
        _gateway_callbacks[self.state] = self

    async def wait(self) -> tuple[str, str | None]:
        return await self._future

    def _resolve(self, code: str, error: str | None) -> None:
        if self._future.done():
            return
        if error:
            self._future.set_exception(
                RuntimeError(f"OAuth provider returned error: {error}")
            )
        else:
            self._future.set_result((code, self.state))


def _state_from_url(url: str) -> str | None:
    """Return the ``state`` query param of an authorization URL, if any."""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except Exception:  # noqa: BLE001
        return None
    values = qs.get("state")
    return values[0] if values else None


def resolve_gateway_oauth_callback(
    state: str, *, code: str = "", error: str | None = None
) -> bool:
    """Hand a provider redirect to its pending flow. Single-use per state;
    unknown/reused states return False (the route answers 400)."""
    cb = _gateway_callbacks.pop(state, None)
    if cb is None:
        return False
    cb._resolve(code, error)
    return True


class McpOauthFlows:
    """Tracks in-flight gateway OAuth sign-ins, one per server.

    The provider builder, loopback factory, and connection driver are injectable
    so tests need no real browser, MCP SDK, or socket; production resolves the
    real ones lazily (keeping this module importable without the ``mcp`` SDK).
    """

    def __init__(
        self,
        *,
        provider_builder: Callable | None = None,
        loopback_factory: Callable | None = None,
        driver: Callable[[Any, Any], Awaitable[None]] | None = None,
        url_timeout: float = 30.0,
        flow_deadline: float = 600.0,
    ) -> None:
        self._provider_builder = provider_builder
        self._loopback_factory = loopback_factory
        self._driver = driver
        self._url_timeout = url_timeout
        # Overall cap on one sign-in attempt. Must exceed the 5-minute loopback
        # wait so a slow-but-live authorization still completes; its real job is
        # aborting a handshake wedged BEFORE the loopback wait (the MCP session
        # layers have no per-request timeouts of their own there).
        self._flow_deadline = flow_deadline
        self._pending: dict[str, PendingFlow] = {}

    def is_pending(self, server: str) -> bool:
        return server in self._pending

    def cancel(self, server: str) -> None:
        """Abort a pending flow (cancel the task, stop the loopback)."""
        pending = self._pending.pop(server, None)
        if pending is None:
            return
        pending.task.cancel()
        pending.callback.stop()

    async def start(
        self,
        server: str,
        cfg: Any,
        on_success: "Callable[[], Awaitable[None]] | None" = None,
        redirect_base: str | None = None,
    ) -> tuple[str, str]:
        """Begin a sign-in; return ``(authorization_url, state)``.

        ``on_success`` (optional) is awaited once the token is stored — used to
        reconnect the live connection race-free (the webui can't, since the popup
        closes a beat before the SDK finishes the token exchange).
        ``redirect_base`` (optional), when set, routes the OAuth redirect through
        the gateway's own public HTTP route (``GatewayCallback``) instead of the
        127.0.0.1 loopback — needed when the browser and the gateway do not share
        a host (e.g. a tailnet or public domain deployment).
        A flow already pending for the server is aborted and replaced — retry is
        idempotent. Raises ``UnavailableError`` if the callback can't bind or no
        URL surfaces.
        """
        if server in self._pending:
            logger.info(
                "MCP '{}' OAuth sign-in restarted; aborting the stale pending flow",
                server,
            )
            self.cancel(server)

        builder = self._provider_builder
        factory = self._loopback_factory
        if builder is None or factory is None:
            from durin.agent.tools.mcp_oauth import (
                LoopbackCallback,
                build_oauth_provider,
            )

            builder = builder or build_oauth_provider
            factory = factory or LoopbackCallback
        driver = self._driver or _drive_auth

        oc = cfg.oauth_config()
        port = oc.callback_port if oc else 1456
        redirect_uri: str | None = None
        if redirect_base:
            callback: Any = GatewayCallback()
            callback.start()
            redirect_uri = f"{redirect_base}/api/v1/mcp/oauth/callback"
        else:
            try:
                callback = factory(port=port)
                callback.start()
            except Exception as exc:  # noqa: BLE001
                raise UnavailableError(
                    f"could not start OAuth callback server: {exc}",
                    details={"name": server},
                ) from None

        try:
            if redirect_uri is not None:
                from durin.agent.tools.mcp_oauth import (
                    SecretsTokenStorage,
                    ensure_registration_covers,
                )

                await ensure_registration_covers(
                    SecretsTokenStorage(server, server_url=cfg.url or None), oc, redirect_uri
                )

            loop = asyncio.get_running_loop()
            url_future: asyncio.Future = loop.create_future()
            flow_errors: list[Exception] = []

            async def _redirect(authorization_url: str) -> None:
                # Gateway path only (redirect_uri is set): the mcp SDK's
                # OAuthClientProvider generates its OWN state and embeds it in
                # this URL. GatewayCallback started out registered under a
                # durin-chosen state the provider never sees, so re-key it to
                # the SDK's state here — before the URL is surfaced to the
                # caller, so this always runs before the real provider
                # redirect (which can only arrive after that point) hits the
                # gateway route.
                if redirect_uri is not None and isinstance(callback, GatewayCallback):
                    sdk_state = _state_from_url(authorization_url)
                    if sdk_state:
                        callback.rekey(sdk_state)
                if not url_future.done():
                    url_future.set_result(authorization_url)

            async def _callback() -> tuple[str, str | None]:
                return await asyncio.wait_for(callback.wait(), timeout=300)

            provider = builder(
                server,
                cfg,
                headless=False,
                redirect_handler=_redirect,
                callback_handler=_callback,
                redirect_uri=redirect_uri,
            )

            async def _run() -> None:
                try:
                    await asyncio.wait_for(driver(provider, cfg), timeout=self._flow_deadline)
                    logger.info("MCP '{}' OAuth sign-in completed (token stored)", server)
                    # A successful sign-in supersedes any interrupted-refresh
                    # marker; the handshake can complete without a token
                    # exchange (stored token, no in-memory expiry), so
                    # set_tokens — the usual clear point — may never run.
                    try:
                        from durin.agent.tools.mcp_oauth import SecretsTokenStorage

                        SecretsTokenStorage(
                            server, server_url=cfg.url or None
                        ).clear_refresh_marker()
                    except Exception:  # noqa: BLE001 — cleanup must not fail the flow
                        logger.warning("MCP '{}': refresh-marker clear failed", server)
                    if on_success is not None:
                        # Reconnect now that the token is stored — race-free, unlike
                        # the webui doing it on popup-close (which beats the token).
                        try:
                            await on_success()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "MCP '{}' post-OAuth reconnect failed: {}", server, exc
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    flow_errors.append(exc)
                    logger.warning(
                        "MCP '{}' OAuth sign-in failed: {}",
                        server,
                        "; ".join(
                            f"{type(e).__name__}: {e}" for e in leaf_errors(exc)
                        ),
                    )
                finally:
                    callback.stop()
                    # Pop only OUR entry: a restarted sign-in replaces the pending
                    # slot while the aborted task is still unwinding — its cleanup
                    # must not evict the fresh flow.
                    current = self._pending.get(server)
                    if current is not None and current.task is asyncio.current_task():
                        self._pending.pop(server, None)

            task = loop.create_task(_run())
        except Exception:  # noqa: BLE001 — must not leak the started callback state
            callback.stop()
            raise

        self._pending[server] = PendingFlow(task, callback)

        try:
            url = await asyncio.wait_for(
                asyncio.shield(url_future), timeout=self._url_timeout
            )
        except Exception:  # noqa: BLE001 — timeout or early flow failure
            self.cancel(server)
            # The flow task swallows its own exceptions (so a failure never
            # crashes the background task) — if it already finished before
            # producing a URL, surface its reason instead of a bare timeout
            # message that hides why (e.g. DCR refused by the provider).
            detail = ""
            if task.done() and flow_errors:
                exc = flow_errors[-1]
                detail = f": {type(exc).__name__}: {exc}"
            raise UnavailableError(
                f"OAuth flow did not produce an authorization URL{detail}",
                details={"name": server},
            ) from None
        logger.info("MCP '{}' OAuth flow started; authorize_url={}", server, url)
        return url, getattr(callback, "state", "")
