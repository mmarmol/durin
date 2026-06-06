# Codex / ChatGPT OAuth Provider — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user authorize durin with their ChatGPT account (device-code or loopback OAuth) and use their plan's Codex usage, wired into the terminal onboarding and the webui, with an updated model list.

**Architecture:** A new `codex_device_auth` module ports hermes' device-code flow and persists tokens through `oauth-cli-kit`'s `FileTokenStorage` (`codex.json`) — the same file the existing provider reads via `get_token()`. The provider, refresh, and file-lock paths are reused unchanged except for the `originator` header, `account_id` re-extraction, and default model. A new `codex_models` module does live discovery with a static fallback. CLI auto-selects loopback (local) vs device-code (remote/headless) with a manual override; the webui always uses device-code via four new HTTP routes.

**Tech Stack:** Python 3.12, httpx, typer, pytest, oauth-cli-kit; webui is React + TypeScript (shadcn/ui), vanilla `fetch`.

**Spec:** `docs/superpowers/specs/2026-06-06-codex-oauth-provider-design.md`

---

## File Structure

New:
- `durin/providers/codex_device_auth.py` — device-code flow, JWT helpers, existing-session detection, disconnect, strict storage.
- `durin/providers/codex_models.py` — live model discovery + static fallback.
- `tests/providers/test_codex_device_auth.py`
- `tests/providers/test_codex_models.py`
- `webui/src/components/settings/CodexOAuthCard.tsx` — connect/status/disconnect UI.

Modified:
- `durin/providers/openai_codex_provider.py` — `originator=codex_cli_rs`, account_id from JWT, strict read, default `gpt-5.5`.
- `durin/utils/oauth.py` — `should_use_device_code()`.
- `durin/cli/commands.py` — device-code path + `--device/--loopback` flag + reuse prompt + disconnect already present.
- `durin/cli/onboard_wizard.py` — add `openai_codex` to choices + models + login step.
- `durin/channels/websocket.py` — 4 routes: status/start/poll/disconnect.
- `webui/src/lib/api.ts` — client functions for the 4 routes.
- `webui/src/components/settings/SettingsView.tsx` — render `CodexOAuthCard`.

---

## Task 1: JWT helpers + strict storage (codex_device_auth foundation)

**Files:**
- Create: `durin/providers/codex_device_auth.py`
- Test: `tests/providers/test_codex_device_auth.py`

- [ ] **Step 1: Write failing tests for JWT helpers**

```python
# tests/providers/test_codex_device_auth.py
import base64
import json
import time

import pytest

from durin.providers import codex_device_auth as cda


def _make_jwt(claims: dict) -> str:
    def seg(d: dict) -> str:
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


def test_account_id_from_jwt_reads_nested_claim():
    tok = _make_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct_123"}})
    assert cda.account_id_from_jwt(tok) == "acct_123"


def test_account_id_from_jwt_tolerates_garbage():
    assert cda.account_id_from_jwt("not-a-jwt") is None
    assert cda.account_id_from_jwt("") is None


def test_expiry_ms_from_jwt_uses_exp_claim():
    exp = int(time.time()) + 3600
    tok = _make_jwt({"exp": exp})
    assert cda.expiry_ms_from_jwt(tok) == exp * 1000


def test_expiry_ms_from_jwt_falls_back_when_missing(monkeypatch):
    monkeypatch.setattr(cda.time, "time", lambda: 1000.0)
    tok = _make_jwt({"no": "exp"})
    assert cda.expiry_ms_from_jwt(tok) == (1000 + 3600) * 1000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/providers/test_codex_device_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'durin.providers.codex_device_auth'`

- [ ] **Step 3: Create the module with JWT helpers + constants + strict storage**

```python
# durin/providers/codex_device_auth.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/providers/test_codex_device_auth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add durin/providers/codex_device_auth.py tests/providers/test_codex_device_auth.py
git commit -m "feat(codex): JWT + storage helpers for device-code OAuth"
```

---

## Task 2: Device-code flow (request / poll / exchange / persist)

**Files:**
- Modify: `durin/providers/codex_device_auth.py`
- Test: `tests/providers/test_codex_device_auth.py`

- [ ] **Step 1: Write failing tests using a MockTransport**

```python
# append to tests/providers/test_codex_device_auth.py
import httpx

from oauth_cli_kit.models import OAuthToken


def _mock_client(handler):
    def factory():
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    return factory


def test_request_device_code_parses_fields(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/deviceauth/usercode")
        assert json.loads(request.content)["client_id"] == cda.CLIENT_ID
        return httpx.Response(200, json={
            "user_code": "WXYZ-1234", "device_auth_id": "dev_1", "interval": 5,
        })
    monkeypatch.setattr(cda, "_client", _mock_client(handler))
    ch = cda.request_device_code()
    assert ch.user_code == "WXYZ-1234"
    assert ch.device_auth_id == "dev_1"
    assert ch.verification_uri == cda.VERIFICATION_URI


def test_poll_once_pending_on_403(monkeypatch):
    monkeypatch.setattr(cda, "_client", _mock_client(
        lambda req: httpx.Response(403, json={})))
    res = cda.poll_once("dev_1", "WXYZ-1234")
    assert res.status == "pending" and res.token is None


def test_poll_once_ok_exchanges_and_persists(monkeypatch, tmp_path):
    exp = int(time.time()) + 3600
    access = _make_jwt({
        "exp": exp,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct_9"},
    })

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deviceauth/token"):
            return httpx.Response(200, json={
                "authorization_code": "AC", "code_verifier": "CV"})
        if request.url.path.endswith("/oauth/token"):
            assert b"grant_type=authorization_code" in request.content
            return httpx.Response(200, json={
                "access_token": access, "refresh_token": "RT"})
        raise AssertionError(request.url.path)

    saved: list[OAuthToken] = []

    class _Storage:
        def save(self, tok): saved.append(tok)
        def load(self): return saved[-1] if saved else None
        def get_token_path(self): return tmp_path / "codex.json"

    monkeypatch.setattr(cda, "_client", _mock_client(handler))
    monkeypatch.setattr(cda, "_strict_storage", lambda: _Storage())
    res = cda.poll_once("dev_1", "WXYZ-1234")
    assert res.status == "ok"
    assert res.token.account_id == "acct_9"
    assert res.token.access == access
    assert res.token.refresh == "RT"
    assert res.token.expires == exp * 1000
    assert saved and saved[-1].account_id == "acct_9"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/providers/test_codex_device_auth.py -k "device_code or poll_once" -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'request_device_code'`

- [ ] **Step 3: Implement the device-code flow**

```python
# append to durin/providers/codex_device_auth.py

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/providers/test_codex_device_auth.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add durin/providers/codex_device_auth.py tests/providers/test_codex_device_auth.py
git commit -m "feat(codex): device-code request/poll/exchange with token persistence"
```

---

## Task 3: Blocking login, existing-session detection, disconnect

**Files:**
- Modify: `durin/providers/codex_device_auth.py`
- Test: `tests/providers/test_codex_device_auth.py`

- [ ] **Step 1: Write failing tests**

```python
# append to tests/providers/test_codex_device_auth.py

def test_existing_codex_session_reads_durin_token(monkeypatch):
    access = _make_jwt({
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct_9", "chatgpt_plan_type": "pro"},
        "https://api.openai.com/profile.email": "u@x.com",
    })

    class _Storage:
        def load(self):
            from oauth_cli_kit.models import OAuthToken
            return OAuthToken(access=access, refresh="RT", expires=10**13, account_id="acct_9")

    monkeypatch.setattr(cda, "_strict_storage", lambda: _Storage())
    monkeypatch.setattr(cda, "_read_codex_cli_session", lambda: None)
    info = cda.existing_codex_session()
    assert info is not None
    assert info.email == "u@x.com"
    assert info.plan == "pro"
    assert info.source == "durin"


def test_existing_codex_session_none_when_absent(monkeypatch):
    class _Storage:
        def load(self): return None
    monkeypatch.setattr(cda, "_strict_storage", lambda: _Storage())
    monkeypatch.setattr(cda, "_read_codex_cli_session", lambda: None)
    assert cda.existing_codex_session() is None


def test_disconnect_removes_token_and_lock(monkeypatch, tmp_path):
    token_path = tmp_path / "codex.json"
    token_path.write_text("{}")
    lock_path = tmp_path / "codex.json.lock"
    lock_path.write_text("")

    class _Storage:
        def get_token_path(self): return token_path

    monkeypatch.setattr(cda, "_strict_storage", lambda: _Storage())
    assert cda.disconnect() is True
    assert not token_path.exists()
    assert not lock_path.exists()
    assert cda.disconnect() is False  # nothing left to remove
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/providers/test_codex_device_auth.py -k "session or disconnect" -v`
Expected: FAIL with `AttributeError: ... 'existing_codex_session'`

- [ ] **Step 3: Implement session detection, blocking login, disconnect**

```python
# append to durin/providers/codex_device_auth.py

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/providers/test_codex_device_auth.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add durin/providers/codex_device_auth.py tests/providers/test_codex_device_auth.py
git commit -m "feat(codex): blocking login, session detection, disconnect"
```

---

## Task 4: Codex model discovery + static fallback

**Files:**
- Create: `durin/providers/codex_models.py`
- Test: `tests/providers/test_codex_models.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/providers/test_codex_models.py
import httpx

from durin.providers import codex_models as cm


def _mock_client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


def test_fallback_when_no_token():
    assert cm.list_codex_models(access_token=None) == list(cm.STATIC_FALLBACK)


def test_discovery_sorts_by_priority_and_drops_hidden(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/codex/models")
        assert request.headers["Authorization"] == "Bearer T"
        assert request.headers["originator"] == "codex_cli_rs"
        return httpx.Response(200, json={"models": [
            {"slug": "gpt-5.5", "priority": 1},
            {"slug": "secret", "priority": 0, "visibility": "hidden"},
            {"slug": "gpt-5.4", "priority": 5},
        ]})
    monkeypatch.setattr(cm, "_client", _mock_client(handler))
    assert cm.list_codex_models(access_token="T") == ["gpt-5.5", "gpt-5.4"]


def test_discovery_failure_falls_back(monkeypatch):
    monkeypatch.setattr(cm, "_client", _mock_client(
        lambda req: httpx.Response(500, json={})))
    assert cm.list_codex_models(access_token="T") == list(cm.STATIC_FALLBACK)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/providers/test_codex_models.py -v`
Expected: FAIL with `ModuleNotFoundError: ... codex_models`

- [ ] **Step 3: Implement codex_models**

```python
# durin/providers/codex_models.py
"""Codex model list: live discovery from the OAuth backend, static fallback.

Live discovery is the source of truth. The static fallback mirrors hermes and
is only used when there is no token or the endpoint is unreachable.
"""

from __future__ import annotations

import httpx
from loguru import logger

MODELS_URL = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
ORIGINATOR = "codex_cli_rs"

STATIC_FALLBACK: tuple[str, ...] = (
    "gpt-5.5",
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
)

DEFAULT_MODEL = "gpt-5.5"


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(10.0))


def _discover(access_token: str) -> list[str]:
    with _client() as client:
        resp = client.get(
            MODELS_URL,
            headers={"Authorization": f"Bearer {access_token}", "originator": ORIGINATOR},
        )
    if resp.status_code != 200:
        return []
    data = resp.json()
    entries = data.get("models", []) if isinstance(data, dict) else []
    ranked: list[tuple[int, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = str(item.get("visibility", "")).strip().lower()
        if visibility in {"hide", "hidden"}:
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        ranked.append((rank, slug.strip()))
    ranked.sort(key=lambda x: (x[0], x[1]))
    return [slug for _, slug in ranked]


def list_codex_models(access_token: str | None) -> list[str]:
    """Discovered models when a token is available; static fallback otherwise."""
    if access_token:
        try:
            models = _discover(access_token)
            if models:
                return models
        except Exception as exc:  # noqa: BLE001
            logger.debug("codex model discovery failed: {}", exc)
    return list(STATIC_FALLBACK)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/providers/test_codex_models.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add durin/providers/codex_models.py tests/providers/test_codex_models.py
git commit -m "feat(codex): model discovery with static fallback"
```

---

## Task 5: Provider edits — originator, account_id from JWT, strict read, default model

**Files:**
- Modify: `durin/providers/openai_codex_provider.py`
- Test: `tests/providers/test_openai_codex_provider.py` (create)

- [ ] **Step 1: Write failing tests**

```python
# tests/providers/test_openai_codex_provider.py
import base64
import json

from durin.providers import openai_codex_provider as ocp


def _make_jwt(account_id: str) -> str:
    def seg(d): return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    return f"{seg({'alg':'none'})}.{seg(claims)}.sig"


def test_headers_use_codex_cli_originator():
    h = ocp._build_headers("acct_1", "tok")
    assert h["originator"] == "codex_cli_rs"
    assert h["User-Agent"].startswith("codex_cli_rs")


def test_headers_recover_account_id_from_jwt_when_missing():
    access = _make_jwt("acct_77")
    h = ocp._build_headers(None, access)
    assert h["chatgpt-account-id"] == "acct_77"


def test_default_model_is_gpt55():
    assert ocp.OpenAICodexProvider().get_default_model() == "openai-codex/gpt-5.5"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/providers/test_openai_codex_provider.py -v`
Expected: FAIL (`originator` == "durin"; default model mismatch; account_id None)

- [ ] **Step 3: Apply the edits**

In `durin/providers/openai_codex_provider.py`:

Replace the originator constant and default model:

```python
DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "codex_cli_rs"
DEFAULT_USER_AGENT = "codex_cli_rs/0.0.0 (durin)"
```

```python
    def __init__(self, default_model: str = "openai-codex/gpt-5.5"):
```

Make the strict read in `_call_codex` (replace the `get_token` import + call at lines 47-50):

```python
        from oauth_cli_kit import get_token as get_codex_token
        from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER

        from durin.providers.codex_device_auth import _strict_storage

        token = await asyncio.to_thread(
            get_codex_token, OPENAI_CODEX_PROVIDER, _strict_storage()
        )
        headers = _build_headers(token.account_id, token.access)
```

Update `_build_headers` to recover account_id from the JWT and pin the User-Agent:

```python
def _build_headers(account_id: str | None, token: str) -> dict[str, str]:
    from durin.providers.codex_device_auth import account_id_from_jwt

    resolved = account_id or account_id_from_jwt(token) or ""
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": resolved,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": DEFAULT_USER_AGENT,
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/providers/test_openai_codex_provider.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add durin/providers/openai_codex_provider.py tests/providers/test_openai_codex_provider.py
git commit -m "feat(codex): codex_cli_rs originator, account_id from JWT, strict read, gpt-5.5 default"
```

---

## Task 6: Login strategy selector (`should_use_device_code`)

**Files:**
- Modify: `durin/utils/oauth.py`
- Test: `tests/utils/test_oauth_strategy.py` (create)

- [ ] **Step 1: Write failing tests**

```python
# tests/utils/test_oauth_strategy.py
from durin.utils import oauth


def test_device_code_when_ssh(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert oauth.should_use_device_code() is True


def test_loopback_when_local_gui(monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setattr(oauth.sys, "platform", "darwin")
    assert oauth.should_use_device_code() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/utils/test_oauth_strategy.py -v`
Expected: FAIL with `AttributeError: module 'durin.utils.oauth' has no attribute 'should_use_device_code'`

- [ ] **Step 3: Implement the selector**

Add to `durin/utils/oauth.py` (add `import os`, `import sys` at top, and extend `__all__`):

```python
import os
import sys

__all__ = [
    "token_storage_paths",
    "any_token_present",
    "should_use_device_code",
]


def should_use_device_code() -> bool:
    """True when loopback PKCE is unlikely to work (remote/headless shell).

    Loopback needs the user's browser and the local callback server on the same
    machine. Over SSH or without a GUI that does not hold, so device-code is the
    safe default.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    if sys.platform in ("darwin", "win32"):
        return False  # GUI desktop assumed
    return not os.environ.get("DISPLAY")  # Linux: GUI only if a display is set
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/utils/test_oauth_strategy.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add durin/utils/oauth.py tests/utils/test_oauth_strategy.py
git commit -m "feat(oauth): should_use_device_code() loopback-vs-device selector"
```

---

## Task 7: CLI login — device-code path, override flag, reuse prompt

**Files:**
- Modify: `durin/cli/commands.py` (oauth_app group, ~2479-2531)

- [ ] **Step 1: Write a failing test for the strategy wiring**

```python
# tests/cli/test_oauth_login_strategy.py
from durin.cli import commands


def test_login_codex_device_calls_blocking(monkeypatch):
    called = {}
    monkeypatch.setattr(commands, "should_use_device_code", lambda: True)
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.existing_codex_session", lambda: None)

    class _Tok:
        access = "A"
        account_id = "acct_1"

    monkeypatch.setattr(
        "durin.providers.codex_device_auth.login_blocking",
        lambda print_fn, **k: _Tok())
    commands._codex_login_flow(force=None)
    assert True  # no exception == device path ran
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cli/test_oauth_login_strategy.py -v`
Expected: FAIL with `AttributeError: ... '_codex_login_flow'`

- [ ] **Step 3: Add the strategy-aware codex login + flag**

In `durin/cli/commands.py`, add the import near the other top-level imports:

```python
from durin.utils.oauth import should_use_device_code
```

Add a shared flow function and rewrite the `openai_codex` login handler (replace lines 2511-2531):

```python
def _codex_login_flow(force: str | None) -> None:
    """force: 'device' | 'loopback' | None (auto-detect)."""
    from contextlib import suppress

    from durin.providers import codex_device_auth as cda

    existing = cda.existing_codex_session()
    if existing is not None:
        who = existing.email or existing.plan or "cuenta existente"
        src = "Codex CLI" if existing.source == "codex-cli" else "durin"
        reuse = typer.confirm(
            f"Encontré una sesión de Codex ({who}, vía {src}). ¿Usarla?",
            default=True,
        )
        if reuse and existing.source == "durin":
            console.print(f"[green]✓ Usando la sesión existente[/green] [dim]{who}[/dim]")
            return
        # codex-cli source or decline: fall through to a fresh connect.

    use_device = force == "device" or (force != "loopback" and should_use_device_code())
    if use_device:
        from durin.providers.codex_device_auth import login_blocking

        token = login_blocking(print_fn=lambda s: console.print(s))
    else:
        from oauth_cli_kit import login_oauth_interactive

        token = login_oauth_interactive(
            print_fn=lambda s: console.print(s),
            prompt_fn=lambda s: typer.prompt(s),
            originator="codex_cli_rs",
        )
    if not (token and token.access):
        console.print("[red]✗ Authentication failed[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]"
    )


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        _codex_login_flow(force=None)
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1) from None
```

Add the `--device/--loopback` override on the `login` command (modify lines 2479-2492). Replace the body with:

```python
@oauth_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex')"),
    device: bool = typer.Option(False, "--device", help="Force device-code flow"),
    loopback: bool = typer.Option(False, "--loopback", help="Force loopback PKCE flow"),
):
    """Authenticate with an OAuth provider."""
    spec = _resolve_oauth_provider(provider)
    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    if spec.name == "openai_codex" and (device or loopback):
        force = "device" if device else "loopback"
        try:
            _codex_login_flow(force=force)
        except ImportError:
            console.print("[red]oauth_cli_kit not installed.[/red]")
            raise typer.Exit(1) from None
        return
    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)
    handler()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cli/test_oauth_login_strategy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/cli/commands.py tests/cli/test_oauth_login_strategy.py
git commit -m "feat(cli): codex device-code/loopback login with reuse prompt and override flag"
```

---

## Task 8: Onboard wizard — add Codex provider, models, login step

**Files:**
- Modify: `durin/cli/onboard_wizard.py`

- [ ] **Step 1: Write a failing test**

```python
# tests/cli/test_onboard_codex.py
from durin.cli import onboard_wizard as ow


def test_codex_in_provider_choices():
    names = [name for _, name, _ in ow.PROVIDER_CHOICES]
    assert "openai_codex" in names


def test_codex_default_models_present():
    assert "openai_codex" in ow.DEFAULT_MODELS
    assert "gpt-5.5" in ow.DEFAULT_MODELS["openai_codex"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cli/test_onboard_codex.py -v`
Expected: FAIL (`openai_codex` not in choices)

- [ ] **Step 3: Add the entries**

In `durin/cli/onboard_wizard.py`, add to `PROVIDER_CHOICES` (after the OpenAI line):

```python
    ("OpenAI Codex (ChatGPT Plus/Pro, OAuth)", "openai_codex", "gpt-5.5"),
```

Add to `DEFAULT_MODELS`:

```python
    "openai_codex": ("gpt-5.5", "gpt-5.4-mini", "gpt-5.4", "gpt-5.3-codex", "gpt-5.3-codex-spark"),
```

Find where a non-OAuth provider prompts for the API key (the key-entry branch keyed off the chosen provider) and special-case `openai_codex` to run the login flow instead. Add this branch immediately before the API-key prompt:

```python
        if provider_name == "openai_codex":
            from durin.cli.commands import _codex_login_flow

            _codex_login_flow(force=None)
            return  # token persisted; no api_key/api_base to store
```

(If the wizard's structure uses a helper like `_configure_provider`, place the branch at the top of that helper. The intent: when `openai_codex` is selected, call `_codex_login_flow` and skip the key/base prompts.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/cli/test_onboard_codex.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Manually verify the wizard flow**

Run: `python -m durin onboard` (from the worktree), select "OpenAI Codex", confirm the device-code/loopback prompt appears.
Expected: the login flow runs; on success the wizard records `provider=openai_codex`, `model=gpt-5.5`.

- [ ] **Step 6: Commit**

```bash
git add durin/cli/onboard_wizard.py tests/cli/test_onboard_codex.py
git commit -m "feat(onboard): offer OpenAI Codex OAuth provider with login step"
```

---

## Task 9: WebUI backend routes — status / start / poll / disconnect

**Files:**
- Modify: `durin/channels/websocket.py`
- Test: `tests/channels/test_codex_oauth_routes.py` (create)

- [ ] **Step 1: Write failing tests for the handlers**

```python
# tests/channels/test_codex_oauth_routes.py
import json
import types

from durin.channels import websocket as ws


def _handler_instance():
    return ws.WebSocketChannel.__new__(ws.WebSocketChannel)


def _ok_token(monkeypatch, inst):
    monkeypatch.setattr(inst, "_check_api_token", lambda request: True, raising=False)


def _req(path):
    return types.SimpleNamespace(path=path, headers={})


def test_status_reports_connected(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    info = ws.CodexSessionInfo(email="u@x.com", plan="pro", source="durin")
    monkeypatch.setattr(ws, "existing_codex_session", lambda: info)
    resp = inst._handle_codex_oauth_status(_req("/api/oauth/codex/status?token=t"))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["connected"] is True and body["email"] == "u@x.com"


def test_start_returns_challenge(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    ch = ws.DeviceCodeChallenge(
        user_code="WXYZ-1", verification_uri="https://auth.openai.com/codex/device",
        device_auth_id="dev_1", interval=5, expires_in=900)
    monkeypatch.setattr(ws, "request_device_code", lambda: ch)
    resp = inst._handle_codex_oauth_start(_req("/api/oauth/codex/start?token=t"))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["user_code"] == "WXYZ-1" and body["device_auth_id"] == "dev_1"


def test_disconnect(monkeypatch):
    inst = _handler_instance()
    _ok_token(monkeypatch, inst)
    monkeypatch.setattr(ws, "codex_disconnect", lambda: True)
    monkeypatch.setattr(ws, "existing_codex_session", lambda: None)
    resp = inst._handle_codex_oauth_disconnect(_req("/api/oauth/codex/disconnect?token=t"))
    body = json.loads(resp.body.decode("utf-8"))
    assert body["connected"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/channels/test_codex_oauth_routes.py -v`
Expected: FAIL with `AttributeError` (handlers / imported names missing)

- [ ] **Step 3: Add imports near the top of `durin/channels/websocket.py`**

```python
from durin.providers.codex_device_auth import (
    CodexSessionInfo,
    DeviceCodeChallenge,
    disconnect as codex_disconnect,
    existing_codex_session,
    poll_once as codex_poll_once,
    request_device_code,
)
```

- [ ] **Step 4: Register the four routes in `_dispatch_http`**

Add these exact-match blocks alongside the other `/api/...` routes:

```python
        if got == "/api/oauth/codex/status":
            return self._handle_codex_oauth_status(request)
        if got == "/api/oauth/codex/start":
            return self._handle_codex_oauth_start(request)
        if got == "/api/oauth/codex/poll":
            return self._handle_codex_oauth_poll(request)
        if got == "/api/oauth/codex/disconnect":
            return self._handle_codex_oauth_disconnect(request)
```

- [ ] **Step 5: Implement the handlers (add as methods on the channel class)**

```python
    def _codex_status_payload(self) -> dict[str, Any]:
        info = existing_codex_session()
        if info is None:
            return {"connected": False}
        return {
            "connected": True,
            "email": info.email,
            "plan": info.plan,
            "source": info.source,
        }

    def _handle_codex_oauth_status(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(self._codex_status_payload())

    def _handle_codex_oauth_start(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            ch = request_device_code()
        except Exception as exc:  # noqa: BLE001
            return _http_error(502, f"device code request failed: {exc}")
        return _http_json_response({
            "user_code": ch.user_code,
            "verification_uri": ch.verification_uri,
            "device_auth_id": ch.device_auth_id,
            "interval": ch.interval,
            "expires_in": ch.expires_in,
        })

    def _handle_codex_oauth_poll(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        device_auth_id = (_query_first(query, "device_auth_id") or "").strip()
        user_code = (_query_first(query, "user_code") or "").strip()
        if not device_auth_id or not user_code:
            return _http_error(400, "device_auth_id and user_code are required")
        res = codex_poll_once(device_auth_id, user_code)
        payload: dict[str, Any] = {"status": res.status}
        if res.status == "ok":
            payload.update(self._codex_status_payload())
        if res.status == "error":
            payload["error"] = res.error
        return _http_json_response(payload)

    def _handle_codex_oauth_disconnect(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        codex_disconnect()
        return _http_json_response(self._codex_status_payload())
```

> Note: the poll handler also returns `user_code` to the client; the client must
> pass both `device_auth_id` and `user_code` (returned from `/start`) to `/poll`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/channels/test_codex_oauth_routes.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: Commit**

```bash
git add durin/channels/websocket.py tests/channels/test_codex_oauth_routes.py
git commit -m "feat(webui-api): codex OAuth status/start/poll/disconnect routes"
```

---

## Task 10: WebUI API client functions

**Files:**
- Modify: `webui/src/lib/api.ts`

- [ ] **Step 1: Add types + client functions (mirror existing `request<T>` pattern)**

```typescript
// webui/src/lib/api.ts

export interface CodexStatus {
  connected: boolean;
  email?: string | null;
  plan?: string | null;
  source?: "durin" | "codex-cli";
}

export interface CodexDeviceChallenge {
  user_code: string;
  verification_uri: string;
  device_auth_id: string;
  interval: number;
  expires_in: number;
}

export interface CodexPollResult extends CodexStatus {
  status: "pending" | "ok" | "error";
  error?: string;
}

export async function fetchCodexStatus(
  token: string,
  base: string = "",
): Promise<CodexStatus> {
  return request<CodexStatus>(`${base}/api/oauth/codex/status`, token);
}

export async function startCodexDeviceAuth(
  token: string,
  base: string = "",
): Promise<CodexDeviceChallenge> {
  return request<CodexDeviceChallenge>(`${base}/api/oauth/codex/start`, token);
}

export async function pollCodexDeviceAuth(
  token: string,
  deviceAuthId: string,
  userCode: string,
  base: string = "",
): Promise<CodexPollResult> {
  const q = new URLSearchParams();
  q.set("device_auth_id", deviceAuthId);
  q.set("user_code", userCode);
  return request<CodexPollResult>(`${base}/api/oauth/codex/poll?${q}`, token);
}

export async function disconnectCodex(
  token: string,
  base: string = "",
): Promise<CodexStatus> {
  return request<CodexStatus>(`${base}/api/oauth/codex/disconnect`, token, {
    method: "POST",
  });
}
```

- [ ] **Step 2: Type-check the webui**

Run: `cd webui && npm run build`
Expected: build succeeds (no TS errors).

- [ ] **Step 3: Commit**

```bash
git add webui/src/lib/api.ts
git commit -m "feat(webui): api client for codex OAuth routes"
```

---

## Task 11: WebUI Codex OAuth card (connect / status / disconnect)

**Files:**
- Create: `webui/src/components/settings/CodexOAuthCard.tsx`
- Modify: `webui/src/components/settings/SettingsView.tsx`

- [ ] **Step 1: Create the card component**

```tsx
// webui/src/components/settings/CodexOAuthCard.tsx
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  CodexChallengeState,
  disconnectCodex,
  fetchCodexStatus,
  pollCodexDeviceAuth,
  startCodexDeviceAuth,
  type CodexStatus,
} from "@/lib/api";

type Props = { token: string; base?: string };

export function CodexOAuthCard({ token, base = "" }: Props) {
  const [status, setStatus] = useState<CodexStatus | null>(null);
  const [challenge, setChallenge] = useState<{
    user_code: string;
    verification_uri: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const pollTimer = useRef<number | null>(null);

  useEffect(() => {
    fetchCodexStatus(token, base).then(setStatus).catch(() => setStatus(null));
    return () => {
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
    };
  }, [token, base]);

  const connect = async () => {
    setError(null);
    setBusy(true);
    try {
      const ch = await startCodexDeviceAuth(token, base);
      setChallenge({ user_code: ch.user_code, verification_uri: ch.verification_uri });
      const intervalMs = Math.max(3, ch.interval) * 1000;
      const tick = async () => {
        try {
          const res = await pollCodexDeviceAuth(token, ch.device_auth_id, ch.user_code, base);
          if (res.status === "ok") {
            setChallenge(null);
            setBusy(false);
            setStatus({ connected: true, email: res.email, plan: res.plan, source: res.source });
            return;
          }
          if (res.status === "error") {
            setError(res.error ?? "error de autorización");
            setChallenge(null);
            setBusy(false);
            return;
          }
          pollTimer.current = window.setTimeout(tick, intervalMs);
        } catch (e) {
          setError((e as Error).message);
          setBusy(false);
        }
      };
      pollTimer.current = window.setTimeout(tick, intervalMs);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const doDisconnect = async () => {
    setConfirmDisconnect(false);
    setBusy(true);
    try {
      setStatus(await disconnectCodex(token, base));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3 rounded-[10px] border border-border/45 p-4">
      <div className="flex items-center justify-between">
        <span className="text-[15px] font-semibold">OpenAI Codex (ChatGPT)</span>
        <span
          className={cn(
            "rounded-full px-2.5 py-1 text-[12px] font-medium",
            status?.connected
              ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
              : "bg-muted text-muted-foreground",
          )}
        >
          {status?.connected
            ? `Conectado${status.email ? ` · ${status.email}` : ""}`
            : "No conectado"}
        </span>
      </div>

      {challenge ? (
        <div className="space-y-2 rounded-[8px] border border-border/60 bg-muted/40 p-3 text-[13px]">
          <p>1. Abrí <a className="underline" href={challenge.verification_uri} target="_blank" rel="noreferrer">{challenge.verification_uri}</a></p>
          <p>2. Ingresá el código: <span className="font-mono font-semibold">{challenge.user_code}</span></p>
          <p className="text-muted-foreground">Esperando la autorización…</p>
        </div>
      ) : null}

      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

      <div className="flex items-center justify-end gap-2">
        {status?.connected && !confirmDisconnect ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirmDisconnect(true)}>
            Desconectar
          </Button>
        ) : null}
        {confirmDisconnect ? (
          <div className="flex items-center gap-2 rounded-[8px] border border-border/60 bg-muted/40 p-2">
            <span className="text-[12px]">¿Desconectar la cuenta?</span>
            <Button size="sm" variant="destructive" disabled={busy} onClick={() => void doDisconnect()}>
              Sí, desconectar
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirmDisconnect(false)}>
              Cancelar
            </Button>
          </div>
        ) : null}
        {!status?.connected && !challenge ? (
          <Button size="sm" disabled={busy} onClick={() => void connect()}>
            Conectar con ChatGPT
          </Button>
        ) : null}
      </div>
    </div>
  );
}
```

> If `CodexChallengeState` is unused, drop it from the import — keep only the
> symbols referenced (`disconnectCodex`, `fetchCodexStatus`, `pollCodexDeviceAuth`,
> `startCodexDeviceAuth`, `CodexStatus`).

- [ ] **Step 2: Render the card in `SettingsView.tsx`**

In the provider settings pane (the `ByokSettings` area), import and render the card above the provider rows:

```tsx
import { CodexOAuthCard } from "@/components/settings/CodexOAuthCard";
```

```tsx
        <CodexOAuthCard token={token} />
```

(Place it inside the same container that renders `renderProviderRow`, before the
configured/unconfigured lists, so OAuth sits visually with the other providers.)

- [ ] **Step 3: Build the webui**

Run: `cd webui && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
git add webui/src/components/settings/CodexOAuthCard.tsx webui/src/components/settings/SettingsView.tsx
git commit -m "feat(webui): Codex OAuth connect/status/disconnect card"
```

---

## Task 12: Wire model discovery into `/api/models` for codex

**Files:**
- Modify: `durin/channels/websocket.py` (`_handle_models_list`)
- Test: `tests/channels/test_codex_models_endpoint.py` (create)

- [ ] **Step 1: Write a failing test**

```python
# tests/channels/test_codex_models_endpoint.py
import json

from durin.channels import websocket as ws


def test_models_list_uses_codex_discovery(monkeypatch):
    inst = ws.WebSocketChannel.__new__(ws.WebSocketChannel)
    monkeypatch.setattr(inst, "_check_api_token", lambda request: True, raising=False)
    monkeypatch.setattr(ws, "list_codex_models", lambda access_token: ["gpt-5.5", "gpt-5.4"])
    import types
    req = types.SimpleNamespace(path="/api/models?provider=openai-codex&token=t", headers={})
    resp = inst._handle_models_list(req)
    body = json.loads(resp.body.decode("utf-8"))
    assert "gpt-5.5" in body["models"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/channels/test_codex_models_endpoint.py -v`
Expected: FAIL (codex models not surfaced / `list_codex_models` not imported)

- [ ] **Step 3: Branch the models handler for codex**

Add the import near the top of `durin/channels/websocket.py`:

```python
from durin.providers.codex_models import list_codex_models
```

At the start of `_handle_models_list` (after the token check and after reading the
`provider` query param), add:

```python
        if provider_name in ("openai-codex", "openai_codex"):
            access = None
            try:
                from oauth_cli_kit import get_token

                from durin.providers.codex_device_auth import _strict_storage
                from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER

                tok = get_token(OPENAI_CODEX_PROVIDER, _strict_storage())
                access = tok.access if tok else None
            except Exception:  # noqa: BLE001
                access = None
            models = list_codex_models(access)
            return _http_json_response({"suggested": models, "models": models})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/channels/test_codex_models_endpoint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/channels/websocket.py tests/channels/test_codex_models_endpoint.py
git commit -m "feat(webui-api): serve codex models via live discovery + fallback"
```

---

## Task 13: Full-suite verification

- [ ] **Step 1: Run the provider + cli + channel tests**

Run: `pytest tests/providers tests/utils/test_oauth_strategy.py tests/cli/test_oauth_login_strategy.py tests/cli/test_onboard_codex.py tests/channels/test_codex_oauth_routes.py tests/channels/test_codex_models_endpoint.py -v`
Expected: all PASS.

- [ ] **Step 2: Lint the changed Python files**

Run: `ruff check durin/providers/codex_device_auth.py durin/providers/codex_models.py durin/providers/openai_codex_provider.py durin/utils/oauth.py durin/cli/commands.py durin/cli/onboard_wizard.py durin/channels/websocket.py`
Expected: no errors.

- [ ] **Step 3: Build the webui**

Run: `cd webui && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Live verify (per spec)**

- Local CLI: `python -m durin oauth login openai-codex --loopback` (browser flow).
- Remote/device: `python -m durin oauth login openai-codex --device` (shows code + URL).
- WebUI: `python -m durin gateway --foreground` (from the worktree, after `npm run build`),
  open the dashboard → Settings → Providers → "Conectar con ChatGPT" → confirm device-code,
  then a chat turn on `openai-codex/gpt-5.5` to confirm a real Codex response.

- [ ] **Step 5: Final commit (docs)**

Update `docs/architecture` (provider/auth section) and `docs/bitacora.md` per repo convention, then:

```bash
git add docs/
git commit -m "docs: record Codex OAuth provider (device-code + webui)"
```

---

## Self-Review Notes

- **Spec coverage:** Objective 1 (onboard) → Task 8; Objective 2 (webui) → Tasks 9-11; Objective 3 (models) → Tasks 4, 12. Device-code → Tasks 1-3; loopback reuse + auto-detect + override → Tasks 6-7; originator `codex_cli_rs` → Tasks 2,4,5; existing-session confirm + no silent import → Tasks 1,3,7 (`import_codex_cli=False`); connect/status/disconnect → Tasks 9-11; refresh `account_id` risk → mitigated in Task 5 (`_build_headers` re-extracts from JWT).
- **Type consistency:** `OAuthToken` fields (`access`, `refresh`, `expires`, `account_id`), `DeviceCodeChallenge`, `PollResult.status`, `CodexSessionInfo.source` reused identically across CLI, webui routes, and webui client types.
- **No placeholders:** every code step is concrete. The two soft spots (exact insertion point of the onboard branch in Task 8; exact location of the provider query-param read in Task 12) are described precisely against quoted code; the executor confirms the surrounding lines when applying.
