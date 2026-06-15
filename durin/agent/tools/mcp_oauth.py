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

import hashlib
import re

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
