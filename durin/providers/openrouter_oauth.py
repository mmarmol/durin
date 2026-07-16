"""OpenRouter OAuth PKCE flow — obtain a user-controlled API key.

OpenRouter's OAuth is simpler than a token-based provider login: the PKCE
exchange at ``/api/v1/auth/keys`` returns a plain, user-controlled API key
(``sk-or-v1-…``) — no client registration, no refresh tokens. The key is
stored exactly like a manually pasted one: plaintext in the secret store,
a ``${secret:}`` reference in ``providers.openrouter.api_key`` — so nothing
downstream knows (or cares) that it came from OAuth.

Two ways in. OpenRouter has no device-code flow, and — unlike Codex, whose
registered redirect is fixed at ``localhost:1455`` — the callback URL is ours
to choose. ``start_loopback_login`` binds ``127.0.0.1`` on an ephemeral port
with a random path nonce (browser and gateway must share a host). When a
public base URL resolves instead (operator-set ``gateway.public_url`` or the
webui's own origin), ``start_gateway_login`` routes the redirect through the
gateway's own ``/api/v1/mcp/oauth/callback`` route — the same one MCP OAuth
uses — so a remote webui gets one-click connect too. Both share the PKCE and
key-exchange helpers below; only the callback transport differs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.server
import os
import threading
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

AUTH_URL = "https://openrouter.ai/auth"
KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"

_SECRET_NAME = "OPENROUTER_API_KEY"
_SECRET_REF = f"${{secret:{_SECRET_NAME}}}"


def _client() -> httpx.Client:
    """HTTP client factory. Tests monkeypatch this to inject a MockTransport."""
    return httpx.Client(timeout=httpx.Timeout(15.0))


def _gen_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _build_authorize_url(callback_url: str, challenge: str) -> str:
    params = {
        "callback_url": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str, code_verifier: str) -> str:
    """Exchange the authorization code for a user-controlled API key."""
    with _client() as client:
        resp = client.post(
            KEYS_URL,
            json={
                "code": code,
                "code_verifier": code_verifier,
                "code_challenge_method": "S256",
            },
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"key exchange failed: HTTP {resp.status_code}")
    key = (resp.json() or {}).get("key", "")
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("key exchange returned no key")
    return key.strip()


def store_key(key: str) -> None:
    """Persist the key the same way a manual paste does: secret store +
    ``${secret:}`` reference in config. Same secret name as the settings
    surface (``openrouter_API_KEY`` sanitizes to ``OPENROUTER_API_KEY``),
    so connect overwrites a stale manual key instead of duplicating it."""
    from durin.config.loader import load_config, save_config
    from durin.security.secrets import store_secret

    ref = store_secret(
        _SECRET_NAME,
        key,
        service="provider:openrouter",
        scope=["provider:openrouter"],
        description="OpenRouter API key",
        origin="oauth",
    )
    config = load_config()
    if config.providers.openrouter.api_key != ref:
        config.providers.openrouter.api_key = ref
        save_config(config)


@dataclass
class OpenRouterKeyStatus:
    connected: bool
    api_key_hint: str | None = None


def key_status() -> OpenRouterKeyStatus:
    """Whether an OpenRouter key is configured (manual or OAuth-obtained)."""
    try:
        from durin.config.loader import load_config
        from durin.security.secrets import mask_secret_hint

        api_key = load_config().providers.openrouter.api_key
    except Exception:  # noqa: BLE001
        return OpenRouterKeyStatus(connected=False)
    if not api_key:
        return OpenRouterKeyStatus(connected=False)
    return OpenRouterKeyStatus(connected=True, api_key_hint=mask_secret_hint(api_key))


def disconnect() -> bool:
    """Forget the OpenRouter key: clear the config field and, when it points
    at durin's own secret, delete the secret too."""
    removed = False
    try:
        from durin.config.loader import load_config, save_config

        config = load_config()
        api_key = config.providers.openrouter.api_key
        if api_key:
            config.providers.openrouter.api_key = None
            save_config(config)
            removed = True
        if api_key == _SECRET_REF:
            from durin.security.secrets import SecretStore, get_secret_store

            store = SecretStore().load()
            if store.remove(_SECRET_NAME):
                store.save()
                get_secret_store(reload=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not disconnect openrouter: {}", exc)
    return removed


class _CallbackResult:
    def __init__(self) -> None:
        self.code: str | None = None
        self.done = threading.Event()


def _make_callback_handler(nonce_path: str, result: _CallbackResult):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != nonce_path:
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            if code:
                result.code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = (
                "Connected to durin. You can close this tab now."
                if code
                else "Authorization failed. Go back to durin and try again."
            )
            self.wfile.write(
                f"<!doctype html><meta charset=utf-8>"
                f"<body style='font-family:sans-serif;padding:2rem'><h2>{msg}</h2></body>".encode()
            )
            result.done.set()

        def log_message(self, *args: Any) -> None:  # silence stderr logging
            return

    return _Handler


def _start_callback_server(result: _CallbackResult) -> tuple[http.server.HTTPServer, str]:
    """Bind 127.0.0.1 on an ephemeral port; return (server, callback_url).

    The random path nonce means a local port-scanner can't feed us a forged
    ``code`` — only the browser redirected by OpenRouter knows the full URL.
    """
    nonce = base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
    nonce_path = f"/callback/{nonce}"
    handler = _make_callback_handler(nonce_path, result)
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}{nonce_path}"


_loopback_lock = threading.Lock()
_loopback_state: dict[str, Any] = {"thread": None, "url": None}


def start_loopback_login(*, max_wait_s: float = 180.0) -> str:
    """Start the loopback PKCE login and return the authorize URL.

    LOCAL installs only (browser and gateway on the same host). The callback
    server is listening before this returns; a background thread waits for
    the code, exchanges it, and stores the key — the caller polls
    ``key_status()`` for completion.
    """
    with _loopback_lock:
        existing = _loopback_state.get("thread")
        if existing is not None and existing.is_alive() and _loopback_state.get("url"):
            return _loopback_state["url"]  # an attempt is already in flight

        verifier, challenge = _gen_pkce()
        result = _CallbackResult()
        srv, callback_url = _start_callback_server(result)
        url = _build_authorize_url(callback_url, challenge)

        def _run() -> None:
            try:
                if result.done.wait(timeout=max_wait_s) and result.code:
                    store_key(exchange_code(result.code, verifier))
            except Exception as exc:  # noqa: BLE001
                logger.debug("openrouter loopback login ended: {}", exc)
            finally:
                try:
                    srv.shutdown()
                except Exception:  # noqa: BLE001
                    pass

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _loopback_state["thread"] = thread
        _loopback_state["url"] = url
        return url


_gateway_state: dict[str, Any] = {"task": None, "url": None}


async def start_gateway_login(base: str, *, max_wait_s: float = 180.0) -> str:
    """Start the PKCE login routed through the gateway's own OAuth callback
    route and return the authorize URL immediately.

    Used instead of ``start_loopback_login`` when a public base URL resolves
    (browser and gateway need not share a host — a remote webui gets the same
    one-click connect). A background asyncio task waits for the redirect,
    exchanges the code, and stores the key — same as the loopback path — then
    always deregisters the callback state.

    No ``threading.Lock`` needed here (unlike the loopback path): this runs
    on the single gateway event loop with no ``await`` between the in-flight
    check and registering the new state, so the check is already atomic.
    An attempt already in flight returns the same URL (mirrors
    ``start_loopback_login``'s idempotence).
    """
    existing = _gateway_state.get("task")
    if existing is not None and not existing.done() and _gateway_state.get("url"):
        return _gateway_state["url"]

    from durin.agent.tools.mcp_oauth_web import GatewayCallback

    verifier, challenge = _gen_pkce()
    callback = GatewayCallback()
    callback.start()
    try:
        # The shared callback route resolves on `state`; OpenRouter appends
        # `&code=...` to whatever callback_url we hand it, so the state
        # travels through as a query param baked into the callback_url itself.
        callback_url = f"{base}/api/v1/mcp/oauth/callback?state={callback.state}"
        url = _build_authorize_url(callback_url, challenge)

        async def _run() -> None:
            try:
                code, _state = await asyncio.wait_for(callback.wait(), timeout=max_wait_s)
                if code:
                    key = await asyncio.to_thread(exchange_code, code, verifier)
                    store_key(key)
            except Exception as exc:  # noqa: BLE001
                logger.debug("openrouter gateway login ended: {}", exc)
            finally:
                callback.stop()

        task = asyncio.create_task(_run())
    except Exception:  # noqa: BLE001 — must not leak the started callback state
        callback.stop()
        raise
    _gateway_state["task"] = task
    _gateway_state["url"] = url
    return url


def login_loopback_blocking(
    print_fn: Callable[[str], None],
    *,
    open_browser: bool = True,
    max_wait_s: float = 180.0,
) -> None:
    """Run the loopback flow to completion (CLI use). Opens the browser, waits."""
    import webbrowser

    verifier, challenge = _gen_pkce()
    result = _CallbackResult()
    srv, callback_url = _start_callback_server(result)
    url = _build_authorize_url(callback_url, challenge)
    try:
        print_fn(f"Open: {url}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        print_fn("Waiting for authorization in the browser...")
        if not result.done.wait(timeout=max_wait_s) or not result.code:
            raise RuntimeError("loopback login timed out")
        store_key(exchange_code(result.code, verifier))
    finally:
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
