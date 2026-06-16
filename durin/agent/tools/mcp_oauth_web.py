"""Gateway-driven interactive OAuth for remote MCP servers.

``durin mcp login`` runs the OAuth handshake from the CLI, opening a browser
server-side. The webui can't do that — the browser is the *user's*. This module
runs the same SDK handshake from the gateway but **surfaces the authorization
URL** to the caller (the webui opens it). The existing ``LoopbackCallback``
still captures the redirect on ``127.0.0.1``, so it works whenever the browser
and the gateway share a host (durin's normal webui deployment).

A sign-in is a background task: ``start()`` kicks it off and returns the URL
immediately; the task blocks on the loopback until the user authorizes, then the
SDK stores the tokens. At most one flow per server is in flight.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from durin.service.types import ConflictError, UnavailableError


async def _drive_auth(provider: Any, cfg: Any) -> None:
    """Force one authenticated request so the SDK runs the full OAuth handshake."""
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with httpx.AsyncClient(
        headers=getattr(cfg, "headers", None) or None,
        follow_redirects=True,
        timeout=None,
        auth=provider,
    ) as http_client:
        async with streamable_http_client(cfg.url, http_client=http_client) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()


class PendingFlow:
    def __init__(self, task: asyncio.Task, callback: Any) -> None:
        self.task = task
        self.callback = callback


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
    ) -> None:
        self._provider_builder = provider_builder
        self._loopback_factory = loopback_factory
        self._driver = driver
        self._url_timeout = url_timeout
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

    async def start(self, server: str, cfg: Any) -> tuple[str, str]:
        """Begin a sign-in; return ``(authorization_url, state)``.

        Raises ``ConflictError`` if a flow is already pending for the server, and
        ``UnavailableError`` if the callback can't bind or no URL surfaces.
        """
        if server in self._pending:
            raise ConflictError(
                "OAuth sign-in already in progress", details={"name": server}
            )

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
        try:
            callback = factory(port=port)
            callback.start()
        except Exception as exc:  # noqa: BLE001
            raise UnavailableError(
                f"could not start OAuth callback server: {exc}",
                details={"name": server},
            ) from None

        loop = asyncio.get_running_loop()
        url_future: asyncio.Future = loop.create_future()

        async def _redirect(authorization_url: str) -> None:
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
        )

        async def _run() -> None:
            try:
                await driver(provider, cfg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP '{}' OAuth sign-in failed: {}", server, exc)
            finally:
                callback.stop()
                self._pending.pop(server, None)

        task = loop.create_task(_run())
        self._pending[server] = PendingFlow(task, callback)

        try:
            url = await asyncio.wait_for(
                asyncio.shield(url_future), timeout=self._url_timeout
            )
        except Exception:  # noqa: BLE001 — timeout or early flow failure
            self.cancel(server)
            raise UnavailableError(
                "OAuth flow did not produce an authorization URL",
                details={"name": server},
            ) from None
        return url, getattr(callback, "state", "")
