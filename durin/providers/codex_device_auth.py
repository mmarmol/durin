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
