"""OAuth glue for remote MCP servers (SP-4).

durin does NOT reimplement OAuth — the `mcp` SDK's `OAuthClientProvider`
(an `httpx.Auth`) drives the full 2.1 auth-code + PKCE + DCR + refresh flow.
This module supplies only what the SDK leaves to the application:

* `SecretsTokenStorage` — persist OAuthToken + OAuthClientInformationFull in
  durin's secret store (`~/.durin/secrets.json`, mode 0600), keyed per server,
  instead of the oauth-cli-kit FileTokenStorage.
* A provider builder + redirect/callback handlers (Tasks 4b/4c).

Cold-load note (verified against mcp 1.27.2): the SDK's `_initialize` loads
tokens without recomputing expiry, and `is_token_valid()` treats a token with
no in-memory expiry as valid — so a stale access token simply 401s and the
SDK's `async_auth_flow` refreshes it. Storage therefore round-trips the
`OAuthToken` verbatim; it does NOT track an absolute expiry (OAuthToken has no
such field).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import os
import re
import socket
import threading
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from durin.security.secrets import (
    SecretNotFoundError,
    SecretStore,
    get_secret_store,
    make_ref,
    resolve_secret,
)


def _secret_name(prefix: str, server: str, url: str | None) -> str:
    """Env-var-safe secret name keyed by server (+ a short url hash).

    The url hash makes the creds URL-bound: if the configured URL changes,
    the lookup misses and the SDK re-runs the flow (opencode/openclaw
    invalidate-on-url-change). When ``url`` is None (tests / unknown), the
    name is server-only.
    """
    base = re.sub(r"[^A-Z0-9_]", "_", server.upper())
    if not base or not base[0].isalpha():
        base = "S_" + base
    if url:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8].upper()
        return f"MCP_{base}_{h}_{prefix}"
    return f"MCP_{base}_{prefix}"


class SecretsTokenStorage:
    """`mcp.client.auth.TokenStorage` backed by durin's secret store.

    Implements the four async methods the SDK Protocol requires:
    get_tokens / set_tokens / get_client_info / set_client_info. Tokens and
    client registration are stored as JSON-serialized pydantic models under
    two per-server secret entries.
    """

    def __init__(self, server: str, server_url: str | None = None) -> None:
        self._server = server
        self._url = server_url
        self._tokens_name = _secret_name("OAUTH_TOKENS", server, server_url)
        self._client_name = _secret_name("OAUTH_CLIENT", server, server_url)

    def _read(self, name: str) -> str | None:
        try:
            raw = resolve_secret(make_ref(name))
        except SecretNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            return None
        return raw if isinstance(raw, str) and raw.strip() else None

    def _write(self, name: str, blob: str) -> None:
        store = SecretStore().load()
        store.put(
            name,
            value=blob,
            service=f"mcp:{self._server}",
            description=f"MCP OAuth ({self._server})",
            scope=[f"mcp:{self._server}"],
            origin="oauth",
        )
        store.save()
        get_secret_store(reload=True)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read(self._tokens_name)
        if raw is None:
            return None
        try:
            return OAuthToken.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP '{}': stored OAuth token unreadable: {}", self._server, exc)
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._write(self._tokens_name, tokens.model_dump_json())

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read(self._client_name)
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP '{}': stored OAuth client info unreadable: {}", self._server, exc)
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._write(self._client_name, client_info.model_dump_json())

    def forget(self) -> bool:
        """Delete both secret entries (used by `durin mcp logout`)."""
        store = SecretStore().load()
        removed = False
        for name in (self._tokens_name, self._client_name):
            if store.remove(name):
                removed = True
        if removed:
            store.save()
            get_secret_store(reload=True)
        return removed


# ---- SP-4b: provider builder + headless redirect handler ----


class NeedsInteractiveAuthError(Exception):
    """Raised by the headless redirect handler: an agent run hit an OAuth
    server with no usable token and cannot pop a browser. The message names
    the `durin mcp login <server>` command the user must run."""


def _redirect_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}/callback"


def _client_metadata(cfg: Any, port: int) -> Any:
    from mcp.shared.auth import OAuthClientMetadata

    oc = cfg.oauth_config()
    return OAuthClientMetadata(
        client_name="durin",
        redirect_uris=[_redirect_uri(port)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=(oc.scope if oc else None),
    )


def make_headless_redirect_handler(server: str) -> Callable[[str], Awaitable[None]]:
    """Return a redirect handler that refuses to open a browser in agent runs."""

    async def _redirect(_authorization_url: str) -> None:
        raise NeedsInteractiveAuthError(
            f"MCP server '{server}' requires OAuth sign-in. "
            f"Run: durin mcp login {server}"
        )

    return _redirect


async def _headless_callback() -> tuple[str, str | None]:
    # Never reached in headless mode — the redirect handler raises first.
    raise NeedsInteractiveAuthError("interactive OAuth callback not available headless")


# Strong refs to fire-and-forget seed tasks so CPython doesn't GC them mid-run.
_PENDING_SEED_TASKS: set[asyncio.Task] = set()


def _seed_static_client(storage: SecretsTokenStorage, oc: Any, port: int) -> None:
    """Persist a static client registration so the SDK skips DCR.

    Best-effort: only writes if nothing is stored yet, so a later refresh/DCR
    result isn't clobbered. Runs the async write synchronously (called from
    __init__ outside of any running event loop).
    """
    async def _maybe() -> None:
        if await storage.get_client_info() is not None:
            return
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id=oc.client_id,
                client_secret=oc.client_secret,
                redirect_uris=[_redirect_uri(port)],
            )
        )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_maybe())
        return
    # Inside a running loop: schedule a task, keeping a strong reference so it
    # isn't garbage-collected before it runs (asyncio.create_task caveat).
    task = loop.create_task(_maybe())
    _PENDING_SEED_TASKS.add(task)
    task.add_done_callback(_PENDING_SEED_TASKS.discard)


def build_oauth_provider(
    server: str,
    cfg: Any,
    *,
    headless: bool,
    redirect_handler: Callable[[str], Awaitable[None]] | None = None,
    callback_handler: Callable[[], Awaitable[tuple[str, str | None]]] | None = None,
) -> Any:
    """Construct the SDK OAuthClientProvider for ``server``.

    ``headless=True`` (agent run) installs handlers that refuse to open a
    browser. ``headless=False`` (CLI) expects interactive handlers passed in.
    A static client_id (config override) is seeded into storage so the SDK
    skips dynamic registration.

    Returns an OAuthClientProvider, which is also an httpx.Auth.
    """
    from mcp.client.auth import OAuthClientProvider

    oc = cfg.oauth_config()
    port = oc.callback_port if oc else 1456
    storage = SecretsTokenStorage(server, server_url=cfg.url or None)

    if oc and oc.client_id:
        _seed_static_client(storage, oc, port)

    if headless:
        redirect_handler = make_headless_redirect_handler(server)
        callback_handler = _headless_callback

    return OAuthClientProvider(
        server_url=cfg.url,
        client_metadata=_client_metadata(cfg, port),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


# ---- SP-4c: loopback callback server + interactive handlers ----


class LoopbackCallback:
    """Localhost OAuth callback server for the interactive ``durin mcp login``.

    Serves GET /callback on 127.0.0.1 (and ::1), verifies the CSRF ``state``,
    and resolves :meth:`wait` with ``(code, state)`` — exactly the tuple the
    SDK's ``callback_handler`` must return.

    Usage::

        cb = LoopbackCallback(port=1456)
        cb.start()
        try:
            redirect_h, callback_h = make_interactive_handlers(cb)
            provider = build_oauth_provider(..., redirect_handler=redirect_h, callback_handler=callback_h)
            # ... run the OAuth flow ...
        finally:
            cb.stop()
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.state = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
        self._loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        self._future: asyncio.Future[tuple[str, str | None]] = self._loop.create_future()
        self._servers: list[http.server.HTTPServer] = []

    def start(self) -> None:
        """Start the callback server on 127.0.0.1 (and ::1 when available)."""
        handler = self._make_handler()
        for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):

            class _Srv(http.server.HTTPServer):
                address_family = family

            try:
                srv = _Srv((host, self.port), handler)
            except OSError as exc:
                logger.debug("loopback bind {}:{} failed: {}", host, self.port, exc)
                continue
            if self.port == 0:
                # OS assigned a free port — adopt it for all subsequent binds.
                self.port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self._servers.append(srv)
        if not self._servers:
            raise RuntimeError(f"could not start loopback callback server on :{self.port}")

    def _resolve(self, code: str, state: str | None) -> None:
        if not self._future.done():
            self._loop.call_soon_threadsafe(self._future.set_result, (code, state))

    def _make_handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        cb = self

        class _H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if not parsed.path.endswith("/callback"):
                    self.send_response(404)
                    self.end_headers()
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                code = (qs.get("code") or [None])[0]
                got = (qs.get("state") or [None])[0]
                ok = bool(code) and got == cb.state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = (
                    "Connected to durin. You can close this tab."
                    if ok
                    else "Authorization failed. Return to durin and retry."
                )
                self.wfile.write(
                    f"<!doctype html><meta charset=utf-8>"
                    f"<body style='font-family:sans-serif;padding:2rem'>"
                    f"<h2>{msg}</h2></body>".encode()
                )
                if ok:
                    cb._resolve(code, got)  # type: ignore[arg-type]

            def log_message(self, *a: Any) -> None:  # silence
                return

        return _H

    async def wait(self) -> tuple[str, str | None]:
        """Await the OAuth code + state. Resolves once the browser redirects back."""
        return await self._future

    def stop(self) -> None:
        """Shut down the callback servers."""
        for srv in self._servers:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self._servers = []


def make_interactive_handlers(
    callback: LoopbackCallback,
    *,
    open_browser: bool = True,
) -> tuple[Callable[[str], Awaitable[None]], Callable[[], Awaitable[tuple[str, str | None]]]]:
    """Return ``(redirect_handler, callback_handler)`` for ``durin mcp login``.

    ``redirect_handler`` opens the user's browser at the authorize URL.
    ``callback_handler`` blocks until the loopback server captures the code,
    with a 5-minute timeout (matching the SDK's own default).
    """

    async def _redirect(authorization_url: str) -> None:
        from rich.console import Console as _Console

        _Console().print(
            f"[dim]Opening browser for authorization…[/dim]\n"
            f"[dim]If it doesn't open, visit:[/dim] {authorization_url}"
        )
        if open_browser:
            import webbrowser

            try:
                webbrowser.open(authorization_url)
            except Exception:  # noqa: BLE001
                pass

    async def _callback() -> tuple[str, str | None]:
        return await asyncio.wait_for(callback.wait(), timeout=300)

    return _redirect, _callback
