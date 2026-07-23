"""OpenAI Codex device-code OAuth flow and session helpers.

Ports the device-code flow used by hermes/openclaw so durin can authorize a
ChatGPT account from a remote/headless host (and the webui). Tokens are
persisted through ``oauth-cli-kit``'s ``FileTokenStorage`` into the same
``codex.json`` that ``OpenAICodexProvider`` reads via ``get_token()`` — nothing
downstream needs to know the token came from device-code.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from loguru import logger

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
TOKEN_URL = f"{ISSUER}/oauth/token"
DEVICE_USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
DEVICE_REDIRECT_URI = f"{ISSUER}/deviceauth/callback"
VERIFICATION_URI = f"{ISSUER}/codex/device"
ORIGINATOR = "codex_cli_rs"
_DEFAULT_TTL_S = 3600

# Loopback (browser) flow — local installs only.
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
SCOPE = "openid profile email offline_access"
LOOPBACK_PORT = 1455
# Must match exactly what's registered for the Codex client_id.
LOOPBACK_REDIRECT = f"http://localhost:{LOOPBACK_PORT}/auth/callback"


def _client() -> httpx.Client:
    """HTTP client factory. Tests monkeypatch this to inject a MockTransport."""
    return httpx.Client(timeout=httpx.Timeout(15.0))


def _decode_jwt_claims(access_token: str) -> dict[str, Any]:
    if not isinstance(access_token, str) or not access_token.strip():
        return {}
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        return {}


def account_id_from_jwt(access_token: str) -> str | None:
    claims = _decode_jwt_claims(access_token)
    acct = claims.get("https://api.openai.com/auth", {})
    val = acct.get("chatgpt_account_id") if isinstance(acct, dict) else None
    return val if isinstance(val, str) and val else None


def plan_from_jwt(access_token: str) -> str | None:
    claims = _decode_jwt_claims(access_token)
    acct = claims.get("https://api.openai.com/auth", {})
    val = acct.get("chatgpt_plan_type") if isinstance(acct, dict) else None
    return val if isinstance(val, str) and val else None


def email_from_jwt(access_token: str) -> str | None:
    claims = _decode_jwt_claims(access_token)
    val = claims.get("https://api.openai.com/profile.email") or claims.get("email")
    return val if isinstance(val, str) and val else None


def expiry_ms_from_jwt(access_token: str) -> int:
    claims = _decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp > 0:
        return int(exp) * 1000
    return int((time.time() + _DEFAULT_TTL_S) * 1000)


_CODEX_SECRET_NAME = "OPENAI_CODEX_OAUTH"


def _kit_file_storage() -> Any:
    """The kit's previous on-disk store — kept only for one-time migration."""
    from oauth_cli_kit.storage import FileTokenStorage

    return FileTokenStorage(token_filename="codex.json", import_codex_cli=False)


def _codex_lock_dir() -> Path:
    from durin.config.paths import get_config_path

    d = get_config_path().parent / "oauth"
    d.mkdir(parents=True, exist_ok=True)
    return d


class _CodexSecretsStorage:
    """``oauth-cli-kit`` ``TokenStorage`` backed by durin's secret store.

    The token blob lives in ``~/.durin/secrets.json`` (mode 0600), like every
    other credential, instead of the kit's own app-data dir. The kit's
    ``get_token()`` still drives refresh + locking — we only swap where the
    bytes land. On first ``load()`` a token left in the kit's file store is
    migrated into secrets.
    """

    def load(self) -> Any:
        from oauth_cli_kit.models import OAuthToken

        from durin.security.secrets import SecretNotFoundError, resolve_secret

        raw: Any = None
        try:
            raw = resolve_secret(f"${{secret:{_CODEX_SECRET_NAME}}}")
        except SecretNotFoundError:
            raw = None
        except Exception:  # noqa: BLE001
            raw = None
        if isinstance(raw, str) and raw.strip():
            try:
                d = json.loads(raw)
                return OAuthToken(
                    access=d.get("access", ""),
                    refresh=d.get("refresh", ""),
                    expires=int(d.get("expires", 0)),
                    account_id=d.get("account_id"),
                )
            except Exception:  # noqa: BLE001
                return None
        # One-time migration from the kit's previous file store.
        legacy = _kit_file_storage().load()
        if legacy is not None and getattr(legacy, "access", None):
            self.save(legacy)
            return legacy
        return None

    def save(self, token: Any) -> None:
        from durin.security.secrets import store_secret

        blob = json.dumps(
            {
                "access": token.access,
                "refresh": getattr(token, "refresh", ""),
                "expires": getattr(token, "expires", 0),
                "account_id": getattr(token, "account_id", None),
            }
        )
        store_secret(
            _CODEX_SECRET_NAME,
            blob,
            service="provider:openai_codex",
            scope=["provider:openai_codex"],
            description="OpenAI Codex OAuth token",
            origin="oauth",
        )

    def get_token_path(self) -> Path:
        # Data lives in secrets.json; this path is only used for the kit's
        # cross-process refresh lock (``<path>.lock``).
        return _codex_lock_dir() / "codex.json"


def _strict_storage() -> Any:
    return _CodexSecretsStorage()


def codex_token_present() -> bool:
    """True when durin holds a usable Codex token (in the secret store)."""
    try:
        return _strict_storage().load() is not None
    except Exception:  # noqa: BLE001
        return False


@dataclass
class DeviceCodeChallenge:
    user_code: str
    verification_uri: str
    device_auth_id: str
    interval: int
    expires_in: int


@dataclass
class PollResult:
    status: Literal["pending", "ok", "error"]
    token: Any | None = None  # oauth_cli_kit.models.OAuthToken
    error: str | None = None


def request_device_code() -> DeviceCodeChallenge:
    with _client() as client:
        resp = client.post(
            DEVICE_USERCODE_URL,
            json={"client_id": CLIENT_ID},
            headers={"Content-Type": "application/json", "originator": ORIGINATOR},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"device code request failed: HTTP {resp.status_code}")
    data = resp.json()
    user_code = data.get("user_code", "")
    device_auth_id = data.get("device_auth_id", "")
    if not user_code or not device_auth_id:
        raise RuntimeError("device code response missing required fields")
    return DeviceCodeChallenge(
        user_code=user_code,
        verification_uri=VERIFICATION_URI,
        device_auth_id=device_auth_id,
        interval=max(3, int(data.get("interval", 5))),
        expires_in=int(data.get("expires_in", 900)),
    )


def poll_once(device_auth_id: str, user_code: str) -> PollResult:
    """One poll tick. ``pending`` while the user has not approved yet."""
    with _client() as client:
        resp = client.post(
            DEVICE_TOKEN_URL,
            json={"device_auth_id": device_auth_id, "user_code": user_code},
            headers={"Content-Type": "application/json", "originator": ORIGINATOR},
        )
    if resp.status_code in (403, 404):
        return PollResult(status="pending")
    if resp.status_code != 200:
        return PollResult(status="error", error=f"poll HTTP {resp.status_code}")
    code_resp = resp.json()
    authorization_code = code_resp.get("authorization_code", "")
    code_verifier = code_resp.get("code_verifier", "")
    if not authorization_code or not code_verifier:
        return PollResult(status="error", error="missing authorization_code/code_verifier")
    try:
        token = _exchange_and_store(authorization_code, code_verifier)
    except Exception as exc:  # noqa: BLE001
        return PollResult(status="error", error=str(exc))
    return PollResult(status="ok", token=token)


def _exchange_and_store(
    authorization_code: str,
    code_verifier: str,
    redirect_uri: str = DEVICE_REDIRECT_URI,
) -> Any:
    from oauth_cli_kit.models import OAuthToken

    with _client() as client:
        resp = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": redirect_uri,
                "client_id": CLIENT_ID,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"token exchange failed: HTTP {resp.status_code}")
    tokens = resp.json()
    access = tokens.get("access_token", "")
    if not access:
        raise RuntimeError("token exchange returned no access_token")
    token = OAuthToken(
        access=access,
        refresh=tokens.get("refresh_token", ""),
        expires=expiry_ms_from_jwt(access),
        account_id=account_id_from_jwt(access),
    )
    _strict_storage().save(token)
    return token


@dataclass
class CodexSessionInfo:
    email: str | None
    plan: str | None
    source: Literal["durin", "codex-cli"]


def _session_from_access(access: str, source: str) -> CodexSessionInfo:
    return CodexSessionInfo(
        email=email_from_jwt(access),
        plan=plan_from_jwt(access),
        source=source,  # type: ignore[arg-type]
    )


def _read_codex_cli_session() -> CodexSessionInfo | None:
    """Detect (do NOT adopt) an official Codex CLI session at ~/.codex/auth.json."""
    path = Path.home() / ".codex" / "auth.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    access = (data.get("tokens", {}) or {}).get("access_token") or data.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    return _session_from_access(access, "codex-cli")


def existing_codex_session() -> CodexSessionInfo | None:
    """Return durin's own session if present, else a detected Codex CLI session."""
    try:
        token = _strict_storage().load()
    except Exception:  # noqa: BLE001
        token = None
    if token and getattr(token, "access", None):
        return _session_from_access(token.access, "durin")
    return _read_codex_cli_session()


def disconnect() -> bool:
    """Forget the Codex token: delete the secret and any legacy kit file."""
    removed = False
    try:
        from durin.security.secrets import SecretStore, get_secret_store

        store = SecretStore().load()
        if store.remove(_CODEX_SECRET_NAME):
            get_secret_store(reload=True)
            removed = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not remove codex secret: {}", exc)
    # Clean up the kit's previous file store (+ lock), if still around.
    try:
        legacy = _kit_file_storage().get_token_path()
        for path in (legacy, legacy.with_suffix(".lock")):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("could not remove {}: {}", path, exc)
    except Exception:  # noqa: BLE001
        pass
    return removed


def login_blocking(
    print_fn: Callable[[str], None],
    *,
    sleep_fn: Callable[[float], None] | None = None,
    now_fn: Callable[[], float] | None = None,
    max_wait_s: int = 15 * 60,
) -> Any:
    """Run the full device-code flow to completion (CLI use)."""
    sleep_fn = sleep_fn or time.sleep
    now_fn = now_fn or time.monotonic
    challenge = request_device_code()
    print_fn(f"Open: {challenge.verification_uri}")
    print_fn(f"Enter the code: {challenge.user_code}")
    print_fn("Waiting for authorization... (Ctrl+C to cancel)")
    start = now_fn()
    while now_fn() - start < max_wait_s:
        sleep_fn(challenge.interval)
        res = poll_once(challenge.device_auth_id, challenge.user_code)
        if res.status == "ok":
            return res.token
        if res.status == "error":
            raise RuntimeError(res.error or "device-code login failed")
    raise RuntimeError("device-code login timed out")


def _gen_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _build_authorize_url(challenge: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": LOOPBACK_REDIRECT,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": ORIGINATOR,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class _CallbackResult:
    def __init__(self) -> None:
        self.code: str | None = None
        self.done = threading.Event()


def _make_callback_handler(state: str, result: _CallbackResult):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if not parsed.path.endswith("/auth/callback"):
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [None])[0]
            got_state = (qs.get("state") or [None])[0]
            ok = bool(code) and got_state == state
            if ok:
                result.code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = (
                "Connected to durin. You can close this tab now."
                if ok
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


def _start_callback_servers(state: str, result: _CallbackResult) -> list[http.server.HTTPServer]:
    """Serve the OAuth callback on 127.0.0.1 AND ::1.

    The Codex client redirect_uri is ``http://localhost:1455/auth/callback``;
    browsers resolve ``localhost`` to 127.0.0.1 (Chromium/Brave/Safari). The
    kit's server bound only the first ``getaddrinfo`` result (::1 on macOS),
    so the redirect hit 127.0.0.1 and got ECONNREFUSED. Bind both stacks.
    """
    handler = _make_callback_handler(state, result)
    servers: list[http.server.HTTPServer] = []
    for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):

        class _Srv(http.server.HTTPServer):
            address_family = family

        try:
            srv = _Srv((host, LOOPBACK_PORT), handler)
        except OSError as exc:
            logger.debug("loopback bind {}:{} failed: {}", host, LOOPBACK_PORT, exc)
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    return servers


_loopback_lock = threading.Lock()
_loopback_state: dict[str, Any] = {"thread": None, "url": None}


def start_loopback_login(*, max_wait_s: float = 180.0) -> str:
    """Start the loopback PKCE login and return the authorize URL.

    LOCAL webui installs only. We serve the OAuth callback ourselves on
    127.0.0.1:1455 (and ::1) — the callback server is listening *before* this
    returns, so the browser's redirect can never race it — then a background
    thread waits for the code, exchanges it, and writes the token to
    ``codex.json``. Unlike device-code, no ChatGPT security toggle is needed.
    """
    with _loopback_lock:
        existing = _loopback_state.get("thread")
        if existing is not None and existing.is_alive() and _loopback_state.get("url"):
            return _loopback_state["url"]  # an attempt is already in flight

        verifier, challenge = _gen_pkce()
        state = base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
        result = _CallbackResult()
        servers = _start_callback_servers(state, result)
        if not servers:
            raise RuntimeError(f"could not start loopback callback server on :{LOOPBACK_PORT}")
        url = _build_authorize_url(challenge, state)

        def _run() -> None:
            try:
                if result.done.wait(timeout=max_wait_s) and result.code:
                    _exchange_and_store(result.code, verifier, redirect_uri=LOOPBACK_REDIRECT)
            except Exception as exc:  # noqa: BLE001
                logger.debug("codex loopback login ended: {}", exc)
            finally:
                for srv in servers:
                    try:
                        srv.shutdown()
                    except Exception:  # noqa: BLE001
                        pass

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _loopback_state["thread"] = thread
        _loopback_state["url"] = url
        return url


def login_loopback_blocking(
    print_fn: Callable[[str], None],
    *,
    open_browser: bool = True,
    max_wait_s: float = 180.0,
) -> Any:
    """Run the loopback flow to completion (CLI use). Opens the browser, waits.

    Same 127.0.0.1+::1 callback server as the webui path — fixes the kit's
    IPv6-only bind that left the browser redirect refused on macOS.
    """
    import webbrowser

    verifier, challenge = _gen_pkce()
    state = base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
    result = _CallbackResult()
    servers = _start_callback_servers(state, result)
    if not servers:
        raise RuntimeError(f"could not start loopback callback server on :{LOOPBACK_PORT}")
    url = _build_authorize_url(challenge, state)
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
        return _exchange_and_store(result.code, verifier, redirect_uri=LOOPBACK_REDIRECT)
    finally:
        for srv in servers:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
