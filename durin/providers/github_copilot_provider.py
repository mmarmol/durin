"""GitHub Copilot OAuth-backed provider."""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Awaitable, Callable
from contextlib import suppress

import httpx

from durin.providers.openai_compat_provider import OpenAICompatProvider

DEFAULT_GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
DEFAULT_GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEFAULT_GITHUB_USER_URL = "https://api.github.com/user"
DEFAULT_COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
DEFAULT_COPILOT_BASE_URL = "https://api.githubcopilot.com"
GITHUB_COPILOT_CLIENT_ID = "Iv1.b507a08c87ecfe98"
GITHUB_COPILOT_SCOPE = "read:user"
TOKEN_FILENAME = "github-copilot.json"
TOKEN_APP_NAME = "durin"
USER_AGENT = "durin/0.1"
EDITOR_VERSION = "vscode/1.99.0"
EDITOR_PLUGIN_VERSION = "copilot-chat/0.26.0"
_EXPIRY_SKEW_SECONDS = 60
_LONG_LIVED_TOKEN_SECONDS = 315360000


_COPILOT_SECRET_NAME = "GITHUB_COPILOT_OAUTH"


def _kit_file_storage():
    """The kit's previous on-disk store — kept only for one-time migration."""
    from oauth_cli_kit.storage import FileTokenStorage

    return FileTokenStorage(
        token_filename=TOKEN_FILENAME,
        app_name=TOKEN_APP_NAME,
        import_codex_cli=False,
    )


class _CopilotSecretsStorage:
    """``oauth-cli-kit`` token storage backed by durin's secret store.

    The GitHub OAuth token lives in ``~/.durin/secrets.json`` (mode 0600), like
    every other credential, instead of the kit's own app-data dir. On first
    ``load()`` a token left in the kit's file store is migrated into secrets.
    Mirrors ``codex_device_auth._CodexSecretsStorage``.
    """

    def load(self):
        import json

        from oauth_cli_kit.models import OAuthToken

        from durin.security.secrets import SecretNotFoundError, resolve_secret

        raw = None
        try:
            raw = resolve_secret(f"${{secret:{_COPILOT_SECRET_NAME}}}")
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

    def save(self, token) -> None:
        import json

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
            _COPILOT_SECRET_NAME,
            blob,
            service="provider:github_copilot",
            scope=["provider:github_copilot"],
            description="GitHub Copilot OAuth token",
            origin="oauth",
        )

    def get_token_path(self):
        # Data lives in secrets.json; this path is only used by the CLI logout
        # to clean up the kit's previous file store (+ its lock).
        return _kit_file_storage().get_token_path()


def get_storage():
    return _CopilotSecretsStorage()


def disconnect() -> bool:
    """Forget the Copilot token: delete the secret and any legacy kit file."""
    removed = False
    try:
        from durin.security.secrets import SecretStore, get_secret_store

        store = SecretStore().load()
        if store.remove(_COPILOT_SECRET_NAME):
            get_secret_store(reload=True)
            removed = True
    except Exception as exc:  # noqa: BLE001
        from loguru import logger

        logger.warning("could not remove github copilot secret: {}", exc)
    # Clean up the kit's previous file store (+ lock), if still around.
    try:
        legacy = _kit_file_storage().get_token_path()
        for path in (legacy, legacy.with_suffix(".lock")):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                continue
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return removed


def _copilot_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Editor-Version": EDITOR_VERSION,
        "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
    }


def _load_github_token():
    token = get_storage().load()
    if not token or not token.access:
        return None
    return token


def login_github_copilot(
    print_fn: Callable[[str], None] | None = None,
    prompt_fn: Callable[[str], str] | None = None,
):
    """Run GitHub device flow and persist the GitHub OAuth token used for Copilot."""
    del prompt_fn
    printer = print_fn or print
    timeout = httpx.Timeout(20.0, connect=20.0)

    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True) as client:
        response = client.post(
            DEFAULT_GITHUB_DEVICE_CODE_URL,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            data={"client_id": GITHUB_COPILOT_CLIENT_ID, "scope": GITHUB_COPILOT_SCOPE},
        )
        response.raise_for_status()
        payload = response.json()

        device_code = str(payload["device_code"])
        user_code = str(payload["user_code"])
        verify_url = str(payload.get("verification_uri") or payload.get("verification_uri_complete") or "")
        verify_complete = str(payload.get("verification_uri_complete") or verify_url)
        interval = max(1, int(payload.get("interval") or 5))
        expires_in = int(payload.get("expires_in") or 900)

        printer(f"Open: {verify_url}")
        printer(f"Code: {user_code}")
        if verify_complete:
            with suppress(Exception):
                webbrowser.open(verify_complete)

        deadline = time.time() + expires_in
        current_interval = interval
        access_token = None
        token_expires_in = _LONG_LIVED_TOKEN_SECONDS
        while time.time() < deadline:
            poll = client.post(
                DEFAULT_GITHUB_ACCESS_TOKEN_URL,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                data={
                    "client_id": GITHUB_COPILOT_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
            poll.raise_for_status()
            poll_payload = poll.json()

            access_token = poll_payload.get("access_token")
            if access_token:
                token_expires_in = int(poll_payload.get("expires_in") or _LONG_LIVED_TOKEN_SECONDS)
                break

            error = poll_payload.get("error")
            if error == "authorization_pending":
                time.sleep(current_interval)
                continue
            if error == "slow_down":
                current_interval += 5
                time.sleep(current_interval)
                continue
            if error == "expired_token":
                raise RuntimeError("GitHub device code expired. Please run login again.")
            if error == "access_denied":
                raise RuntimeError("GitHub device flow was denied.")
            if error:
                desc = poll_payload.get("error_description") or error
                raise RuntimeError(str(desc))
            time.sleep(current_interval)
        else:
            raise RuntimeError("GitHub device flow timed out.")

        user = client.get(
            DEFAULT_GITHUB_USER_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
            },
        )
        user.raise_for_status()
        user_payload = user.json()
        account_id = user_payload.get("login") or str(user_payload.get("id") or "") or None

    from oauth_cli_kit.models import OAuthToken

    expires_ms = int((time.time() + token_expires_in) * 1000)
    token = OAuthToken(
        access=str(access_token),
        refresh="",
        expires=expires_ms,
        account_id=str(account_id) if account_id else None,
    )
    get_storage().save(token)
    return token


class GitHubCopilotProvider(OpenAICompatProvider):
    """Provider that exchanges a stored GitHub OAuth token for Copilot access tokens."""

    def __init__(self, default_model: str = "github-copilot/gpt-4.1"):
        from durin.providers.registry import find_by_name

        self._copilot_access_token: str | None = None
        self._copilot_expires_at: float = 0.0
        super().__init__(
            api_key="no-key",
            api_base=DEFAULT_COPILOT_BASE_URL,
            default_model=default_model,
            extra_headers={
                "Editor-Version": EDITOR_VERSION,
                "Editor-Plugin-Version": EDITOR_PLUGIN_VERSION,
                "User-Agent": USER_AGENT,
            },
            spec=find_by_name("github_copilot"),
        )

    async def _get_copilot_access_token(self) -> str:
        now = time.time()
        if self._copilot_access_token and now < self._copilot_expires_at - _EXPIRY_SKEW_SECONDS:
            return self._copilot_access_token

        github_token = _load_github_token()
        if not github_token or not github_token.access:
            raise RuntimeError("GitHub Copilot is not logged in. Run: durin provider login github-copilot")

        timeout = httpx.Timeout(20.0, connect=20.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=True) as client:
            response = await client.get(
                DEFAULT_COPILOT_TOKEN_URL,
                headers=_copilot_headers(github_token.access),
            )
            response.raise_for_status()
            payload = response.json()

        token = payload.get("token")
        if not token:
            raise RuntimeError("GitHub Copilot token exchange returned no token.")

        expires_at = payload.get("expires_at")
        if isinstance(expires_at, (int, float)):
            self._copilot_expires_at = float(expires_at)
        else:
            refresh_in = payload.get("refresh_in") or 1500
            self._copilot_expires_at = time.time() + int(refresh_in)
        self._copilot_access_token = str(token)
        return self._copilot_access_token

    async def _refresh_client_api_key(self) -> str:
        token = await self._get_copilot_access_token()
        self.api_key = token
        self._client.api_key = token
        return token

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, object] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        extra_body: dict[str, object] | None = None,
    ):
        await self._refresh_client_api_key()
        return await super().chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
            top_p=top_p,
            top_k=top_k,
            repeat_penalty=repeat_penalty,
            extra_body=extra_body,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, object] | None = None,
        on_content_delta: Callable[[str], None] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        repeat_penalty: float | None = None,
        extra_body: dict[str, object] | None = None,
    ):
        await self._refresh_client_api_key()
        return await super().chat_stream(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
            on_content_delta=on_content_delta,
            on_thinking_delta=on_thinking_delta,
            top_p=top_p,
            top_k=top_k,
            repeat_penalty=repeat_penalty,
            extra_body=extra_body,
        )
