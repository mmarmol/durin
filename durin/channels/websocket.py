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
from durin.providers.codex_device_auth import (
    disconnect as codex_disconnect,
)
from durin.providers.codex_device_auth import (
    existing_codex_session,
    request_device_code,
    start_loopback_login,
)
from durin.providers.codex_device_auth import (
    poll_once as codex_poll_once,
)
from durin.service import DomainError, Principal, ServiceRegistry
from durin.session.goal_state import goal_state_ws_blob
from durin.utils.helpers import safe_filename
from durin.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from durin.utils.webui_transcript import append_transcript_object, build_webui_thread_response
from durin.utils.webui_turn_helpers import websocket_turn_wall_started_at

if TYPE_CHECKING:
    from durin.cron.service import CronService
    from durin.session.manager import SessionManager


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def _humanize_interval_ms(every_ms: int) -> str:
    """Render an ``every`` interval with the largest whole unit instead of raw
    seconds, so a cron row reads ``every 2h`` rather than ``every 7200s``."""
    secs = every_ms // 1000
    if secs <= 0:
        return "0s"
    for unit_secs, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs % unit_secs == 0:
            return f"{secs // unit_secs}{suffix}"
    return f"{secs}s"


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


_DOMAIN_ERROR_STATUS: dict[str, int] = {
    "unauthenticated": 401,
    "forbidden": 403,
    "not_found": 404,
    "conflict": 409,
    "validation_failed": 400,
    "unavailable": 503,
    "error": 500,
}


def _domain_error_response(err: DomainError) -> Response:
    """Map a service-layer ``DomainError`` to the HTTP response the legacy
    handlers returned — a plain-text body with the matching status."""
    status = _DOMAIN_ERROR_STATUS.get(err.code, 500)
    return _http_error(status, err.message)


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


def _request_host_is_local(request: WsRequest) -> bool:
    """True when the browser reached the webui via localhost.

    Used to decide whether the loopback OAuth flow (callback on localhost:1455,
    served by this gateway) can reach the user's browser — only when both run on
    the same machine, i.e. the dashboard was opened at localhost/127.0.0.1/::1.
    """
    try:
        host = request.headers.get("Host") or request.headers.get("host") or ""
    except Exception:  # noqa: BLE001
        host = ""
    host = host.strip()
    if host.startswith("["):  # bracketed IPv6, e.g. [::1]:8765
        host = host[1:].split("]", 1)[0]
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host in ("localhost", "127.0.0.1", "::1")


def _logs_query_from_params(query: dict[str, list[str]]):
    """Build a :class:`durin.logs.reader.LogQuery` from URL query params."""
    from durin.logs.reader import LogQuery

    def first(name: str) -> str | None:
        vals = query.get(name)
        return vals[0] if vals else None

    source = (first("source") or "gateway").strip()
    filters: dict[str, set[str]] = {}
    for key in ("level", "channel", "session", "type"):
        vals = query.get(key)
        if vals:
            collected: set[str] = set()
            for v in vals:  # repeated params OR comma-joined both supported
                collected.update(part for part in v.split(",") if part)
            if collected:
                filters[key] = collected
    before_ts = first("before_ts")
    window = first("window_hours")
    limit = first("limit")
    return LogQuery(
        source="telemetry" if source == "telemetry" else "gateway",
        q=(first("q") or None),
        before_ts=float(before_ts) if before_ts else None,
        window_hours=(None if window == "all" else float(window) if window else 24.0),
        limit=max(1, min(int(limit), 1000)) if limit else 200,
        filters=filters,
    )


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
        cron_service: "CronService | None" = None,
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
        # Running CronService (same process). Used only by the run-now
        # endpoint so a manual trigger reaches the live scheduler + its
        # in-process overlap guard. None outside the gateway (tests).
        self._cron_service = cron_service
        # Strong refs to fire-and-forget run-now tasks (else GC'd mid-run).
        self._background_run_tasks: set[asyncio.Task] = set()
        # Process-local secret used to HMAC-sign media URLs. The signed URL is
        # the capability — anyone who holds a valid URL can fetch that one
        # file, nothing else. The secret regenerates on restart so links
        # become self-expiring (callers just refresh the session list).
        self._media_secret: bytes = secrets.token_bytes(32)
        # Service-core registry: domain logic extracted out of this channel
        # (SP1). The HTTP handlers are now thin shims that call these services.
        # Built here so the deps are available; services are registered as each
        # domain is extracted.
        self._services: ServiceRegistry = self._build_services(bus)

    def _build_services(self, bus: MessageBus) -> ServiceRegistry:
        """Construct the registry holding the extracted domain services."""
        from durin.service.config import ConfigService
        from durin.service.cron import CronService
        from durin.service.secrets import SecretsService
        from durin.service.sessions import SessionsService
        from durin.service.settings import SettingsService
        from durin.service.skills import SkillsService

        registry = ServiceRegistry(
            config=self.config,
            session_manager=self._session_manager,
            cron_service=self._cron_service,
            bus=bus,
        )
        registry.register("secrets", SecretsService())
        registry.register("cron", CronService(cron_scheduler=self._cron_service))
        registry.register("sessions", SessionsService(session_manager=self._session_manager))
        registry.register("settings", SettingsService())
        registry.register("config", ConfigService())
        registry.register("skills", SkillsService(workspace=self._endpoint_workspace()))
        from durin.service.memory import MemoryService
        registry.register("memory", MemoryService(workspace_resolver=self._endpoint_workspace))
        return registry

    def _endpoint_workspace(self) -> Path:
        """The gateway's ACTUAL workspace for read-only memory endpoints.

        The ``--workspace`` flag sets the workspace on the session manager;
        ``load_config()`` would re-read the config file's ``workspace_path`` and
        ignore that override, so the memory graph/search endpoints must resolve
        the workspace from the session manager when present.
        """
        if self._session_manager is not None:
            return self._session_manager.workspace
        from durin.config.loader import load_config
        return load_config().workspace_path

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
            return await self._handle_sessions_list(request)

        if got == "/api/settings":
            return await self._handle_settings(request)

        if got == "/api/commands":
            return self._handle_commands(request)

        if got == "/api/settings/update":
            return await self._handle_settings_update(request)

        if got == "/api/settings/provider/update":
            return await self._handle_settings_provider_update(request)

        if got == "/api/settings/web-search/update":
            return await self._handle_settings_web_search_update(request)

        if got == "/api/oauth/codex/status":
            return self._handle_codex_oauth_status(request)

        if got == "/api/oauth/codex/start":
            return self._handle_codex_oauth_start(request)

        if got == "/api/oauth/codex/start-loopback":
            return self._handle_codex_oauth_start_loopback(request)

        if got == "/api/oauth/codex/poll":
            return self._handle_codex_oauth_poll(request)

        if got == "/api/oauth/codex/disconnect":
            return self._handle_codex_oauth_disconnect(request)

        if got == "/api/secrets":
            return await self._handle_secrets_list(request)

        if got == "/api/secrets/delete":
            return await self._handle_secret_delete(request)

        if got == "/api/cron":
            return await self._handle_cron_list(request)

        if got == "/api/cron/remove":
            return await self._handle_cron_remove(request)

        if got == "/api/cron/toggle":
            return await self._handle_cron_toggle(request)

        if got == "/api/cron/run":
            return await self._handle_cron_run(request)

        if got == "/api/config":
            return await self._handle_config_get(request)

        if got == "/api/config/set":
            return await self._handle_config_set(request)

        if got == "/api/logs":
            return self._handle_logs_list(request)

        if got == "/api/skills":
            return await self._handle_skills_list(request)

        # Exact matches BEFORE the `([^/]+)` patterns so "quarantine"/"resolve"/
        # "import" are not captured as skill names by `^/api/skills/([^/]+)$`.
        if got == "/api/skills/quarantine":
            return await self._handle_skills_quarantine(request)

        if got == "/api/skills/resolve":
            return await self._handle_skills_resolve(request)

        if got == "/api/skills/import":
            return await self._handle_skills_import(request)

        if got == "/api/skills/github-token-test":
            return await self._handle_skills_github_token_test(request)

        if got == "/api/skills/search":
            return await self._handle_skill_search(request)

        if got == "/api/skills/describe":
            return await self._handle_skill_describe(request)

        m = re.match(r"^/api/skills/([^/]+)/save$", got)
        if m:
            return await self._handle_skill_save(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/mode$", got)
        if m:
            return await self._handle_skill_mode(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/approve$", got)
        if m:
            return await self._handle_skill_approve(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/reject$", got)
        if m:
            return await self._handle_skill_reject(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/judge$", got)
        if m:
            return await self._handle_skill_judge(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/remove$", got)
        if m:
            return await self._handle_skill_remove(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/files$", got)
        if m:
            return await self._handle_skill_files(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/file/save$", got)
        if m:
            return await self._handle_skill_file_save(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/file$", got)
        if m:
            return await self._handle_skill_file(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/history$", got)
        if m:
            return await self._handle_skill_history(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)/install-deps$", got)
        if m:
            return await self._handle_skill_install_deps(request, m.group(1))

        m = re.match(r"^/api/skills/([^/]+)$", got)
        if m:
            return await self._handle_skill_get(request, m.group(1))

        if got == "/api/channels":
            return await self._handle_channels_list(request)

        if got == "/api/models":
            return await self._handle_models_list(request)

        if got == "/api/memory/graph":
            return await self._handle_memory_graph(request)

        if got == "/api/memory/subgraph":
            return await self._handle_memory_subgraph(request, query)

        if got == "/api/memory/search":
            return await self._handle_memory_search_api(request, query)

        m = re.match(r"^/api/memory/entity/(.+)$", got)
        if m:
            return await self._handle_memory_entity(request, m.group(1))

        m = re.match(r"^/api/memory/session/(.+)$", got)
        if m:
            return await self._handle_memory_session(request, m.group(1))

        m = re.match(r"^/api/memory/edge/([^/]+)/([^/]+)$", got)
        if m:
            return await self._handle_memory_edge(request, m.group(1), m.group(2))

        if got == "/api/memory/entry":
            return await self._handle_memory_entry(request, query)

        if got == "/api/memory/forget":
            return await self._handle_memory_forget(request, query)

        if got == "/api/memory/backlinks":
            return await self._handle_memory_backlinks(request, query)

        if got == "/api/model/test":
            return await self._handle_model_test(request)

        if got == "/api/model/capabilities":
            return await self._handle_model_capabilities(request)

        if got == "/api/memory/cross-encoder/test":
            return await self._handle_cross_encoder_test(request, query)

        if got == "/api/extras/status":
            return self._handle_extras_status(request, query)
        if got == "/api/extras/ensure":
            return await self._handle_extras_ensure(request, query)
        if got == "/api/extras/restart":
            return self._handle_extras_restart(request)

        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return await self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return await self._handle_webui_thread_get(request, m.group(1))

        # NOTE: websockets' HTTP parser only accepts GET, so we cannot expose a
        # true ``DELETE`` verb. The action is folded into the path instead.
        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return await self._handle_session_delete(request, m.group(1))

        # P2 (doc 20): user-driven rename. Same constraint as delete —
        # GET-only HTTP, action folded into the path. New title arrives
        # as a ``title`` query param (URL-encoded).
        m = re.match(r"^/api/sessions/([^/]+)/rename$", got)
        if m:
            return await self._handle_session_rename(request, m.group(1))

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

    async def _handle_memory_graph(self, request: WsRequest) -> Response:
        """GET /api/memory/graph — entity-centric memory as nodes + edges."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.memory import MemoryGraphQuery

        try:
            result = await self._services.get("memory").graph(
                MemoryGraphQuery(), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data)

    async def _handle_memory_subgraph(
        self, request: WsRequest, query: dict[str, list[str]]
    ) -> Response:
        """GET /api/memory/subgraph?ref=<type:slug>&hops=N — ego-graph."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        ref = _query_first(query, "ref") or ""
        if ":" not in ref:
            return _http_error(400, "ref must be '<type>:<slug>'")
        try:
            hops = int(_query_first(query, "hops") or "1")
        except ValueError:
            hops = 1
        from durin.service.memory import MemorySubgraphQuery

        try:
            result = await self._services.get("memory").subgraph(
                MemorySubgraphQuery(ref=ref, hops=hops), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data)

    async def _handle_memory_entity(self, request: WsRequest, ref_encoded: str) -> Response:
        """GET /api/memory/entity/<ref> — full page + history + archive + entries."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        from durin.service.memory import MemoryEntityQuery

        ref = unquote(ref_encoded)
        try:
            result = await self._services.get("memory").entity(
                MemoryEntityQuery(ref=ref), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data)

    async def _handle_memory_session(
        self, request: WsRequest, stem_encoded: str,
    ) -> Response:
        """GET /api/memory/session/<stem> — session detail for the graph view."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        from durin.service.memory import MemorySessionQuery

        stem = unquote(stem_encoded)
        try:
            result = await self._services.get("memory").session(
                MemorySessionQuery(stem=stem), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data)

    async def _handle_memory_entry(
        self, request: WsRequest, query: dict[str, list[str]],
    ) -> Response:
        """GET /api/memory/entry?uri=memory/<class>/<id> — one entry's frontmatter + body."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.memory import MemoryEntryQuery

        uri = (_query_first(query, "uri") or "").strip()
        try:
            result = await self._services.get("memory").entry(
                MemoryEntryQuery(uri=uri), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data)

    async def _handle_memory_forget(
        self, request: WsRequest, query: dict[str, list[str]],
    ) -> Response:
        """GET /api/memory/forget?uri=memory/<class>/<id> — archive an entry.

        Returns ``{"result": "archived"|"not_found"|"protected"|"invalid"}``.
        HTTP status mirrors the result: 200 archived/not_found,
        403 protected, 400 invalid.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.memory import MemoryForgetCommand

        uri = (_query_first(query, "uri") or "").strip()
        try:
            result = await self._services.get("memory").forget(
                MemoryForgetCommand(uri=uri), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        payload = {"result": result.result}
        if result.result == "protected":
            return _http_json_response(payload, status=403)
        if result.result == "invalid":
            return _http_json_response(payload, status=400)
        return _http_json_response(payload)

    async def _handle_memory_backlinks(
        self, request: WsRequest, query: dict[str, list[str]],
    ) -> Response:
        """GET /api/memory/backlinks?uri=memory/<class>/<id> — entries that reference this one."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.memory import MemoryBacklinksQuery

        uri = (_query_first(query, "uri") or "").strip()
        try:
            result = await self._services.get("memory").backlinks(
                MemoryBacklinksQuery(uri=uri), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data)

    async def _handle_memory_edge(
        self, request: WsRequest, source_enc: str, target_enc: str,
    ) -> Response:
        """GET /api/memory/edge/<source>/<target> — entries co-mentioning both."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from urllib.parse import unquote

        from durin.service.memory import MemoryEdgeQuery

        a = unquote(source_enc)
        b = unquote(target_enc)
        try:
            result = await self._services.get("memory").edge(
                MemoryEdgeQuery(a=a, b=b), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data)

    async def _handle_memory_search_api(
        self, request: WsRequest, query: dict[str, list[str]],
    ) -> Response:
        """GET /api/memory/search?q=…&scope=…&level=… — same shape as memory_search tool."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.memory import MemorySearchQuery

        q = _query_first(query, "q") or ""
        scope = _query_first(query, "scope") or "all"
        level = _query_first(query, "level") or "warm"
        kinds = _query_first(query, "kinds") or "all"
        try:
            result = await self._services.get("memory").search(
                MemorySearchQuery(q=q, scope=scope, level=level, kinds=kinds),
                Principal.local(),
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data)

    async def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.sessions import SessionsListQuery

        try:
            result = await self._services.get("sessions").list(
                SessionsListQuery(), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response({"sessions": result.sessions})

    def _settings_payload(self, *, requires_restart: bool = False) -> dict[str, Any]:
        """Thin wrapper — delegates to ``SettingsService._payload`` (moved in SP1)."""
        return self._services.get("settings")._payload(requires_restart=requires_restart).model_dump()

    async def _handle_settings(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.settings import SettingsQuery

        try:
            result = await self._services.get("settings").get(
                SettingsQuery(), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

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
        payload = self._codex_status_payload()
        # Loopback (no device-auth toggle) only works when the browser is on
        # the gateway machine — i.e. the webui was reached via localhost.
        payload["can_loopback"] = _request_host_is_local(request)
        return _http_json_response(payload)

    def _handle_codex_oauth_start_loopback(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if not _request_host_is_local(request):
            return _http_error(400, "loopback unavailable on a remote gateway; use device code")
        try:
            url = start_loopback_login()
        except Exception as exc:  # noqa: BLE001
            return _http_error(502, f"loopback login failed to start: {exc}")
        return _http_json_response({"authorize_url": url})

    def _handle_codex_oauth_start(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        try:
            ch = request_device_code()
        except Exception as exc:  # noqa: BLE001
            return _http_error(502, f"device code request failed: {exc}")
        return _http_json_response(
            {
                "user_code": ch.user_code,
                "verification_uri": ch.verification_uri,
                "device_auth_id": ch.device_auth_id,
                "interval": ch.interval,
                "expires_in": ch.expires_in,
            }
        )

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

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    async def _handle_settings_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.settings import SettingsUpdateCommand

        query = _parse_query(request.path)
        model = _query_first(query, "model")
        provider = _query_first(query, "provider")
        try:
            result = await self._services.get("settings").update(
                SettingsUpdateCommand(model=model, provider=provider),
                Principal.local(),
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

    async def _handle_settings_provider_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.settings import SettingsProviderUpdateCommand

        query = _parse_query(request.path)
        provider_name = _query_first(query, "provider") or ""
        # Normalize camelCase aliases for api_key / api_base.
        api_key: str | None = None
        if "api_key" in query or "apiKey" in query:
            api_key = _query_first(query, "api_key")
            if api_key is None:
                api_key = _query_first(query, "apiKey")
        api_base: str | None = None
        if "api_base" in query or "apiBase" in query:
            api_base = _query_first(query, "api_base")
            if api_base is None:
                api_base = _query_first(query, "apiBase")
        try:
            result = await self._services.get("settings").provider_update(
                SettingsProviderUpdateCommand(
                    provider=provider_name, api_key=api_key, api_base=api_base
                ),
                Principal.local(),
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

    async def _handle_settings_web_search_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.settings import SettingsWebSearchUpdateCommand

        query = _parse_query(request.path)
        provider_name = _query_first(query, "provider") or ""
        # Normalize camelCase aliases for api_key / base_url.
        api_key = _query_first(query, "api_key")
        if api_key is None:
            api_key = _query_first(query, "apiKey")
        base_url = _query_first(query, "base_url")
        if base_url is None:
            base_url = _query_first(query, "baseUrl")
        try:
            result = await self._services.get("settings").web_search_update(
                SettingsWebSearchUpdateCommand(
                    provider=provider_name, api_key=api_key, base_url=base_url
                ),
                Principal.local(),
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

    # -- secret store --------------------------------------------------------

    async def _handle_secrets_list(self, request: WsRequest) -> Response:
        """`GET /api/secrets` — entries' metadata. Never returns a value."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.secrets import SecretsListQuery

        result = await self._services.get("secrets").list(
            SecretsListQuery(), Principal.local()
        )
        return _http_json_response(result.model_dump())

    # A secret is created/updated over the websocket — see the
    # ``secret_store`` envelope in ``_handle_secret_store_envelope``.
    # The value rides a JSON frame, never a URL query.

    async def _handle_secret_delete(self, request: WsRequest) -> Response:
        """`GET /api/secrets/delete` — remove a secret."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.secrets import SecretDeleteCommand

        name = (_query_first(_parse_query(request.path), "name") or "").strip()
        try:
            result = await self._services.get("secrets").delete(
                SecretDeleteCommand(name=name), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

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
            label = f"every {_humanize_interval_ms(sched.every_ms or 0)}"
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
                # True while a run (scheduled or manual) is in flight — lets
                # the webui disable "Run now" and show a spinner.
                "executing": bool(
                    self._cron_service is not None
                    and self._cron_service.is_executing(job.id)
                ),
            },
            "created_at_ms": job.created_at_ms,
            "updated_at_ms": job.updated_at_ms,
        }

    async def _handle_skills_list(self, request: WsRequest) -> Response:
        """`GET /api/skills` — shim → SkillsService.list."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.skills import SkillsListQuery

        try:
            result = await self._services.get("skills").list(SkillsListQuery(), Principal.local())
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skills_quarantine(self, request: WsRequest) -> Response:
        """`GET /api/skills/quarantine` — shim → SkillsService.quarantine."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.skills import SkillsQuarantineQuery

        try:
            result = await self._services.get("skills").quarantine(
                SkillsQuarantineQuery(), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_get(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}` — shim → SkillsService.get."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.service.skills import SkillGetQuery

        try:
            result = await self._services.get("skills").get(
                SkillGetQuery(name=decoded), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_save(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/save?content=...` — shim → SkillsService.save."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        query = _parse_query(request.path)
        content = _query_first(query, "content")
        if content is None:
            return _http_error(400, "content is required")
        from durin.service.skills import SkillSaveCommand

        try:
            result = await self._services.get("skills").save(
                SkillSaveCommand(name=decoded, content=content), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_files(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/files` — shim → SkillsService.files."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.service.skills import SkillFilesQuery

        try:
            result = await self._services.get("skills").files(
                SkillFilesQuery(name=decoded), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_file(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/file?path=...` — shim → SkillsService.file_get."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        path = _query_first(_parse_query(request.path), "path")
        if not path:
            return _http_error(400, "path is required")
        from durin.service.skills import SkillFileQuery

        try:
            result = await self._services.get("skills").file_get(
                SkillFileQuery(name=decoded, path=path), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_file_save(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/file/save?path=&content=` — shim → SkillsService.file_save."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        query = _parse_query(request.path)
        path = _query_first(query, "path")
        content = _query_first(query, "content")
        if not path:
            return _http_error(400, "path is required")
        if content is None:
            return _http_error(400, "content is required")
        from durin.service.skills import SkillFileSaveCommand

        try:
            result = await self._services.get("skills").file_save(
                SkillFileSaveCommand(name=decoded, path=path, content=content), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_history(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/history` — shim → SkillsService.history."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.service.skills import SkillHistoryQuery

        try:
            result = await self._services.get("skills").history(
                SkillHistoryQuery(name=decoded), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_mode(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/mode?value=auto|manual` — shim → SkillsService.mode."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        value = (_query_first(_parse_query(request.path), "value") or "").strip()
        from durin.service.skills import SkillModeCommand

        try:
            result = await self._services.get("skills").mode(
                SkillModeCommand(name=decoded, value=value), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skills_resolve(self, request: WsRequest) -> Response:
        """`GET /api/skills/resolve?source=` — shim → SkillsService.resolve."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        source = (_query_first(_parse_query(request.path), "source") or "").strip()
        if not source:
            return _http_error(400, "source is required")
        from durin.service.skills import SkillsResolveQuery

        try:
            result = await self._services.get("skills").resolve(
                SkillsResolveQuery(source=source), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skills_import(self, request: WsRequest) -> Response:
        """`GET /api/skills/import?source=` — shim → SkillsService.import_skill."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        source = (_query_first(_parse_query(request.path), "source") or "").strip()
        if not source:
            return _http_error(400, "source is required")
        from durin.service.skills import SkillsImportCommand

        try:
            result = await self._services.get("skills").import_skill(
                SkillsImportCommand(source=source), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_search(self, request: WsRequest) -> Response:
        """`GET /api/skills/search?query=&limit=` — shim → SkillsService.search (async off-thread)."""
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
        from durin.service.skills import SkillSearchQuery

        try:
            result = await self._services.get("skills").search(
                SkillSearchQuery(q=q, limit=limit), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_describe(self, request: WsRequest) -> Response:
        """`GET /api/skills/describe?ref=` — shim → SkillsService.describe (async off-thread)."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        ref = (_query_first(_parse_query(request.path), "ref") or "").strip()
        if not ref:
            return _http_error(400, "ref is required")
        from durin.service.skills import SkillDescribeQuery

        try:
            result = await self._services.get("skills").describe(
                SkillDescribeQuery(ref=ref), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skills_github_token_test(self, request: WsRequest) -> Response:
        """`GET /api/skills/github-token-test?secret=` — shim → SkillsService.github_token_test."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        secret = (_query_first(_parse_query(request.path), "secret") or "").strip()
        from durin.service.skills import GithubTokenTestQuery

        try:
            result = await self._services.get("skills").github_token_test(
                GithubTokenTestQuery(secret=secret), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_approve(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/approve?confirm=&override=&install_deps=` — shim → SkillsService.approve."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        query = _parse_query(request.path)
        confirm = (_query_first(query, "confirm") or "").lower() in ("1", "true", "yes")
        override = (_query_first(query, "override") or "").lower() in ("1", "true", "yes")
        replace = (_query_first(query, "replace") or "").lower() in ("1", "true", "yes")
        install_deps = (_query_first(query, "install_deps") or "").lower() in ("1", "true", "yes")
        from durin.service.skills import SkillApproveCommand

        try:
            result = await self._services.get("skills").approve(
                SkillApproveCommand(
                    name=decoded, confirm=confirm, override=override,
                    replace=replace, install_deps=install_deps,
                ),
                Principal.local(),
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_install_deps(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/install-deps?bin=` — shim → SkillsService.install_deps."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        bin_name = _query_first(_parse_query(request.path), "bin")
        from durin.service.skills import SkillInstallDepsCommand

        try:
            result = await self._services.get("skills").install_deps(
                SkillInstallDepsCommand(name=decoded, bin_name=bin_name), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _run_skill_audit(self, connection: Any, name: str) -> None:
        """Stream an on-demand LLM audit of a quarantined skill: reasoning deltas
        on ``audit:<name>`` (reusing the chat reasoning stream), then a terminal
        ``skill_audit_done`` with the structured outcome. Persists .scan.json."""
        import json as _json

        from durin.agent import skills_store as ss
        from durin.memory.llm_invoke import default_llm_invoke_astream
        from durin.providers.base import LLMProvider
        from durin.security.skill_judge import JudgeError, judge_skill_astream
        from durin.security.skill_scan import ScanReport, scan_skill

        chat_id = f"audit:{name}"
        workspace = self._endpoint_workspace()
        qdir = Path(workspace) / ".durin" / "import-quarantine" / name
        if not (qdir / "SKILL.md").is_file():
            await self._send_event(connection, "skill_audit_done", chat_id=chat_id,
                                   name=name, judged=False, error_code="not_found")
            return
        _, model, max_sev = ss._import_judge()
        det = scan_skill(qdir)

        async def on_reasoning(text: str) -> None:
            await self.send_reasoning_delta(chat_id, text)

        try:
            outcome = await judge_skill_astream(
                qdir, ainvoke_stream=default_llm_invoke_astream,
                model=model or "glm-5.1", max_severity=max_sev, on_reasoning=on_reasoning,
            )
        except JudgeError as exc:
            await self.send_reasoning_end(chat_id)
            code = "parse" if "parse" in str(exc).lower() else "unreachable"
            await self._send_event(connection, "skill_audit_done", chat_id=chat_id,
                                   name=name, judged=False, error_code=code)
            return
        except Exception as exc:  # noqa: BLE001
            await self.send_reasoning_end(chat_id)
            code = "unreachable" if LLMProvider._is_transient_error(str(exc)) else "no_model"
            await self._send_event(connection, "skill_audit_done", chat_id=chat_id,
                                   name=name, judged=False, error_code=code)
            return

        await self.send_reasoning_end(chat_id)
        merged = ScanReport(findings=det.findings + outcome.findings)
        merged.tools = outcome.tools
        merged.judge_verdict = outcome.verdict
        findings = [{"category": f.category, "severity": f.severity, "where": f.where,
                     "detail": f.detail} for f in merged.findings]
        source = name
        sj = qdir / ".scan.json"
        if sj.is_file():
            try:
                source = _json.loads(sj.read_text()).get("source", name)
            except Exception:  # noqa: BLE001
                pass
        ss._persist_judge_result(qdir, source, merged.verdict, findings, outcome.summary)
        await self._send_event(connection, "skill_audit_done", chat_id=chat_id, name=name,
                               judged=True, verdict=merged.verdict, findings=findings,
                               summary=outcome.summary)

    async def _handle_skill_judge(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/judge` — shim → SkillsService.judge (async off-thread)."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.service.skills import SkillJudgeQuery

        try:
            result = await self._services.get("skills").judge(
                SkillJudgeQuery(name=decoded), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_reject(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/reject` — shim → SkillsService.reject."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.service.skills import SkillRejectCommand

        try:
            result = await self._services.get("skills").reject(
                SkillRejectCommand(name=decoded), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_skill_remove(self, request: WsRequest, name: str) -> Response:
        """`GET /api/skills/{name}/remove` — shim → SkillsService.remove."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded = _decode_api_key(name)
        if decoded is None:
            return _http_error(400, "invalid skill name")
        from durin.service.skills import SkillRemoveCommand

        try:
            result = await self._services.get("skills").remove(
                SkillRemoveCommand(name=decoded), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.data, status=result.status)

    async def _handle_cron_list(self, request: WsRequest) -> Response:
        """`GET /api/cron` — list all scheduled jobs."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.cron import CronListQuery

        try:
            result = await self._services.get("cron").list(CronListQuery(), Principal.local())
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

    async def _handle_cron_remove(self, request: WsRequest) -> Response:
        """`GET /api/cron/remove?id=...` — remove a non-system job."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.cron import CronRemoveCommand

        job_id = (_query_first(_parse_query(request.path), "id") or "").strip()
        if not job_id:
            return _http_error(400, "id is required")
        try:
            result = await self._services.get("cron").remove(
                CronRemoveCommand(id=job_id), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

    async def _handle_cron_run(self, request: WsRequest) -> Response:
        """`GET /api/cron/run?id=...` — manually trigger a job now.

        The service validates whether the job can run and returns the decision.
        When it returns ``started=True``, this shim spawns the actual background
        task on the live scheduler (loop/connection concern stays here).
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.cron import CronRunCommand

        job_id = (_query_first(_parse_query(request.path), "id") or "").strip()
        if not job_id:
            return _http_error(400, "id is required")
        try:
            decision = await self._services.get("cron").run(
                CronRunCommand(id=job_id), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        if decision.started:
            async def _run() -> None:
                try:
                    await self._cron_service.run_job(job_id, force=True)
                except Exception:  # noqa: BLE001
                    logger.exception("cron run-now failed for {}", job_id)

            task = asyncio.create_task(_run())
            self._background_run_tasks.add(task)
            task.add_done_callback(self._background_run_tasks.discard)
        return _http_json_response(decision.model_dump(exclude_none=True))

    async def _handle_cron_toggle(self, request: WsRequest) -> Response:
        """`GET /api/cron/toggle?id=...&enabled=true|false` — enable or disable a job."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.cron import CronToggleCommand

        query = _parse_query(request.path)
        job_id = (_query_first(query, "id") or "").strip()
        enabled_raw = (_query_first(query, "enabled") or "").strip().lower()
        if not job_id:
            return _http_error(400, "id is required")
        enabled = enabled_raw in ("1", "true", "yes")
        try:
            result = await self._services.get("cron").toggle(
                CronToggleCommand(id=job_id, enabled=enabled), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

    # -- generic config API (web parity Phase B) ---------------------------

    async def _handle_config_get(self, request: WsRequest) -> Response:
        """`GET /api/config` — shim → ConfigService.get."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.config import ConfigGetQuery

        try:
            result = await self._services.get("config").get(ConfigGetQuery(), Principal.local())
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump(by_alias=True))

    async def _handle_config_set(self, request: WsRequest) -> Response:
        """`GET /api/config/set` — shim → ConfigService.set."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.config import ConfigSetCommand

        query = _parse_query(request.path)
        key = (_query_first(query, "key") or "").strip()
        if not key:
            return _http_error(400, "key is required")
        raw_value = _query_first(query, "value")
        if raw_value is None:
            return _http_error(400, "value is required")

        try:
            result = await self._services.get("config").set(
                ConfigSetCommand(key=key, value=raw_value), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump(by_alias=True))

    def _handle_logs_list(self, request: WsRequest) -> Response:
        """`GET /api/logs?source=&...` — read-only log viewer (gateway/telemetry).

        Reads JSONL log segments newest-first with transparent gz
        decompression, grep-before-parse, before_ts cursor pagination, and
        a bounded scan window. The telemetry backend is NOT touched — this
        only reads the existing files.
        """
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from pathlib import Path

        from durin.cli.gateway_daemon import daemon_logs_path
        from durin.logs.reader import compute_facets, read_page

        query = _parse_query(request.path)
        log_query = _logs_query_from_params(query)
        if log_query.source == "telemetry":
            directory = Path.home() / ".cache" / "durin" / "telemetry"
        else:
            directory = daemon_logs_path().parent
        try:
            page = read_page(directory, log_query)
            facets = compute_facets(directory, log_query.source)
        except Exception as exc:  # noqa: BLE001
            return _http_error(500, f"log read failed: {exc}")
        return _http_json_response(
            {
                "lines": page.lines,
                "facets": facets,
                "next_cursor": page.next_cursor,
                "scanned_through_ts": page.scanned_through_ts,
                "has_more": page.has_more,
            }
        )

    async def _handle_models_list(self, request: WsRequest) -> Response:
        """`GET /api/models?provider=X&capability=Y` — shim → ConfigService.models_list."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.config import ModelsListQuery

        query = _parse_query(request.path)
        provider = (_query_first(query, "provider") or "").strip()
        capability = (_query_first(query, "capability") or "").strip().lower()
        try:
            result = await self._services.get("config").models_list(
                ModelsListQuery(provider=provider, capability=capability), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump(by_alias=True))

    async def _handle_model_capabilities(self, request: WsRequest) -> Response:
        """`GET /api/model/capabilities?model=&provider=` — shim → ConfigService.model_capabilities."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.config import ModelCapabilitiesQuery

        query = _parse_query(request.path)
        model = (_query_first(query, "model") or "").strip()
        provider = (_query_first(query, "provider") or "").strip() or None
        if not model:
            return _http_error(400, "model is required")
        try:
            result = await self._services.get("config").model_capabilities(
                ModelCapabilitiesQuery(model=model, provider=provider), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        # Wire format is snake_case (matches original handler) — do not use by_alias.
        return _http_json_response(result.model_dump())

    async def _handle_channels_list(self, request: WsRequest) -> Response:
        """`GET /api/channels` — shim → ConfigService.channels_list."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.config import ChannelsListQuery

        try:
            result = await self._services.get("config").channels_list(
                ChannelsListQuery(), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump(by_alias=True))

    async def _handle_model_test(self, request: WsRequest) -> Response:
        """`GET /api/model/test` — shim → ConfigService.model_test (async probe)."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.config import ModelTestQuery

        query = _parse_query(request.path)
        model = (_query_first(query, "model") or "").strip()
        provider = (_query_first(query, "provider") or "").strip()
        try:
            result = await self._services.get("config").model_test(
                ModelTestQuery(model=model, provider=provider), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump(by_alias=True))

    async def _handle_cross_encoder_test(
        self, request: WsRequest, query: dict,
    ) -> Response:
        """`GET /api/memory/cross-encoder/test?model=<id>` — shim → ConfigService.cross_encoder_test."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.service.config import CrossEncoderTestQuery

        model = (_query_first(query, "model") or "").strip()
        if not model:
            return _http_error(400, "missing required `model` query param")
        try:
            result = await self._services.get("config").cross_encoder_test(
                CrossEncoderTestQuery(model=model), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        # Wire format is snake_case (matches original handler) — do not use by_alias.
        return _http_json_response(result.model_dump())

    def _handle_extras_status(self, request: WsRequest, query: dict) -> Response:
        """`GET /api/extras/status?feature=<f>` — is the feature's pip extra
        importable, and what would installing it cost / require?"""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from durin.extras import REGISTRY, _module_present

        feature = (_query_first(query, "feature") or "").strip()
        fe = REGISTRY.get(feature)
        if fe is None:
            return _http_error(400, f"unknown feature '{feature}'")
        return _http_json_response(
            {
                "present": _module_present(fe.module),
                "extra": fe.extra,
                "approx_size": fe.approx_size,
                "needs_restart": fe.needs_restart,
                "label": fe.label,
            }
        )

    async def _handle_extras_ensure(self, request: WsRequest, query: dict) -> Response:
        """`POST /api/extras/ensure?feature=<f>&restart=<bool>` — install the
        feature's extra (off-loop; may take minutes for heavy deps) and, if it
        installed something that needs a restart and the caller opted in, kick a
        gateway restart."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        import asyncio

        from durin.config.loader import load_config
        from durin.extras import REGISTRY, ensure_extra

        feature = (_query_first(query, "feature") or "").strip()
        if feature not in REGISTRY:
            return _http_error(400, f"unknown feature '{feature}'")
        restart = (_query_first(query, "restart") or "").lower() in ("1", "true", "yes")
        res = await asyncio.to_thread(ensure_extra, feature, config=load_config())
        out = {
            "status": res.status,
            "needs_restart": res.needs_restart,
            "message": res.message,
        }
        if res.status == "installed" and restart and res.needs_restart:
            self._spawn_gateway_restart()
            out["restarting"] = True
        return _http_json_response(out)

    def _handle_extras_restart(self, request: WsRequest) -> Response:
        """`POST /api/extras/restart` — restart the gateway daemon."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        self._spawn_gateway_restart()
        return _http_json_response({"restarting": True})

    @staticmethod
    def _spawn_gateway_restart() -> None:
        import subprocess
        import sys

        # Detached so it survives this process being killed by the restart.
        subprocess.Popen(
            [sys.executable, "-m", "durin", "gateway", "restart"],
            start_new_session=True,
        )

    @staticmethod
    def _is_websocket_channel_session_key(key: str) -> bool:
        """True when *key* is a ``websocket:…`` session exposed on this HTTP surface."""
        return key.startswith("websocket:")

    async def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        from durin.service.sessions import SessionMessagesQuery

        try:
            result = await self._services.get("sessions").messages(
                SessionMessagesQuery(key=decoded_key), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        # Decorate persisted user messages with signed media URLs so the
        # client can render previews. The raw on-disk ``media`` paths are
        # stripped on the way out — they leak server filesystem layout and
        # the client never needs them once it has the signed fetch URL.
        self._augment_media_urls(result.data)
        return _http_json_response(result.data)

    async def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        from durin.service.sessions import WebuiThreadQuery

        try:
            await self._services.get("sessions").webui_thread(
                WebuiThreadQuery(key=decoded_key), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        # The service validated the key; the signing callback (_augment_transcript_user_media)
        # HMAC-signs file paths using this channel's per-process _media_secret — it cannot
        # live in a pure service method, so the build happens here in the shim.
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

    async def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        from durin.service.sessions import SessionDeleteCommand

        try:
            result = await self._services.get("sessions").delete(
                SessionDeleteCommand(key=decoded_key), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

    async def _handle_session_rename(self, request: WsRequest, key: str) -> Response:
        """P2 (doc 20): set a user-edited title for a webui session."""
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        from durin.service.sessions import SessionRenameCommand

        query = _parse_query(request.path)
        raw_title = _query_first(query, "title") or ""
        try:
            result = await self._services.get("sessions").rename(
                SessionRenameCommand(key=decoded_key, title=raw_title), Principal.local()
            )
        except DomainError as err:
            return _domain_error_response(err)
        return _http_json_response(result.model_dump())

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
        if t == "skill_judge":
            name = envelope.get("name")
            if not isinstance(name, str) or not name:
                await self._send_event(connection, "error", detail="missing skill name")
                return
            self._attach(connection, f"audit:{name}")
            await self._run_skill_audit(connection, name)
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
            except asyncio.CancelledError:
                # Expected: the server task is cancelled during shutdown. It is a
                # BaseException (not Exception), so without this it escapes the
                # handler below and surfaces as an "uncaught exception" on every stop.
                pass
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
