"""WebSocket server channel: durin acts as a WebSocket server and serves connected clients."""

from __future__ import annotations

import asyncio
import base64
import binascii
import email.utils
import hashlib
import hmac
import http
import json
import mimetypes
import re
import secrets
import shutil
import ssl
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger
from pydantic import Field, field_validator, model_validator
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from durin.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.command.builtin import builtin_command_palette
from durin.config.paths import get_media_dir
from durin.config.schema import Base
from durin.session.goal_state import goal_state_ws_blob
from durin.utils.helpers import safe_filename
from durin.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from durin.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from durin.utils.webui_thread_disk import delete_webui_thread
from durin.utils.webui_transcript import append_transcript_object, build_webui_thread_response
from durin.utils.webui_turn_helpers import websocket_turn_wall_started_at

if TYPE_CHECKING:
    from durin.session.manager import SessionManager


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def _normalize_config_path(path: str) -> str:
    return _strip_trailing_slash(path)


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    - ``client_id``: Used for ``allow_from`` authorization; if omitted, a value is generated and logged.
    - ``token``: If non-empty, the ``token`` query param may match this static secret; short-lived tokens
      from ``token_issue_path`` are also accepted.
    - ``token_issue_path``: If non-empty, **GET** (HTTP/1.1) to this path returns JSON
      ``{"token": "...", "expires_in": <seconds>}``; use ``?token=...`` when opening the WebSocket.
      Must differ from ``path`` (the WS upgrade path). If the client runs in the **same process** as
      durin and shares the asyncio loop, use a thread or async HTTP client for GET—do not call
      blocking ``urllib`` or synchronous ``httpx`` from inside a coroutine.
    - ``token_issue_secret``: If non-empty, token requests must send ``Authorization: Bearer <secret>`` or
      ``X-Durin-Auth: <secret>``.
    - ``websocket_requires_token``: If True, the handshake must include a valid token (static or issued and not expired).
    - Each connection has its own session: a unique ``chat_id`` maps to the agent session internally.
    - ``media`` field in outbound messages contains local filesystem paths; remote clients need a
      shared filesystem or an HTTP file server to access these files.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer). 4 × 6 MB × 1.37
    # (base64 overhead) + envelope framing stays under 36 MB; the 40 MB ceiling
    # leaves a small margin for sender slop without opening a DoS avenue.
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if not self.token_issue_path:
            return self
        if _normalize_config_path(self.token_issue_path) == _normalize_config_path(self.path):
            raise ValueError("token_issue_path must differ from path (the WebSocket upgrade path)")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.token.strip() or self.token_issue_secret.strip():
            return self
        raise ValueError(
            "host is 0.0.0.0 (all interfaces) but neither token nor "
            "token_issue_secret is set — set one to prevent unauthenticated access"
        )


def _http_json_response(data: dict[str, Any], *, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json; charset=utf-8"),
        ]
    )
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, headers, body)


def publish_runtime_model_update(
    bus: MessageBus,
    model: str,
    model_preset: str | None,
) -> None:
    """Enqueue a runtime model snapshot for websocket subscribers (fan-out in-channel)."""
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={
            "_runtime_model_updated": True,
            "model": model,
            "model_preset": model_preset,
        },
    ))


def _default_model_name_from_config() -> str | None:
    """Resolved model string from on-disk config (bootstrap fallback)."""
    try:
        from durin.config.loader import load_config

        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str | None:
    """Prefer an in-process resolver (e.g. AgentLoop); else config-derived default."""
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config()


def _parse_request_path(path_with_query: str) -> tuple[str, dict[str, list[str]]]:
    """Parse normalized path and query parameters in one pass."""
    parsed = urlparse("ws://x" + path_with_query)
    path = _strip_trailing_slash(parsed.path or "/")
    return path, parse_qs(parsed.query, keep_blank_values=True)


def _normalize_http_path(path_with_query: str) -> str:
    """Return the path component (no query string), with trailing slash normalized (root stays ``/``)."""
    return _parse_request_path(path_with_query)[0]


def _parse_query(path_with_query: str) -> dict[str, list[str]]:
    return _parse_request_path(path_with_query)[1]


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key*, or None."""
    values = query.get(key)
    return values[0] if values else None


def _mask_secret_hint(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"


_WEB_SEARCH_PROVIDER_OPTIONS: tuple[dict[str, str], ...] = (
    {"name": "duckduckgo", "label": "DuckDuckGo", "credential": "none"},
    {"name": "brave", "label": "Brave Search", "credential": "api_key"},
    {"name": "tavily", "label": "Tavily", "credential": "api_key"},
    {"name": "searxng", "label": "SearXNG", "credential": "base_url"},
    {"name": "jina", "label": "Jina", "credential": "api_key"},
    {"name": "kagi", "label": "Kagi", "credential": "api_key"},
    {"name": "olostep", "label": "Olostep", "credential": "api_key"},
)
_WEB_SEARCH_PROVIDER_BY_NAME = {
    provider["name"]: provider for provider in _WEB_SEARCH_PROVIDER_OPTIONS
}


def _parse_inbound_payload(raw: str) -> str | None:
    """Parse a client frame into text; return None for empty or unrecognized content."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


# Accept UUIDs and short scoped keys like "unified:default". Keeps the capability
# namespace small enough to rule out path traversal / quote injection tricks.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """Return a typed envelope dict if the frame is a new-style JSON envelope, else None.

    A frame qualifies when it parses as a JSON object with a string ``type`` field.
    Legacy frames (plain text, or ``{"content": ...}`` without ``type``) return None;
    callers should fall back to :func:`_parse_inbound_payload` for those.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


# Per-message media limits. The server-side guard is a touch looser than the
# client's ``Worker`` normalization target (6 MB) — tolerate client slop, but
# still cap total ingress at ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``
# which fits comfortably inside ``max_message_bytes``.
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024

# Image MIME whitelist — matches the Composer's ``accept`` list. SVG is
# explicitly excluded to avoid the XSS surface inside embedded scripts.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED

_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


_LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Matches the legacy chat-id pattern but allows file-system-safe stems too,
# so the API can address sessions whose keys came from non-WebSocket channels.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


def _decode_api_key(raw_key: str) -> str | None:
    """Decode a percent-encoded API path segment, then validate the result."""
    key = unquote(raw_key)
    if _API_KEY_RE.match(key) is None:
        return None
    return key


def _is_localhost(connection: Any) -> bool:
    """Return True if *connection* originated from the loopback interface."""
    addr = getattr(connection, "remote_address", None)
    if not addr:
        return False
    host = addr[0] if isinstance(addr, tuple) else addr
    if not isinstance(host, str):
        return False
    # ``::ffff:127.0.0.1`` is loopback in IPv6-mapped form.
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _LOCALHOSTS


def _http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: list[tuple[str, str]] | None = None,
) -> Response:
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def _http_error(status: int, message: str | None = None) -> Response:
    body = (message or http.HTTPStatus(status).phrase).encode("utf-8")
    return _http_response(body, status=status)


def _bearer_token(headers: Any) -> str | None:
    """Pull a Bearer token out of standard or query-style headers."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _is_websocket_upgrade(request: WsRequest) -> bool:
    """Detect an actual WS upgrade; plain HTTP GETs to the same path should fall through."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 without padding — compact + friendly in URL paths."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Reverse of :func:`_b64url_encode`; caller handles ``ValueError``."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# Allowed MIME types we actually serve from the media endpoint. Anything
# outside this set is degraded to ``application/octet-stream`` so an
# attacker who somehow gets a signed URL for an unexpected file type can't
# trick the browser into sniffing executable content.
_MEDIA_ALLOWED_MIMES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/webm",
    "video/quicktime",
})


def _issue_route_secret_matches(headers: Any, configured_secret: str) -> bool:
    """Return True if the token-issue HTTP request carries credentials matching ``token_issue_secret``."""
    if not configured_secret:
        return True
    authorization = headers.get("Authorization") or headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        return hmac.compare_digest(supplied, configured_secret)
    header_token = headers.get("X-Durin-Auth") or headers.get("x-durin-auth")
    if not header_token:
        return False
    return hmac.compare_digest(header_token.strip(), configured_secret)


class WebSocketChannel(BaseChannel):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        static_dist_path: Path | None = None,
        runtime_model_name: Callable[[], str | None] | None = None,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        # Single-use tokens consumed at WebSocket handshake.
        self._issued_tokens: dict[str, float] = {}
        # Multi-use tokens for HTTP routes served beside WS; checked but not consumed.
        self._api_tokens: dict[str, float] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._session_manager = session_manager
        self._static_dist_path: Path | None = (
            static_dist_path.resolve() if static_dist_path is not None else None
        )
        self._runtime_model_name = runtime_model_name
        # Process-local secret used to HMAC-sign media URLs. The signed URL is
        # the capability — anyone who holds a valid URL can fetch that one
        # file, nothing else. The secret regenerates on restart so links
        # become self-expiring (callers just refresh the session list).
        self._media_secret: bytes = secrets.token_bytes(32)

    # -- Subscription bookkeeping -------------------------------------------

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        """Remove *connection* from every subscription set; safe to call multiple times."""
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """Replay an active sustained goal from session metadata after *chat_id* is subscribed.

        Goal metadata lives on the session JSONL and survives gateway restarts, but
        connected clients normally see it via ``goal_state`` / ``turn_end`` frames.
        Pushing here makes refresh + reconnect restore the strip without a new model turn.
        """
        if self._session_manager is None:
            return
        row = self._session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        blob = goal_state_ws_blob(meta)
        if not blob.get("active"):
            return
        await self.send_goal_state(chat_id, blob)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """Replay ``goal_status: running`` when a turn is still active (same-process refresh)."""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay goal/run strip state after subscribe (same-process refresh)."""
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    _MAX_ISSUED_TOKENS = 10_000

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._issued_tokens.items()):
            if now > expiry:
                self._issued_tokens.pop(token_key, None)

    def _take_issued_token_if_valid(self, token_value: str | None) -> bool:
        """Validate and consume one issued token (single use per connection attempt).

        Uses single-step pop to minimize the window between lookup and removal;
        safe under asyncio's single-threaded cooperative model.
        """
        if not token_value:
            return False
        self._purge_expired_issued_tokens()
        expiry = self._issued_tokens.pop(token_value, None)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            return False
        return True

    def _handle_token_issue_http(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self.logger.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        self._purge_expired_issued_tokens()
        if len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS:
            self.logger.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self._issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        self._issued_tokens[token_value] = time.monotonic() + float(self.config.token_ttl_s)

        return _http_json_response(
            {"token": token_value, "expires_in": self.config.token_ttl_s}
        )

    # -- HTTP dispatch ------------------------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to a handler or to the WS upgrade path."""
        got, query = _parse_request_path(request.path)

        # 1. Token issue endpoint (legacy, optional, gated by configured secret).
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue_http(connection, request)

        # 2. Bootstrap (`/webui/bootstrap`): mint WS/API tokens + shared session metadata.
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # 3. REST handlers co-located with this channel (sessions, settings, …).
        if got == "/api/sessions":
            return self._handle_sessions_list(request)

        if got == "/api/settings":
            return self._handle_settings(request)

        if got == "/api/commands":
            return self._handle_commands(request)

        if got == "/api/settings/update":
            return self._handle_settings_update(request)

        if got == "/api/settings/provider/update":
            return self._handle_settings_provider_update(request)

        if got == "/api/settings/web-search/update":
            return self._handle_settings_web_search_update(request)

        if got == "/api/secrets":
            return self._handle_secrets_list(request)

        if got == "/api/secrets/delete":
            return self._handle_secret_delete(request)

        if got == "/api/cron":
            return self._handle_cron_list(request)

        if got == "/api/cron/remove":
            return self._handle_cron_remove(request)

        if got == "/api/cron/toggle":
            return self._handle_cron_toggle(request)

        if got == "/api/config":
            return self._handle_config_get(request)

        if got == "/api/config/set":
            return self._handle_config_set(request)

        if got == "/api/skills":
            return self._handle_skills_list(request)

        # Exact matches BEFORE the `([^/]+)` patterns so "quarantine"/"resolve"/
        # "import" are not captured as skill names by `^/api/skills/([^/]+)$`.
        if got == "/api/skills/quarantine":
            return self._handle_skills_quarantine(request)

        if got == "/api/skills/resolve":
            return self._handle_skills_resolve(request)

        if got == "/api/skills/import":
            return self._handle_skills_import(request)

        if got == "/api/skills/github-token-test":
            return self._handle_skills_github_token_test(request)

        if got == "/api/skills/search":
            return await self._handle_skill_search(request)

        m = re.match(r"^/api/skills/([^/]+)/save$", got)
        if m:
            return self._handle_skill_save(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/mode$", got)
        if m:
            return self._handle_skill_mode(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/approve$", got)
        if m:
            return self._handle_skill_approve(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/reject$", got)
        if m:
            return self._handle_skill_reject(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/judge$", got)
        if m:
            return await self._handle_skill_judge(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)$", got)
        if m:
            return self._handle_skill_get(request, m.group(1))

        if got == "/api/channels":
            return self._handle_channels_list(request)

        if got == "/api/models":
            return self._handle_models_list(request)

        if got == "/api/memory/graph":
            return self._handle_memory_graph(request)

        if got == "/api/memory/search":
            return await self._handle_memory_search_api(request, query)

        m = re.match(r"^/api/memory/entity/(.+)$", got)
        if m:
            return self._handle_memory_entity(request, m.group(1))

        m = re.match(r"^/api/memory/session/(.+)$", got)
        if m:
            return self._handle_memory_session(request, m.group(1))

        m = re.match(r"^/api/memory/edge/([^/]+)/([^/]+)$", got)
        if m:
            return self._handle_memory_edge(request, m.group(1), m.group(2))

        if got == "/api/memory/entry":
            return self._handle_memory_entry(request, query)

        if got == "/api/memory/forget":
            return self._handle_memory_forget(request, query)

        if got == "/api/memory/backlinks":
            return self._handle_memory_backlinks(request, query)

        if got == "/api/model/test":
            return await self._handle_model_test(request)

        if got == "/api/model/capabilities":
            return self._handle_model_capabilities(request)

        if got == "/api/memory/cross-encoder/test":
            return await self._handle_cross_encoder_test(request, query)

        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        # NOTE: websockets' HTTP parser only accepts GET, so we cannot expose a
        # true ``DELETE`` verb. The action is folded into the path instead.
        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        # P2 (doc 20): user-driven rename. Same constraint as delete —
        # GET-only HTTP, action folded into the path. New title arrives
        # as a ``title`` query param (URL-encoded).
        m = re.match(r"^/api/sessions/([^/]+)/rename$", got)
        if m:
            return self._handle_session_rename(request, m.group(1))

        # Signed media fetch: ``<sig>`` is an HMAC over ``<payload>``; the
        # payload decodes to a path inside :func:`get_media_dir`. See
        # :meth:`_sign_media_path` for the inverse direction used to build
        # these URLs when replaying a session.
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2))

        # 4. WebSocket upgrade (the channel's primary purpose). Only run the
        # handshake gate on requests that actually ask to upgrade; otherwise
        # a bare ``GET /`` from the browser would be rejected as an
        # unauthorized WS handshake instead of serving the SPA's index.html.
        expected_ws = self._expected_path()
        if got == expected_ws and _is_websocket_upgrade(request):
            client_id = _query_first(query, "client_id") or ""
            if len(client_id) > 128:
                client_id = client_id[:128]
            if not self.is_allowed(client_id):
                return connection.respond(403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)

        # 5. Static SPA serving (only if a build directory was wired in).
        if self._static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    # -- HTTP route handlers ------------------------------------------------

    def _check_api_token(self, request: WsRequest) -> bool:
        """Validate a request against the API token pool (multi-use, TTL-bound)."""
        self._purge_expired_api_tokens()
        token = _bearer_token(request.headers) or _query_first(
            _parse_query(request.path), "token"
        )
        if not token:
            return False
        expiry = self._api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self._api_tokens.pop(token, None)
            return False
        return True

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._api_tokens.items()):
            if now > expiry:
                self._api_tokens.pop(token_key, None)

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        # When a secret is configured (token_issue_secret or static token),
        # validate it regardless of source IP.  This secures deployments
        # behind a reverse proxy where all connections appear as localhost.
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not _is_localhost(connection):
            # No secret configured: only allow localhost (local dev mode).
            return _http_error(403, "bootstrap is localhost-only")
        # Cap outstanding tokens to avoid runaway growth from a misbehaving client.
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if (
            len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        token = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(self.config.token_ttl_s)
        # Same string registered in both pools: the WS handshake consumes one copy
        # while the REST surface keeps validating the other until TTL expiry.
        self._issued_tokens[token] = expiry
        self._api_tokens[token] = expiry
        return _http_json_response(
            {
                "token": token,
                "ws_path": self._expected_path(),
                "expires_in": self.config.token_ttl_s,
                "model_name": _resolve_bootstrap_model_name(self._runtime_model_name),
                # True when this deploy gates bootstrap on a setup
                # secret (token_issue_secret or static token). The
                # webui uses this to decide whether to expose a
                # "Logout" affordance — without a secret in play,
                # logout would just strand the user on an auth form
                # they have nothing to type into (the bootstrap
                # auto-mints tokens for localhost). UX trap removed
                # by hiding the button when requires_secret=false.
                "requires_secret": bool(secret),
            }
        )

    def _handle_memory_graph(self, request: WsRequest) -> Response:
        """GET /api/memory/graph — entity-centric memory as nodes + edges.

        Read-only over ``memory/entities/<type>/*.md`` + episodic
        co-occurrence. Powers the Obsidian-style graph view in the
        webui. No LLM call, no mutation. See
        :func:`durin.memory.graph.build_memory_graph` for the shape.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.config.loader import load_config
        from durin.memory.graph import build_memory_graph

        try:
            workspace = load_config().workspace_path
            payload = build_memory_graph(workspace)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory graph build failed")
            return _http_error(500, f"memory graph build failed: {exc}")
        return _http_json_response(payload)

    def _handle_memory_entity(self, request: WsRequest, ref_encoded: str) -> Response:
        """GET /api/memory/entity/<ref> — full page + history + archive + entries.

        ``<ref>`` is the entity reference (``type:slug``) URL-encoded —
        ``person%3Amarcelo``. Returns 404 when the canonical page is
        missing on disk. See :func:`graph_api.get_entity_detail`.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        from durin.config.loader import load_config
        from durin.memory.graph_api import get_entity_detail

        ref = unquote(ref_encoded)
        try:
            workspace = load_config().workspace_path
            payload = get_entity_detail(workspace, ref)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory entity detail failed for %s", ref)
            return _http_error(500, f"entity detail failed: {exc}")
        if payload is None:
            return _http_error(404, f"entity not found: {ref}")
        return _http_json_response(payload)

    def _handle_memory_session(
        self, request: WsRequest, stem_encoded: str,
    ) -> Response:
        """GET /api/memory/session/<stem> — session detail for the graph view.

        ``<stem>`` is the filename stem (e.g. ``cli_direct``,
        ``websocket_<uuid>``), URL-encoded. Returns 404 when the
        corresponding ``sessions/<stem>.jsonl`` doesn't exist.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        from durin.config.loader import load_config
        from durin.memory.graph_api import get_session_detail

        stem = unquote(stem_encoded)
        try:
            workspace = load_config().workspace_path
            payload = get_session_detail(workspace, stem)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory session detail failed for %s", stem)
            return _http_error(500, f"session detail failed: {exc}")
        if payload is None:
            return _http_error(404, f"session not found: {stem}")
        return _http_json_response(payload)

    def _handle_memory_entry(
        self, request: WsRequest, query: dict[str, list[str]],
    ) -> Response:
        """GET /api/memory/entry?uri=memory/<class>/<id> — one entry's frontmatter + body.

        Distinct from ``_handle_memory_entity`` (which serves entity
        PAGES under ``memory/entities/``). This handler is for the
        individual entries — episodic / stable / corpus /
        session_summary — that the P12 Entries panel browses.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.config.loader import load_config
        from durin.memory.graph_api import get_entry_detail

        uri = (_query_first(query, "uri") or "").strip()
        try:
            workspace = load_config().workspace_path
            payload = get_entry_detail(workspace, uri)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory entry detail failed for %s", uri)
            return _http_error(500, f"entry detail failed: {exc}")
        if payload is None:
            return _http_error(404, f"entry not found: {uri}")
        return _http_json_response(payload)

    def _handle_memory_forget(
        self, request: WsRequest, query: dict[str, list[str]],
    ) -> Response:
        """GET /api/memory/forget?uri=memory/<class>/<id> — archive an entry.

        Returns ``{"result": "archived"|"not_found"|"protected"|"invalid"}``.
        HTTP status mirrors the result: 200 archived/not_found,
        403 protected, 400 invalid. (Operators can distinguish "URI
        was malformed" from "URI named a real entry that's gone"
        without parsing the body.)
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.config.loader import load_config
        from durin.memory.graph_api import forget_entry

        uri = (_query_first(query, "uri") or "").strip()
        try:
            workspace = load_config().workspace_path
            payload = forget_entry(workspace, uri)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory forget failed for %s", uri)
            return _http_error(500, f"forget failed: {exc}")
        result = payload.get("result")
        if result == "protected":
            # 403 + payload so the UI can switch on result text too.
            return _http_json_response(payload, status=403)
        if result == "invalid":
            return _http_json_response(payload, status=400)
        return _http_json_response(payload)

    def _handle_memory_backlinks(
        self, request: WsRequest, query: dict[str, list[str]],
    ) -> Response:
        """GET /api/memory/backlinks?uri=memory/<class>/<id> — entries that reference this one.

        Walks ``memory/`` (excluding archive / pending) once and returns
        up to 50 hits (``truncated`` flag indicates more were found).
        Synchronous; benchmark target is < 100 ms over O(thousands) of
        entries.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.config.loader import load_config
        from durin.memory.graph_api import get_entry_backlinks

        uri = (_query_first(query, "uri") or "").strip()
        try:
            workspace = load_config().workspace_path
            payload = get_entry_backlinks(workspace, uri)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory backlinks failed for %s", uri)
            return _http_error(500, f"backlinks failed: {exc}")
        return _http_json_response(payload)

    def _handle_memory_edge(
        self, request: WsRequest, source_enc: str, target_enc: str,
    ) -> Response:
        """GET /api/memory/edge/<source>/<target> — entries co-mentioning both.

        Both refs URL-encoded. Returns the raw evidence behind a graph
        edge: episodic entries that tag BOTH refs.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        from durin.config.loader import load_config
        from durin.memory.graph_api import get_edge_detail

        a = unquote(source_enc)
        b = unquote(target_enc)
        try:
            workspace = load_config().workspace_path
            payload = get_edge_detail(workspace, a, b)
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory edge detail failed for %s ↔ %s", a, b)
            return _http_error(500, f"edge detail failed: {exc}")
        return _http_json_response(payload)

    async def _handle_memory_search_api(
        self, request: WsRequest, query: dict[str, list[str]],
    ) -> Response:
        """GET /api/memory/search?q=…&scope=…&level=… — same shape as memory_search tool.

        Mirrors the LLM tool's surface so the webui filter behaves
        identically to what the agent sees. Results carry ``kind`` +
        ``rendered`` per doc 25 §2.H so canonical vs fragment markers
        stay consistent across paths.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.config.loader import load_config
        from durin.memory.graph_api import search_memory_api

        q = _query_first(query, "q") or ""
        scope = _query_first(query, "scope") or "all"
        level = _query_first(query, "level") or "warm"
        try:
            cfg = load_config()
            workspace = cfg.workspace_path
            embedding_model = None
            try:
                if cfg.memory.enabled:
                    embedding_model = cfg.memory.embedding.model
            except (AttributeError, TypeError):
                embedding_model = None
            payload = await search_memory_api(
                workspace, q, scope=scope, level=level,
                embedding_model=embedding_model,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("memory search api failed")
            return _http_error(500, f"memory search failed: {exc}")
        return _http_json_response(payload)

    def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        sessions = self._session_manager.list_sessions()
        # Sidebar/chat listing for WS-backed sessions only — CLI / Slack / etc.
        # keys are not intended for resume over this HTTP surface.
        cleaned = [
            {k: v for k, v in s.items() if k != "path"}
            for s in sessions
            if isinstance(s.get("key"), str) and s["key"].startswith("websocket:")
        ]
        return _http_json_response({"sessions": cleaned})

    def _settings_payload(self, *, requires_restart: bool = False) -> dict[str, Any]:
        from durin.config.loader import get_config_path, load_config
        from durin.providers.registry import PROVIDERS, find_by_name

        config = load_config()
        defaults = config.agents.defaults
        provider_name = config.get_provider_name(defaults.model) or defaults.provider
        provider = config.get_provider(defaults.model)
        selected_provider = provider_name
        if defaults.provider != "auto":
            spec = find_by_name(defaults.provider)
            selected_provider = spec.name if spec else provider_name
        providers = []
        for spec in PROVIDERS:
            provider_config = getattr(config.providers, spec.name, None)
            if provider_config is None or spec.is_oauth or spec.is_local:
                continue
            providers.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "configured": bool(provider_config.api_key),
                    "api_key_hint": _mask_secret_hint(provider_config.api_key),
                    "api_base": provider_config.api_base,
                    "default_api_base": spec.default_api_base or None,
                }
            )
        search_config = config.tools.web.search
        search_provider = (
            search_config.provider
            if search_config.provider in _WEB_SEARCH_PROVIDER_BY_NAME
            else "duckduckgo"
        )
        return {
            "agent": {
                "model": defaults.model,
                "provider": selected_provider,
                "resolved_provider": provider_name,
                "has_api_key": bool(provider and provider.api_key),
            },
            "providers": providers,
            "web_search": {
                "provider": search_provider,
                "api_key_hint": _mask_secret_hint(search_config.api_key),
                "base_url": search_config.base_url or None,
                "providers": list(_WEB_SEARCH_PROVIDER_OPTIONS),
            },
            "runtime": {
                "config_path": str(get_config_path().expanduser()),
            },
            "requires_restart": requires_restart,
        }

    def _handle_settings(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(self._settings_payload())

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_settings_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.config.loader import load_config, save_config
        from durin.providers.registry import find_by_name

        query = _parse_query(request.path)
        config = load_config()
        defaults = config.agents.defaults
        changed = False

        model = _query_first(query, "model")
        if model is not None:
            model = model.strip()
            if not model:
                return _http_error(400, "model is required")
            if defaults.model != model:
                defaults.model = model
                changed = True

        provider = _query_first(query, "provider")
        if provider is not None:
            provider = provider.strip()
            if not provider:
                return _http_error(400, "provider is required")
            if find_by_name(provider) is None:
                return _http_error(400, "unknown provider")
            provider_config = getattr(config.providers, provider, None)
            if provider_config is None or not provider_config.api_key:
                return _http_error(400, "provider is not configured")
            if defaults.provider != provider:
                defaults.provider = provider
                changed = True

        if changed:
            save_config(config)
        # LLM provider/model changes are hot-reloaded by AgentLoop before each
        # new turn via the provider snapshot loader, so a restart is unnecessary.
        return _http_json_response(self._settings_payload(requires_restart=False))

    def _handle_settings_provider_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.config.loader import load_config, save_config
        from durin.providers.registry import find_by_name

        query = _parse_query(request.path)
        provider_name = (_query_first(query, "provider") or "").strip()
        if not provider_name:
            return _http_error(400, "provider is required")
        spec = find_by_name(provider_name)
        if spec is None or spec.is_oauth or spec.is_local:
            return _http_error(400, "unknown provider")

        config = load_config()
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is None:
            return _http_error(400, "unknown provider")

        changed = False
        if "api_key" in query or "apiKey" in query:
            api_key = _query_first(query, "api_key")
            if api_key is None:
                api_key = _query_first(query, "apiKey")
            api_key = (api_key or "").strip() or None
            # Store the key in the secret store and keep only a
            # ${secret:} reference in config — the dashboard must not
            # write plaintext (see docs/11_secrets_design.md).
            from durin.security.secrets import is_secret_ref, store_secret

            if api_key and not is_secret_ref(api_key):
                api_key = store_secret(
                    f"{spec.name}_API_KEY",
                    api_key,
                    service=f"provider:{spec.name}",
                    scope=[f"provider:{spec.name}"],
                    description=f"{spec.name} API key",
                    origin="webui",
                )
            if provider_config.api_key != api_key:
                provider_config.api_key = api_key
                changed = True

        if "api_base" in query or "apiBase" in query:
            api_base = _query_first(query, "api_base")
            if api_base is None:
                api_base = _query_first(query, "apiBase")
            api_base = (api_base or "").strip() or None
            if provider_config.api_base != api_base:
                provider_config.api_base = api_base
                changed = True

        if changed:
            save_config(config)
        # API key/base changes are picked up by the next provider snapshot refresh.
        return _http_json_response(self._settings_payload(requires_restart=False))

    def _handle_settings_web_search_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.config.loader import load_config, save_config

        query = _parse_query(request.path)
        provider_name = (_query_first(query, "provider") or "").strip().lower()
        provider_option = _WEB_SEARCH_PROVIDER_BY_NAME.get(provider_name)
        if provider_option is None:
            return _http_error(400, "unknown web search provider")

        config = load_config()
        search_config = config.tools.web.search
        previous_provider = search_config.provider
        changed = False

        def set_value(attr: str, value: str | None) -> None:
            nonlocal changed
            if getattr(search_config, attr) != value:
                setattr(search_config, attr, value)
                changed = True

        if search_config.provider != provider_name:
            search_config.provider = provider_name
            changed = True

        credential = provider_option["credential"]
        if credential == "none":
            set_value("api_key", "")
            set_value("base_url", "")
        elif credential == "base_url":
            base_url = _query_first(query, "base_url")
            if base_url is None:
                base_url = _query_first(query, "baseUrl")
            base_url = base_url.strip() if base_url is not None else None
            if not base_url and previous_provider == provider_name and search_config.base_url:
                base_url = search_config.base_url
            if not base_url:
                return _http_error(400, "base_url is required")
            set_value("base_url", base_url)
            set_value("api_key", "")
        else:
            api_key = _query_first(query, "api_key")
            if api_key is None:
                api_key = _query_first(query, "apiKey")
            api_key = api_key.strip() if api_key is not None else None
            if not api_key and previous_provider == provider_name and search_config.api_key:
                api_key = search_config.api_key
            if not api_key:
                return _http_error(400, "api_key is required")
            set_value("api_key", api_key)
            set_value("base_url", "")

        if changed:
            save_config(config)
        return _http_json_response(self._settings_payload(requires_restart=False))

    # -- secret store --------------------------------------------------------

    def _handle_secrets_list(self, request: WsRequest) -> Response:
        """`GET /api/secrets` — entries' metadata. Never returns a value."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.security.secrets import SecretStore

        store = SecretStore().load()
        items = [
            {
                "name": name,
                "service": entry.service,
                "account": entry.account or "",
                "description": entry.description,
                "scope": list(entry.scope),
                "origin": entry.origin,
                "created_at": entry.created_at,
                "value_hint": _mask_secret_hint(entry.value),
            }
            for name, entry in sorted(store.all().items())
        ]
        return _http_json_response({"secrets": items})

    # A secret is created/updated over the websocket — see the
    # ``secret_store`` envelope in ``_handle_secret_store_envelope``.
    # The value rides a JSON frame, never a URL query.

    def _handle_secret_delete(self, request: WsRequest) -> Response:
        """`GET /api/secrets/delete` — remove a secret."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.security.secrets import SecretStore, get_secret_store

        name = (_query_first(_parse_query(request.path), "name") or "").strip()
        store = SecretStore().load()
        if not store.remove(name):
            return _http_error(404, "no such secret")
        store.save()
        get_secret_store(reload=True)
        return _http_json_response({"ok": True})

    # -- cron API (P11) ----------------------------------------------------

    def _fresh_cron_service(self):
        """Build a non-running CronService bound to the workspace's
        store path. Read-only ops (`list_jobs`) work directly; mutating
        ops (`remove_job`, `enable_job`) write to the action.jsonl log
        which the gateway's running service drains on its next tick.
        Avoids reaching across processes for a shared CronService
        handle — mirrors how the SecretStore endpoints work.
        """
        from durin.config.loader import load_config
        from durin.cron.service import CronService

        cfg = load_config()
        path = cfg.workspace_path / "cron" / "jobs.json"
        return CronService(path)

    def _cron_job_to_dict(self, job) -> dict:
        sched = job.schedule
        # Render a human label per schedule kind. The full structured
        # data also goes out so the frontend can format dates locally.
        if sched.kind == "every":
            secs = (sched.every_ms or 0) // 1000
            label = f"every {secs}s"
        elif sched.kind == "cron":
            tz = f" ({sched.tz})" if sched.tz else ""
            label = f"{sched.expr}{tz}"
        elif sched.kind == "at":
            label = f"once at {sched.at_ms}"
        else:
            label = sched.kind
        is_system = job.payload.kind == "system_event"
        return {
            "id": job.id,
            "name": job.name,
            "enabled": job.enabled,
            "is_system": is_system,
            "schedule": {
                "kind": sched.kind,
                "label": label,
                "expr": sched.expr,
                "every_ms": sched.every_ms,
                "at_ms": sched.at_ms,
                "tz": sched.tz,
            },
            # System jobs hide their message — usually internal references
            # to consolidation prompts that aren't useful to surface.
            "message": "" if is_system else job.payload.message,
            "channel": job.payload.channel or "",
            "state": {
                "next_run_at_ms": job.state.next_run_at_ms,
                "last_run_at_ms": job.state.last_run_at_ms,
                "last_status": job.state.last_status,
                "last_error": job.state.last_error,
            },
            "created_at_ms": job.created_at_ms,
            "updated_at_ms": job.updated_at_ms,
        }

    def _handle_skills_list(self, request: WsRequest) -> Response:
        """`GET /api/skills` — list installed skills + the skills store HEAD."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_list(workspace)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skills list failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skills_quarantine(self, request: WsRequest) -> Response:
        """`GET /api/skills/quarantine` — list skills awaiting an import decision."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_quarantine(workspace)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skills quarantine failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skill_get(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}` — fetch a skill's mode + SKILL.md content."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_get(workspace, decoded)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skill read failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skill_save(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/save?content=...` — overwrite a MANUAL skill."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        query = _parse_query(request.path)
        content = _query_first(query, "content")
        if content is None:
            return _http_error(400, "content is required")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_save(workspace, decoded, content)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skill save failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skill_mode(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/mode?value=auto|manual` — set a skill's mode."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        query = _parse_query(request.path)
        value = (_query_first(query, "value") or "").strip()
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_mode(workspace, decoded, value)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"skill mode failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skills_resolve(self, request: WsRequest) -> Response:
        """`GET /api/skills/resolve?source=` — list the candidates a source points at."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        source = (_query_first(_parse_query(request.path), "source") or "").strip()
        if not source:
            return _http_error(400, "source is required")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_import_resolve(workspace, source)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"resolve failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skills_import(self, request: WsRequest) -> Response:
        """`GET /api/skills/import?source=` — fetch one candidate to quarantine + scan."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        source = (_query_first(_parse_query(request.path), "source") or "").strip()
        if not source:
            return _http_error(400, "source is required")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_import_fetch(workspace, source)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"import failed: {exc}")
        return _http_json_response(payload, status=status)

    async def _handle_skill_search(self, request: WsRequest) -> Response:
        """`GET /api/skills/search?query=&limit=` — search the configured registries.
        Async + off-thread: `web_skill_search` drives the registries via
        `asyncio.run`, so it MUST run in a worker thread, never the event loop."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        q = (_query_first(query, "query") or "").strip()
        if not q:
            return _http_error(400, "query is required")
        try:
            limit = int(_query_first(query, "limit") or 0)
        except ValueError:
            limit = 0
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = await asyncio.to_thread(ss.web_skill_search, workspace, q, limit)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"search failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skills_github_token_test(self, request: WsRequest) -> Response:
        """`GET /api/skills/github-token-test?secret=` — verify a GitHub-token secret."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        secret = (_query_first(_parse_query(request.path), "secret") or "").strip()
        from durin.agent import skills_store as ss
        try:
            status, payload = ss.web_github_token_test(secret)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"token test failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skill_approve(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/approve?confirm=&override=` — install through the gate."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        query = _parse_query(request.path)
        confirm = (_query_first(query, "confirm") or "").lower() in ("1", "true", "yes")
        override = (_query_first(query, "override") or "").lower() in ("1", "true", "yes")
        replace = (_query_first(query, "replace") or "").lower() in ("1", "true", "yes")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_skill_approve(workspace, decoded,
                                                   confirm=confirm, override=override,
                                                   replace=replace)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"approve failed: {exc}")
        return _http_json_response(payload, status=status)

    async def _handle_skill_judge(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/judge` — run the LLM judge on-demand. Async +
        off-thread so the (multi-second) model call doesn't stall the event loop."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = await asyncio.to_thread(ss.web_skill_judge, workspace, decoded)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"judge failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_skill_reject(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/reject` — discard a quarantined skill."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.agent import skills_store as ss
        from durin.config.loader import load_config
        try:
            workspace = load_config().workspace_path
            status, payload = ss.web_skill_reject(workspace, decoded)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"reject failed: {exc}")
        return _http_json_response(payload, status=status)

    def _handle_cron_list(self, request: WsRequest) -> Response:
        """`GET /api/cron` — list all scheduled jobs (including disabled
        + system jobs). System jobs are flagged so the UI can disable
        the delete button for them."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            cron = self._fresh_cron_service()
            jobs = cron.list_jobs(include_disabled=True)
            return _http_json_response({
                "jobs": [self._cron_job_to_dict(j) for j in jobs],
            })
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"could not load cron jobs: {exc}")

    def _handle_cron_remove(self, request: WsRequest) -> Response:
        """`GET /api/cron/remove?id=...` — remove a non-system job."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        job_id = (_query_first(_parse_query(request.path), "id") or "").strip()
        if not job_id:
            return _http_error(400, "id is required")
        try:
            cron = self._fresh_cron_service()
            result = cron.remove_job(job_id)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"could not remove job: {exc}")
        if result == "not_found":
            return _http_error(404, "no such job")
        if result == "protected":
            return _http_error(403, "system job; cannot remove")
        return _http_json_response({"result": result})

    def _handle_cron_toggle(self, request: WsRequest) -> Response:
        """`GET /api/cron/toggle?id=...&enabled=true|false` — enable
        or disable a job without removing it."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        job_id = (_query_first(query, "id") or "").strip()
        enabled_raw = (_query_first(query, "enabled") or "").strip().lower()
        if not job_id:
            return _http_error(400, "id is required")
        enabled = enabled_raw in ("1", "true", "yes")
        try:
            cron = self._fresh_cron_service()
            job = cron.enable_job(job_id, enabled=enabled)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"could not toggle job: {exc}")
        if job is None:
            return _http_error(404, "no such job")
        return _http_json_response({"job": self._cron_job_to_dict(job)})

    # -- generic config API (web parity Phase B) ---------------------------

    def _handle_config_get(self, request: WsRequest) -> Response:
        """`GET /api/config` — full effective config (secret-masked) + schema.

        The web equivalent of `durin config show`. Returns every field
        with its current value (defaults filled in) so the dashboard can
        render a settings form, plus the JSON schema for that form.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.cli.config_cmd import load_raw_config, mask_secrets, validate_dict
        from durin.config.loader import get_config_path
        from durin.config.schema import Config

        raw = load_raw_config(get_config_path())
        try:
            effective = validate_dict(raw).model_dump(mode="json", by_alias=True)
        except Exception:  # noqa: BLE001
            effective = raw
        return _http_json_response(
            {
                "config": mask_secrets(effective),
                "schema": Config.model_json_schema(by_alias=True),
            }
        )

    def _handle_config_set(self, request: WsRequest) -> Response:
        """`GET /api/config/set` — set one dotted key, schema-validated.

        The web equivalent of `durin config set`. ``value`` is JSON-
        decoded when possible (so booleans / numbers / objects work),
        else kept as a string. A schema-invalid value is rejected
        without writing.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.cli.config_cmd import (
            _normalize_dotted_path,
            load_raw_config,
            mask_secrets,
            parse_value,
            set_at,
            validate_dict,
        )
        from durin.config.loader import get_config_path, save_config

        query = _parse_query(request.path)
        key = (_query_first(query, "key") or "").strip()
        if not key:
            return _http_error(400, "key is required")
        raw_value = _query_first(query, "value")
        if raw_value is None:
            return _http_error(400, "value is required")

        path = get_config_path()
        try:
            canonical = validate_dict(load_raw_config(path)).model_dump(
                mode="json", by_alias=True
            )
        except Exception as e:  # noqa: BLE001
            return _http_error(400, f"on-disk config is invalid: {e}")
        new_data = set_at(canonical, _normalize_dotted_path(key), parse_value(raw_value))
        try:
            config = validate_dict(new_data)
        except Exception as e:  # noqa: BLE001
            return _http_error(400, f"validation failed: {e}")
        save_config(config, path)
        return _http_json_response(
            {
                "ok": True,
                "config": mask_secrets(
                    config.model_dump(mode="json", by_alias=True)
                ),
            }
        )

    def _handle_models_list(self, request: WsRequest) -> Response:
        """`GET /api/models?provider=X&capability=Y` — model catalog for the picker.

        ``suggested`` is the curated per-provider shortlist
        (``DEFAULT_MODELS[provider]``); ``models`` is the catalog filtered
        by:

        - ``capability``: ``vision`` keeps only models with
          ``supports_vision`` truthy; ``audio`` keeps
          ``supports_audio_input``; omit or ``text`` for no filter.
        - ``provider``: keeps only models whose id matches a keyword of
          the provider's ``ProviderSpec`` (the same heuristic
          ``_match_provider`` uses to auto-route by model name). Empty
          provider keeps the full catalog (filtered only by capability).
          For providers with no keywords (e.g. ``custom``, gateways like
          OpenRouter) we keep the full catalog too — they can route
          anything, so there's nothing to filter against.

        Image-generation models are always excluded — the picker is for
        chat/completion bridges only.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.cli.onboard_wizard import DEFAULT_MODELS
        from durin.providers.registry import find_by_name

        query = _parse_query(request.path)
        provider = (_query_first(query, "provider") or "").strip()
        capability = (_query_first(query, "capability") or "").strip().lower()
        suggested = list(DEFAULT_MODELS.get(provider, ()))

        # Resolve provider keywords once. Empty tuple means "don't filter"
        # (gateway / custom / unknown provider).
        #
        # Coding-plan variants (e.g. `zai_coding_plan` is the same
        # backend as `zhipu`, just a different endpoint with separate
        # quota) borrow their base provider's keywords so the catalog
        # surfaces the same models. The plan's own keywords are
        # intentionally rare (so auto-routing doesn't accidentally pick
        # them) — they shouldn't double as a model-filter heuristic.
        _plan_base = {
            "zai_coding_plan": "zhipu",
            "volcengine_coding_plan": "volcengine",
            "byteplus_coding_plan": "byteplus",
        }
        provider_keywords: tuple[str, ...] = ()
        if provider:
            lookup_name = _plan_base.get(provider, provider)
            spec = find_by_name(lookup_name)
            if spec is not None:
                provider_keywords = spec.keywords

        def _capability_ok(info: object) -> bool:
            if capability in ("", "text"):
                return True
            if not isinstance(info, dict):
                # Without capability metadata we can't prove the model
                # supports the modality — drop conservatively.
                return False
            if capability == "vision":
                return bool(info.get("supports_vision"))
            if capability == "audio":
                return bool(info.get("supports_audio_input"))
            return True

        def _provider_ok(mid: str) -> bool:
            if not provider_keywords:
                return True
            mid_lower = mid.lower()
            mid_normalized = mid_lower.replace("-", "_")
            for kw in provider_keywords:
                kw_lower = kw.lower()
                if kw_lower in mid_lower or kw_lower.replace("-", "_") in mid_normalized:
                    return True
            return False

        catalog: list[str] = []
        try:
            from durin.providers.capabilities import _load_capabilities_snapshot

            models = _load_capabilities_snapshot() or {}
            catalog = sorted(
                mid
                for mid, info in models.items()
                if isinstance(mid, str)
                # Exclude pure image-generation models from the chat pickers
                # (vision / audio / text) — durin has no image-gen feature.
                and (
                    not isinstance(info, dict)
                    or info.get("mode") != "image_generation"
                )
                and _capability_ok(info)
                and _provider_ok(mid)
            )
        except Exception:  # noqa: BLE001
            catalog = []

        # Trim suggested by the same capability filter so the curated
        # shortlist doesn't surface (e.g.) a text-only model in the vision
        # picker. Unknown ids in the snapshot stay (charity for fresh
        # picks the catalog doesn't have yet).
        if capability in ("vision", "audio", "image"):
            try:
                from durin.providers.capabilities import _load_capabilities_snapshot

                snapshot = _load_capabilities_snapshot() or {}
            except Exception:  # noqa: BLE001
                snapshot = {}
            suggested = [
                m for m in suggested
                if m not in snapshot or _capability_ok(snapshot.get(m))
            ]

        return _http_json_response({"suggested": suggested, "models": catalog})

    def _handle_model_capabilities(self, request: WsRequest) -> Response:
        """`GET /api/model/capabilities?model=&provider=` — what a model supports."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        query = _parse_query(request.path)
        model = (_query_first(query, "model") or "").strip()
        provider = (_query_first(query, "provider") or "").strip() or None
        if not model:
            return _http_error(400, "model is required")
        try:
            from durin.providers.capabilities import get_model_capabilities

            caps = get_model_capabilities(model, provider)
        except Exception as e:  # noqa: BLE001
            return _http_error(400, f"could not resolve capabilities: {e}")
        return _http_json_response(
            {
                "model": model,
                "max_input_tokens": getattr(caps, "max_input_tokens", None),
                "supports_vision": bool(getattr(caps, "supports_vision", False)),
                "supports_audio_input": bool(getattr(caps, "supports_audio_input", False)),
                "supports_function_calling": bool(
                    getattr(caps, "supports_function_calling", False)
                ),
            }
        )

    def _handle_channels_list(self, request: WsRequest) -> Response:
        """`GET /api/channels` — discovered channels + enabled state.

        Lets the dashboard's curated Channels section enable a channel
        and know which credential field it needs — config the generic
        `/api/config` form can't create from scratch.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.channels.registry import discover_all
        from durin.config.loader import load_config

        # First match wins — channels expose exactly one primary credential.
        cred_fields = (
            "token", "bot_token", "app_token", "appId", "app_id",
            "api_key", "claw_token", "access_token",
        )
        config = load_config()
        extra = getattr(config.channels, "__pydantic_extra__", None) or {}
        items: list[dict[str, Any]] = []
        for name, cls in sorted(discover_all().items()):
            section = extra.get(name)
            enabled = (
                bool(section.get("enabled")) if isinstance(section, dict) else False
            )
            credential_field = None
            try:
                defaults = cls.default_config() if hasattr(cls, "default_config") else {}
                credential_field = next(
                    (f for f in cred_fields if f in defaults), None
                )
            except Exception:  # noqa: BLE001
                credential_field = None
            items.append(
                {
                    "name": name,
                    "display_name": getattr(cls, "display_name", name),
                    "enabled": enabled,
                    "credential_field": credential_field,
                }
            )
        return _http_json_response({"channels": items})

    async def _handle_model_test(self, request: WsRequest) -> Response:
        """`GET /api/model/test` — a real round-trip to a model.

        Tests the given ``model``/``provider`` (defaults to the
        configured ones) so the dashboard can verify a pick before the
        user commits it. Async so the ping doesn't block the gateway
        event loop.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.cli.doctor import check_model_ping_async
        from durin.config.loader import load_config

        query = _parse_query(request.path)
        model = (_query_first(query, "model") or "").strip()
        provider = (_query_first(query, "provider") or "").strip()
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001
            return _http_error(400, f"could not load config: {e}")
        if model:
            cfg.agents.defaults.model = model
            cfg.agents.defaults.model_preset = None  # honor the override
        if provider:
            cfg.agents.defaults.provider = provider
        result = await check_model_ping_async(cfg=cfg)
        return _http_json_response(
            {
                "status": result.status,
                "message": result.message,
                "fix": result.fix or "",
            }
        )

    async def _handle_cross_encoder_test(
        self, request: WsRequest, query: dict,
    ) -> Response:
        """`GET /api/memory/cross-encoder/test?model=<id>` — probe a
        cross-encoder model id by loading + running a trivial score.

        Audit B12 (2026-05-28). Replaces the previously-considered
        hardcoded enum: any model id that the user wants to evaluate
        can be tested live, with the result surfaced to the webui
        before the value is committed to config.

        The load is potentially slow (model download + warmup). Run
        it in a thread so the gateway event loop stays responsive.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        import asyncio

        from durin.memory.cross_encoder import probe_model

        model = (_query_first(query, "model") or "").strip()
        if not model:
            return _http_error(400, "missing required `model` query param")
        try:
            result = await asyncio.to_thread(probe_model, model)
        except Exception as exc:  # noqa: BLE001
            return _http_json_response(
                {
                    "status": "fail",
                    "message": f"unexpected error: {type(exc).__name__}: {exc}",
                    "model_id": model,
                    "duration_ms": 0.0,
                },
            )
        return _http_json_response(result)

    @staticmethod
    def _is_websocket_channel_session_key(key: str) -> bool:
        """True when *key* is a ``websocket:…`` session exposed on this HTTP surface."""
        return key.startswith("websocket:")

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        # Only ``websocket:…`` sessions are listed/served here — same boundary as
        # ``/api/sessions``. Block handcrafted URLs from probing CLI / Slack / etc.
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self._session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        # Decorate persisted user messages with signed media URLs so the
        # client can render previews. The raw on-disk ``media`` paths are
        # stripped on the way out — they leak server filesystem layout and
        # the client never needs them once it has the signed fetch URL.
        self._augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = build_webui_thread_response(
            decoded_key,
            augment_user_media=self._augment_transcript_user_media,
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        return _http_json_response(data)

    def _try_append_webui_transcript(self, chat_id: str, wire: dict[str, Any]) -> None:
        sk = f"websocket:{chat_id}"
        try:
            dup = json.loads(json.dumps(wire, ensure_ascii=False))
            append_transcript_object(sk, dup)
        except (ValueError, TypeError) as e:
            self.logger.warning("webui transcript append failed: {}", e)

    def _augment_transcript_user_media(self, paths: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pstr in paths:
            path = Path(pstr)
            att = self._sign_or_stage_media_path(path)
            if att is None:
                continue
            mime, _ = mimetypes.guess_type(path.name)
            kind = "video" if mime and mime.startswith("video/") else "image"
            out.append(
                {"kind": kind, "url": att["url"], "name": att.get("name", path.name)},
            )
        return out

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        meta = metadata or {}
        if meta.get("webui"):
            user_obj: dict[str, Any] = {
                "event": "user",
                "chat_id": chat_id,
                "text": content,
            }
            if media:
                user_obj["media_paths"] = list(media)
            self._try_append_webui_transcript(chat_id, user_obj)
        await super()._handle_message(
            sender_id,
            chat_id,
            content,
            media,
            metadata,
            session_key,
            is_dm,
        )

    def _augment_media_urls(self, payload: dict[str, Any]) -> None:
        """Mutate *payload* in place: each message's ``media`` path list is
        replaced by a parallel ``media_urls`` list of signed fetch URLs.

        Messages without media or with non-string path entries are left
        untouched. Paths that no longer live inside ``media_dir`` (e.g. the
        file was deleted, or the dir was relocated) are silently skipped;
        the client falls back to the historical-replay placeholder tile.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            media = msg.get("media")
            if not isinstance(media, list) or not media:
                continue
            urls: list[dict[str, str]] = []
            for entry in media:
                if not isinstance(entry, str) or not entry:
                    continue
                signed = self._sign_media_path(Path(entry))
                if signed is None:
                    continue
                urls.append({"url": signed, "name": Path(entry).name})
            if urls:
                msg["media_urls"] = urls
            # Always drop the raw paths from the wire payload.
            msg.pop("media", None)

    def _sign_media_path(self, abs_path: Path) -> str | None:
        """Return a ``/api/media/<sig>/<payload>`` URL for *abs_path*, or
        ``None`` when the path does not resolve inside the media root.

        The URL is self-authenticating: the signature binds the payload to
        this process's ``_media_secret``, so only paths we chose to sign can
        be fetched. The returned path is relative to the server origin; the
        client joins it against this server's HTTP origin (same host as WS).
        """
        try:
            media_root = get_media_dir().resolve()
            rel = abs_path.resolve().relative_to(media_root)
        except (OSError, ValueError):
            return None
        payload = _b64url_encode(rel.as_posix().encode("utf-8"))
        mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        return f"/api/media/{_b64url_encode(mac)}/{payload}"

    def _sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        """Return a signed media URL payload for *path*.

        Persisted inbound media already lives under ``get_media_dir`` and can
        be signed directly. Outbound bot-generated files may live anywhere on
        disk; copy those into the websocket media bucket first so the browser
        can fetch them through the existing signed media route without
        exposing arbitrary filesystem paths.
        """
        signed = self._sign_media_path(path)
        if signed is not None:
            return {"url": signed, "name": path.name}
        try:
            if not path.is_file():
                return None
            media_dir = get_media_dir("websocket")
            safe_name = safe_filename(path.name) or "attachment"
            staged = media_dir / f"{uuid.uuid4().hex[:12]}-{safe_name}"
            shutil.copyfile(path, staged)
        except OSError as exc:
            self.logger.warning("failed to stage outbound media {}: {}", path, exc)
            return None
        signed = self._sign_media_path(staged)
        if signed is None:
            return None
        return {"url": signed, "name": path.name}

    def _handle_media_fetch(self, sig: str, payload: str) -> Response:
        """Serve a single media file previously signed via
        :meth:`_sign_media_path`. Validates the signature, decodes the
        payload to a relative path, and streams the file bytes with a
        long-lived immutable cache header (the URL already encodes the
        file identity, so caches can be aggressive)."""
        try:
            provided_mac = _b64url_decode(sig)
        except (ValueError, binascii.Error):
            return _http_error(401, "invalid signature")
        expected_mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(expected_mac, provided_mac):
            return _http_error(401, "invalid signature")
        try:
            rel_bytes = _b64url_decode(payload)
            rel_str = rel_bytes.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return _http_error(400, "invalid payload")
        # An attacker who somehow bypassed the HMAC check would still need
        # the resolved path to escape the media root; guard defensively.
        try:
            media_root = get_media_dir().resolve()
            candidate = (media_root / rel_str).resolve()
            candidate.relative_to(media_root)
        except (OSError, ValueError):
            return _http_error(404, "not found")
        if not candidate.is_file():
            return _http_error(404, "not found")
        try:
            body = candidate.read_bytes()
        except OSError:
            return _http_error(500, "read error")
        mime, _ = mimetypes.guess_type(candidate.name)
        if mime not in _MEDIA_ALLOWED_MIMES:
            mime = "application/octet-stream"
        return _http_response(
            body,
            content_type=mime,
            extra_headers=[
                ("Cache-Control", "private, max-age=31536000, immutable"),
                # Paired with the MIME whitelist above: prevents browsers from
                # MIME-sniffing an octet-stream fallback into executable HTML.
                ("X-Content-Type-Options", "nosniff"),
            ],
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        # Same boundary as ``_handle_session_messages``: mutations apply only to
        # websocket-channel sessions; deletion unlinks local JSONL — keep scope narrow.
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        deleted = self._session_manager.delete_session(decoded_key)
        delete_webui_thread(decoded_key)
        return _http_json_response({"deleted": bool(deleted)})

    def _handle_session_rename(self, request: WsRequest, key: str) -> Response:
        """P2 (doc 20): set a user-edited title for a webui session.

        Pulls ``?title=...`` from the query string, persists it into the
        session metadata via the same fields :func:`maybe_generate_webui_title`
        uses (``title`` + ``title_user_edited``). Marking
        ``title_user_edited`` blocks future auto-regeneration so the
        user's choice sticks. Trims + caps at the same length the LLM
        title generator does to avoid one path producing values the
        other refuses to display.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")

        from durin.utils.webui_titles import (
            TITLE_MAX_CHARS,
            WEBUI_TITLE_METADATA_KEY,
            WEBUI_TITLE_USER_EDITED_METADATA_KEY,
            clean_generated_title,
        )

        query = _parse_query(request.path)
        raw_title = _query_first(query, "title") or ""
        title = clean_generated_title(raw_title)
        if not title:
            return _http_error(400, "title is required")
        if len(title) > TITLE_MAX_CHARS:
            title = title[: TITLE_MAX_CHARS - 1].rstrip() + "…"

        if not self._session_manager.exists(decoded_key):
            return _http_error(404, "session not found")
        # Mutate the cached instance (not a fresh `_load` snapshot) so an
        # in-flight turn holding the same session sees the title and its
        # end-of-turn save does not clobber it — and vice versa (B2).
        session = self._session_manager.get_or_create(decoded_key)
        session.metadata[WEBUI_TITLE_METADATA_KEY] = title
        session.metadata[WEBUI_TITLE_USER_EDITED_METADATA_KEY] = True
        self._session_manager.save(session)
        return _http_json_response({"title": title})

    def _serve_static(self, request_path: str) -> Response | None:
        """Resolve *request_path* against the built SPA directory; SPA fallback to index.html."""
        assert self._static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        # Reject path-traversal attempts and absolute targets.
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self._static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self._static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            # SPA history-mode fallback: unknown routes serve index.html so the
            # client-side router can render them.
            index = self._static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self.logger.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        # Hash-named build assets are cache-friendly; index.html must stay fresh.
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._take_issued_token_if_valid(supplied)
        return None

    async def start(self) -> None:
        from durin.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")

        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(
            connection: ServerConnection,
            request: WsRequest,
        ) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}://{}:{}{}",
            scheme,
            self.config.host,
            self.config.port,
            self.config.path,
        )
        if self.config.token_issue_path:
            self.logger.info(
                "WebSocket token issue route: {}://{}:{}{}",
                scheme,
                self.config.host,
                self.config.port,
                _normalize_config_path(self.config.token_issue_path),
            )

        async def runner() -> None:
            async with serve(
                handler,
                self.config.host,
                self.config.port,
                process_request=process_request,
                max_size=self.config.max_message_bytes,
                ping_interval=self.config.ping_interval_s,
                ping_timeout=self.config.ping_timeout_s,
                ssl=ssl_context,
            ):
                assert self._stop_event is not None
                await self._stop_event.wait()

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = str(uuid.uuid4())

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                    },
                    ensure_ascii=False,
                )
            )
            # Register only after ready is successfully sent to avoid out-of-order sends
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                # WebSocket already authenticates at handshake time (token),
                # so pairing is not applicable. Treat as non-DM to avoid
                # sending pairing codes to an already-authenticated client.
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": getattr(connection, "remote_address", None)},
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            self._cleanup_connection(connection)

    def _save_envelope_media(
        self,
        media: list[Any],
    ) -> tuple[list[str], str | None]:
        """Decode and persist ``media`` items from a ``message`` envelope.

        Returns ``(paths, None)`` on success or ``([], reason)`` on the first
        failure — the caller is expected to surface ``reason`` to the client
        and skip publishing so no half-formed message ever reaches the agent.
        On failure, any files already written to disk earlier in the same
        call are unlinked so partial ingress doesn't leak orphan files.
        ``reason`` is a short, stable token suitable for UI localization.

        Shape: ``list[{"data_url": str, "name"?: str | None}]``.
        """
        image_count = 0
        video_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "failed to unlink partial media {}: {}", p, exc
                    )
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            max_bytes = _MAX_VIDEO_BYTES if is_video else _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(
                    data_url, media_dir, max_bytes=max_bytes,
                )
            except FileSizeExceeded:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Route one typed inbound envelope (``new_chat`` / ``attach`` / ``message``)."""
        t = envelope.get("type")
        if t == "new_chat":
            new_id = str(uuid.uuid4())
            self._attach(connection, new_id)
            await self._send_event(connection, "attached", chat_id=new_id)
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason="malformed",
                    )
                    return
                media_paths, reason = self._save_envelope_media(raw_media)
                if reason is not None:
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason=reason,
                    )
                    return

            # Allow image-only turns (content may be empty when media is attached).
            if not content.strip() and not media_paths:
                await self._send_event(connection, "error", detail="missing content")
                return

            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if envelope.get("webui") is True:
                metadata["webui"] = True
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        if t == "secret_store":
            await self._handle_secret_store_envelope(connection, client_id, envelope)
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def _handle_secret_store_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Write a credential to the secret store from a ``secret_store`` frame.

        The value rides the JSON frame — never a URL query — and is never
        placed into the agent conversation. On success the agent on
        ``chat_id`` is told the secret exists (metadata only, no value)
        so it can resume.
        """
        from durin.security.secrets import (
            SecretStore,
            get_secret_store,
            is_valid_secret_name,
        )

        request_id = str(envelope.get("request_id") or "")

        async def _fail(detail: str) -> None:
            await self._send_event(
                connection, "secret_stored", request_id=request_id, ok=False, detail=detail
            )

        name = str(envelope.get("name") or "").strip()
        service = str(envelope.get("service") or "").strip()
        if not is_valid_secret_name(name):
            await _fail("invalid secret name (use UPPER_SNAKE)")
            return
        if not service:
            await _fail("service is required")
            return

        raw_scope = envelope.get("scope")
        scope = (
            [str(s).strip() for s in raw_scope if str(s).strip()]
            if isinstance(raw_scope, list)
            else []
        )
        try:
            store = SecretStore().load()
            existing = store.get(name)
            # An empty value on an existing secret is a metadata-only
            # edit — keep the stored credential, change scope/etc.
            value = envelope.get("value")
            if not isinstance(value, str) or not value:
                if existing is None:
                    await _fail("value is required for a new secret")
                    return
                value = existing.value
            store.put(
                name,
                value=value,
                service=service,
                account=(str(envelope.get("account") or "").strip() or None),
                description=str(envelope.get("description") or "").strip(),
                scope=scope,
                origin=existing.origin if existing else "webui",
            )
            store.save()
            get_secret_store(reload=True)
        except Exception as exc:  # noqa: BLE001
            await _fail(f"could not store secret: {exc}")
            return

        await self._send_event(
            connection, "secret_stored", request_id=request_id, ok=True, name=name
        )

        # Tell the agent — metadata only, never the value — so it resumes.
        cid = envelope.get("chat_id")
        if _is_valid_chat_id(cid):
            scope_label = ", ".join(scope) or "none"
            usable = (
                f" It is available to your shell commands as ${name}."
                if "exec" in scope
                else ""
            )
            note = (
                f"The user stored the secret '{name}' (service={service}, "
                f"scope={scope_label}).{usable} Please continue the task."
            )
            self._attach(connection, cid)
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=note,
                metadata={"webui": True},
                is_dm=False,
            )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._issued_tokens.clear()
        self._api_tokens.clear()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        if msg.metadata.get("_runtime_model_updated"):
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return

        # Provider retry heartbeat — surface as a dedicated WS event so the
        # UI can render a transient banner ("retrying in 4s, attempt 2 of 7")
        # instead of letting the error become the assistant's reply.
        if msg.metadata.get("_retry_wait"):
            status = msg.metadata.get("retry_status") or {}
            await self.send_api_status(
                msg.chat_id, status, message=msg.content,
            )
            return

        # Snapshot the subscriber set so ConnectionClosed cleanups mid-iteration are safe.
        conns = list(self._subs.get(msg.chat_id, ()))
        if not conns:
            if (
                msg.metadata.get("_progress")
                or msg.metadata.get("_turn_end")
                or msg.metadata.get("_session_updated")
                or msg.metadata.get("_goal_status")
                or msg.metadata.get("_goal_state_sync")
            ):
                self.logger.debug("no active subscribers for chat_id={}", msg.chat_id)
            else:
                self.logger.warning("no active subscribers for chat_id={}", msg.chat_id)
            return
        if msg.metadata.get("_goal_state_sync"):
            blob = msg.metadata.get("goal_state")
            await self.send_goal_state(msg.chat_id, blob if isinstance(blob, dict) else {"active": False})
            return
        if msg.metadata.get("_goal_status"):
            status = msg.metadata.get("goal_status")
            if status in ("running", "idle"):
                started_raw = msg.metadata.get("started_at", msg.metadata.get("goal_started_at"))
                await self.send_goal_status(
                    msg.chat_id,
                    status,
                    started_at=float(started_raw) if isinstance(started_raw, int | float) else None,
                )
            return
        # Signal that the agent has fully finished processing the current turn.
        if msg.metadata.get("_turn_end"):
            lat = msg.metadata.get("latency_ms")
            lat_i = int(lat) if isinstance(lat, (int, float)) else None
            gs = msg.metadata.get("goal_state")
            gs_blob = gs if isinstance(gs, dict) else None
            await self.send_turn_end(msg.chat_id, latency_ms=lat_i, goal_state=gs_blob)
            return
        if msg.metadata.get("_session_updated"):
            await self.send_session_updated(msg.chat_id)
            return
        text = msg.content
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": text,
        }
        # `render_as: "text"` is set by command handlers (e.g. /status,
        # /memory show, /sessions) that produce pre-formatted plain
        # text with explicit newlines. Without this hint, the WebUI
        # would feed the content to its Markdown renderer which collapses
        # single newlines into spaces and drops the table-like layout.
        render_as = msg.metadata.get("render_as")
        if render_as in ("text", "markdown"):
            payload["render_as"] = render_as
        if msg.media:
            payload["media"] = msg.media
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                signed = self._sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        lat = msg.metadata.get("latency_ms")
        if isinstance(lat, (int, float)):
            payload["latency_ms"] = int(lat)
        if msg.metadata.get("_tool_events"):
            payload["tool_events"] = msg.metadata["_tool_events"]
        agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
        if agent_ui is not None:
            payload["agent_ui"] = agent_ui
        # Mark intermediate agent breadcrumbs (tool-call hints, generic
        # progress strings) so WS clients can render them as subordinate
        # trace rows rather than conversational replies.
        if msg.metadata.get("_tool_hint"):
            payload["kind"] = "tool_hint"
        elif msg.metadata.get("_progress"):
            payload["kind"] = "progress"
        self._try_append_webui_transcript(msg.chat_id, payload)
        raw = json.dumps(payload, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Push one chunk of model reasoning. Mirrors ``send_delta`` shape so
        clients receive a stream that opens, updates in place, and closes —
        rendered above the active assistant bubble with a shimmer header
        until the matching ``reasoning_end`` arrives.
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns or not delta:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_delta",
            "chat_id": chat_id,
            "text": delta,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Close the current reasoning stream segment for in-place renderers."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning_end ")

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        meta = metadata or {}
        if meta.get("_stream_end"):
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
    ) -> None:
        """Signal that the agent has fully finished processing the current turn."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "turn_end", "chat_id": chat_id}
        if latency_ms is not None:
            body["latency_ms"] = int(latency_ms)
        if goal_state is not None:
            body["goal_state"] = goal_state
        self._try_append_webui_transcript(chat_id, body)
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_end ")

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """Push persisted goal-state snapshot for *chat_id* (multi-chat isolation)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body = {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_state ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """Notify subscribed clients that a turn started or finished (wall-clock hint)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "goal_status",
            "chat_id": chat_id,
            "status": status,
        }
        if status == "running" and started_at is not None:
            body["started_at"] = started_at
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_status ")

    async def send_api_status(
        self,
        chat_id: str,
        status: dict[str, Any],
        *,
        message: str | None = None,
    ) -> None:
        """Push a transient API status (retrying, giving up) for *chat_id*.

        The frontend renders this as a banner; it never enters the
        chat transcript. ``status`` carries ``kind`` (retry_wait /
        giving_up / exhausted_persistent), ``attempt``,
        ``max_attempts``, ``delay_s``, ``persistent``, ``final``.
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "api_status",
            "chat_id": chat_id,
            "status": status,
        }
        if message:
            body["message"] = message
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" api_status ")

    async def send_session_updated(self, chat_id: str) -> None:
        """Notify clients that session metadata changed outside the main turn."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "session_updated", "chat_id": chat_id}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" session_updated ")

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """Broadcast runtime model changes to every open websocket connection."""
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {
            "event": "runtime_model_updated",
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" runtime_model_updated ")
