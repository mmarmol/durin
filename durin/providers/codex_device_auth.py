"""OpenAI Codex device-code OAuth flow and session helpers.

Ports the device-code flow used by hermes/openclaw so durin can authorize a
ChatGPT account from a remote/headless host (and the webui). Tokens are
persisted through ``oauth-cli-kit``'s ``FileTokenStorage`` into the same
``codex.json`` that ``OpenAICodexProvider`` reads via ``get_token()`` — nothing
downstream needs to know the token came from device-code.
"""

from __future__ import annotations

import base64
import json
import threading
import time
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


def _strict_storage() -> Any:
    """``FileTokenStorage`` for codex.json with silent CLI import disabled."""
    from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
    from oauth_cli_kit.storage import FileTokenStorage

    return FileTokenStorage(
        token_filename=OPENAI_CODEX_PROVIDER.token_filename,
        import_codex_cli=False,
    )


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


def _exchange_and_store(authorization_code: str, code_verifier: str) -> Any:
    from oauth_cli_kit.models import OAuthToken

    with _client() as client:
        resp = client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": authorization_code,
                "redirect_uri": DEVICE_REDIRECT_URI,
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
    """Delete durin's codex.json (+ .lock). True if anything was removed."""
    try:
        token_path = _strict_storage().get_token_path()
    except Exception:  # noqa: BLE001
        return False
    removed = False
    for path in (token_path, token_path.with_suffix(".lock")):
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("could not remove {}: {}", path, exc)
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
    print_fn(f"Abrí: {challenge.verification_uri}")
    print_fn(f"Ingresá el código: {challenge.user_code}")
    print_fn("Esperando la autorización... (Ctrl+C para cancelar)")
    start = now_fn()
    while now_fn() - start < max_wait_s:
        sleep_fn(challenge.interval)
        res = poll_once(challenge.device_auth_id, challenge.user_code)
        if res.status == "ok":
            return res.token
        if res.status == "error":
            raise RuntimeError(res.error or "device-code login failed")
    raise RuntimeError("device-code login timed out")


_loopback_lock = threading.Lock()
_loopback_state: dict[str, Any] = {"thread": None, "url": None}


def start_loopback_login(*, wait_url_s: float = 6.0) -> str:
    """Start the loopback PKCE login in a background thread; return the authorize URL.

    For LOCAL webui installs only: ``oauth-cli-kit`` serves the OAuth callback on
    ``localhost:1455`` — reachable only when the browser runs on the gateway
    machine — and writes the token to ``codex.json`` on success. Unlike
    device-code, the loopback flow does not require enabling device authorization
    in ChatGPT's security settings. The kit also opens the gateway's default
    browser; the returned URL is surfaced as a manual fallback.
    """
    from oauth_cli_kit import login_oauth_interactive

    with _loopback_lock:
        existing = _loopback_state.get("thread")
        if existing is not None and existing.is_alive() and _loopback_state.get("url"):
            return _loopback_state["url"]  # an attempt is already in flight
        _loopback_state["url"] = None
        captured: list[str] = []

        def _run() -> None:
            try:
                login_oauth_interactive(
                    print_fn=lambda s: captured.append(str(s)),
                    prompt_fn=lambda s: "",
                    originator=ORIGINATOR,
                    storage=_strict_storage(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("codex loopback login ended: {}", exc)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        _loopback_state["thread"] = thread

    deadline = time.monotonic() + wait_url_s
    prefix = f"{ISSUER}/oauth/authorize"
    while time.monotonic() < deadline:
        url = next((c for c in captured if c.startswith(prefix)), None)
        if url:
            _loopback_state["url"] = url
            return url
        time.sleep(0.1)
    raise RuntimeError("could not obtain the authorization URL")
