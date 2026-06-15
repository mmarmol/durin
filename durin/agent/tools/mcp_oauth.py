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
import hashlib
import re
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


def _seed_static_client(storage: SecretsTokenStorage, oc: Any, port: int) -> None:
    """Persist a static client registration so the SDK skips DCR.

    Best-effort: only writes if nothing is stored yet, so a later refresh/DCR
    result isn't clobbered. Runs the async write synchronously (called from
    __init__ outside of any running event loop).
    """
    from mcp.shared.auth import OAuthClientInformationFull as _OCIF

    async def _maybe() -> None:
        if await storage.get_client_info() is not None:
            return
        await storage.set_client_info(
            _OCIF(
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
    # Inside a running loop: schedule as a fire-and-forget task.
    loop.create_task(_maybe())


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
