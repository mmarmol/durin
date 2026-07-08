"""GitHub device-flow connect for durin's shared GitHub credential.

Mirrors the Copilot device flow (``durin/providers/github_copilot_provider.py``)
but uses durin's own OAuth App (client id ``github_auth.DURIN_GITHUB_CLIENT_ID``)
and minimal scope, and persists the raw access token as the shared ``GITHUB_OAUTH``
secret so ``github_auth.resolve_github_token`` (and thus skills, MCP discovery, and
the GitHub MCP server) can read it.

Split into ``start_device_flow`` (get the user code + URL) and
``exchange_device_code`` (one poll of the token endpoint) so the web UI can poll
from the browser instead of blocking the server on a long-lived request.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from secrets import token_urlsafe

from durin.security.github_auth import DURIN_GITHUB_CLIENT_ID as CLIENT_ID
from durin.security.github_auth import SHARED_SECRET_NAME

USER_URL = "https://api.github.com/user"

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# Minimal privilege: identify the user + read public repos. Private-repo access
# (`repo`) is requested only when a private action needs it (escalation).
DEFAULT_SCOPE = "read:user"
PRIVATE_REPO_SCOPE = "read:user repo"

_USER_AGENT = "durin"

# GitHub device-flow poll errors -> our status vocabulary.
_ERROR_STATUS = {
    "authorization_pending": "pending",
    "slow_down": "slow_down",
    "expired_token": "expired",
    "access_denied": "denied",
}

Poster = Callable[[str, dict], dict]


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    expires_in: int


@dataclass
class Exchange:
    status: str  # authorized | pending | slow_down | expired | denied | error
    access_token: str = ""
    scope: str = ""
    error: str = ""


def _default_poster(url: str, data: dict) -> dict:
    import httpx

    with httpx.Client(timeout=httpx.Timeout(20.0, connect=20.0), trust_env=True) as client:
        resp = client.post(
            url,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            data=data,
        )
        resp.raise_for_status()
        return resp.json()


def start_device_flow(*, scope: str = DEFAULT_SCOPE, poster: Poster | None = None) -> DeviceCode:
    """Kick off the device flow; returns the user code + URL to show the user."""
    poster = poster or _default_poster
    d = poster(DEVICE_CODE_URL, {"client_id": CLIENT_ID, "scope": scope})
    verify = str(d.get("verification_uri") or "")
    return DeviceCode(
        device_code=str(d["device_code"]),
        user_code=str(d["user_code"]),
        verification_uri=verify,
        verification_uri_complete=str(d.get("verification_uri_complete") or verify),
        interval=max(1, int(d.get("interval") or 5)),
        expires_in=int(d.get("expires_in") or 900),
    )


def exchange_device_code(device_code: str, *, poster: Poster | None = None) -> Exchange:
    """One poll of the token endpoint. Maps GitHub's reply to a status verb."""
    poster = poster or _default_poster
    d = poster(
        ACCESS_TOKEN_URL,
        {"client_id": CLIENT_ID, "device_code": device_code, "grant_type": DEVICE_GRANT},
    )
    access = d.get("access_token")
    if access:
        return Exchange(status="authorized", access_token=str(access), scope=str(d.get("scope") or ""))
    error = str(d.get("error") or "")
    return Exchange(status=_ERROR_STATUS.get(error, "error"), error=error)


def store_github_token(access_token: str) -> str:
    """Persist the access token as the shared ``GITHUB_OAUTH`` secret; return its ref.

    The raw token is stored as the value so ``resolve_github_token`` gets the token
    string directly (login + scopes are read live from GitHub, never cached).
    """
    from durin.security.secrets import store_secret

    return store_secret(
        SHARED_SECRET_NAME,
        access_token,
        service="github",
        scope=["github"],
        description="GitHub OAuth token (device flow)",
    )


def forget_github_token() -> bool:
    """Remove the shared GitHub secret. Returns True if one was present."""
    from durin.security.secrets import SecretStore, get_secret_store

    store = SecretStore().load()
    removed = store.remove(SHARED_SECRET_NAME)
    if removed:
        store.save()
        get_secret_store(reload=True)
    return removed


# --- device-flow orchestration for the web UI --------------------------------
# The raw device_code is the poll secret; keep it server-side and hand the
# browser only an opaque flow_id (mirrors how the Codex flow hides its handle).

# flow_id -> (device_code, deadline_epoch)
_PENDING: dict[str, tuple[str, float]] = {}


@dataclass
class Challenge:
    flow_id: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    expires_in: int


def _prune(now_epoch: float) -> None:
    for fid in [f for f, (_dc, dl) in _PENDING.items() if dl < now_epoch]:
        _PENDING.pop(fid, None)


def request_device_code(
    *, scope: str = DEFAULT_SCOPE, poster: Poster | None = None,
    now: Callable[[], float] | None = None,
) -> Challenge:
    """Start the flow and stash the device_code under an opaque flow_id."""
    clock = now or time.time
    dc = start_device_flow(scope=scope, poster=poster)
    flow_id = token_urlsafe(16)
    _prune(clock())
    _PENDING[flow_id] = (dc.device_code, clock() + dc.expires_in)
    return Challenge(
        flow_id=flow_id,
        user_code=dc.user_code,
        verification_uri=dc.verification_uri,
        verification_uri_complete=dc.verification_uri_complete,
        interval=dc.interval,
        expires_in=dc.expires_in,
    )


def poll_flow(
    flow_id: str, *, poster: Poster | None = None, now: Callable[[], float] | None = None
) -> Exchange:
    """One poll of a stashed flow. On authorization, stores the shared secret."""
    clock = now or time.time
    entry = _PENDING.get(flow_id)
    if not entry:
        return Exchange(status="expired", error="unknown or expired flow")
    device_code, deadline = entry
    if clock() > deadline:
        _PENDING.pop(flow_id, None)
        return Exchange(status="expired", error="device code expired")
    res = exchange_device_code(device_code, poster=poster)
    if res.status == "authorized":
        store_github_token(res.access_token)
        _PENDING.pop(flow_id, None)
    elif res.status in ("expired", "denied"):
        _PENDING.pop(flow_id, None)
    return res


# --- live status / test ------------------------------------------------------

# (url, token) -> (status_code, json_body, headers)
GetJson = Callable[[str, str], "tuple[int, dict, dict]"]


@dataclass
class Status:
    connected: bool  # a token is configured (gh / env / shared secret)
    reachable: bool  # GitHub answered 200 to that token
    login: str = ""
    scopes: str = ""
    rate_remaining: int | None = None
    rate_limit: int | None = None


def _int_or_none(v: object) -> int | None:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _default_get(url: str, token: str) -> tuple[int, dict, dict]:
    import httpx

    with httpx.Client(timeout=httpx.Timeout(15.0, connect=15.0), trust_env=True) as client:
        resp = client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": _USER_AGENT,
            },
        )
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001 - non-JSON error body -> empty
            body = {}
        return resp.status_code, body, dict(resp.headers)


def github_status(
    *, resolver: Callable[[], str] | None = None, get: GetJson | None = None
) -> Status:
    """Is a token configured, does GitHub accept it, and who + rate budget."""
    from durin.security.github_auth import resolve_github_token

    resolver = resolver or resolve_github_token
    token = resolver()
    if not token:
        return Status(connected=False, reachable=False)
    status, body, headers = (get or _default_get)(USER_URL, token)
    if status != 200:
        return Status(connected=True, reachable=False)

    def hget(key: str) -> object:
        return headers.get(key) or headers.get(key.lower())

    return Status(
        connected=True,
        reachable=True,
        login=str(body.get("login") or ""),
        scopes=str(hget("X-OAuth-Scopes") or ""),
        rate_remaining=_int_or_none(hget("X-RateLimit-Remaining")),
        rate_limit=_int_or_none(hget("X-RateLimit-Limit")),
    )
