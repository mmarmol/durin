"""WebSocket server channel: durin acts as a WebSocket server and serves connected clients."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
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

from loguru import logger
from pydantic import Field, field_validator, model_validator
from websockets.exceptions import ConnectionClosed

from durin.bus.events import INBOUND_META_NOT_AN_ANSWER, OUTBOUND_META_AGENT_UI, OutboundMessage
from durin.bus.queue import MessageBus
from durin.channels.base import BaseChannel
from durin.config.paths import get_media_dir
from durin.config.schema import Base
from durin.service import DomainError, Principal, ServiceRegistry
from durin.service.principal import Scope
from durin.service.types import (
    ForbiddenError,
    NotFoundError,
    TooManyRequestsError,
    UnauthenticatedError,
    ValidationFailedError,
)
from durin.session.goal_state import goal_state_ws_blob
from durin.utils.helpers import safe_filename
from durin.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from durin.utils.webui_transcript import get_transcript_writer
from durin.utils.webui_turn_helpers import websocket_turn_wall_started_at

if TYPE_CHECKING:
    from durin.cron.service import CronService
    from durin.session.manager import SessionManager


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def _normalize_config_path(path: str) -> str:
    return _strip_trailing_slash(path)


def _voice_preview_sample(language: str | None) -> str:
    lang = (language or "en")[:2].lower()
    return {"es": "Hola, soy durin.", "en": "Hi, I am durin."}.get(lang, "Hi, I am durin.")


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    - ``client_id``: Used for ``allow_from`` authorization; if omitted, a value is generated and logged.
    - ``token``: If non-empty, the ``token`` query param may match this static secret; short-lived tokens
      minted by ``GET /webui/bootstrap`` are also accepted.
    - ``token_issue_secret``: If non-empty, ``GET /webui/bootstrap`` must present the secret via
      ``Authorization: Bearer <secret>`` or ``X-Durin-Auth: <secret>`` (the reverse-proxy deployment path).
      On success the gateway sets an ``httpOnly`` ``durin_session`` cookie holding an opaque session
      token; later bootstraps (reloads) are re-authorized by that cookie, so the webui never stores the
      secret client-side. ``POST /webui/signout`` revokes the session token and clears the cookie.
    - ``webui_session_ttl_s``: Lifetime of the ``durin_session`` cookie/token (default 7 days).
    - ``websocket_requires_token``: If True, the handshake must include a valid token (static or issued and not expired).
    - Each connection has its own session: a unique ``chat_id`` maps to the agent session internally.
    - ``media`` field in outbound messages contains local filesystem paths; remote clients need a
      shared filesystem or an HTTP file server to access these files.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/"
    token: str = Field(default="", json_schema_extra={"secret": True})
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    webui_session_ttl_s: int = Field(default=604_800, ge=300, le=2_592_000)
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


def publish_dream_progress(bus: MessageBus, payload: dict[str, Any]) -> None:
    """Enqueue a dream-progress frame for ALL websocket subscribers (global fan-out).

    ``payload`` is a small dict with a ``kind`` ("run_started" | "activity" |
    "run_finished") plus kind-specific fields. Mirrors
    :func:`publish_runtime_model_update`: a ``chat_id="*"`` outbound message
    flagged so the channel's ``send`` dispatcher broadcasts it to every open
    connection rather than a single chat's subscribers.
    """
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={"_dream_progress": True, "dream": payload},
    ))


def publish_concurrency_snapshot(bus: MessageBus, snapshot: dict[str, Any]) -> None:
    """Enqueue a concurrency snapshot for ALL websocket connections (global fan-out).

    Mirrors :func:`publish_dream_progress`: a ``chat_id="*"`` frame flagged so
    the channel's ``send`` dispatcher broadcasts it to every open connection.
    Droppable — a full outbound bus skips this frame; the next boundary
    re-publishes the latest state, so a lost snapshot never desyncs the UI for
    long."""
    try:
        bus.outbound.put_nowait(OutboundMessage(
            channel="websocket",
            chat_id="*",
            content="",
            metadata={"_concurrency_snapshot": True, "snapshot": snapshot},
        ))
    except asyncio.QueueFull:
        pass


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


def _resolve_bootstrap_model_preset(
    runtime_preset: Callable[[], str | None] | None,
) -> str | None:
    """Active preset name for bootstrap (carries the effort suffix, e.g.
    ``default:high``) so the dashboard's effort picker is correct on first load,
    not only after a live switch. Runtime-only, so no config fallback."""
    if runtime_preset is None:
        return None
    try:
        raw = runtime_preset()
    except Exception as e:
        logger.debug("bootstrap runtime preset resolver failed: {}", e)
        return None
    return raw.strip() or None if isinstance(raw, str) else None


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key*, or None."""
    values = query.get(key)
    return values[0] if values else None


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
_TURN_OUTCOMES = frozenset({"completed", "stopped", "failed"})
# Approval ids are server-minted 12-hex-char tokens. Anything else is refused
# before it can name a path under ``.approvals/``.
_APPROVAL_ID_RE = re.compile(r"^[0-9a-f]{12}$")
# Decision outcomes meaning the click did what the person asked.
_APPROVAL_OK = frozenset({"applied", "rejected", "pending"})


def _is_live_progress_only(payload: dict[str, Any]) -> bool:
    """Return True when every tool_event in the payload needs no transcript row.

    Running frames are streamed live to open connections for inline block
    updates but are reconstructable from the terminal frame, so they do not
    need to be persisted to the webui transcript. A memory_prefetch 'start'
    frame is the same case: the loop always follows it with an 'end' frame,
    even on cancellation (delivered from a background task rather than
    dropped — see AgentLoop._state_build), so persisting 'start' on its own
    would only ever produce an orphan record. A mixed frame (at least one
    record-worthy event) or a frame with no tool_events still persists.
    """
    te = payload.get("tool_events")
    return (
        isinstance(te, list)
        and len(te) > 0
        and all(_is_live_only_event(e) for e in te)
    )


def _is_live_only_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("phase") == "running":
        return True
    return event.get("phase") == "start" and event.get("name") == "memory_prefetch"


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _answers_approvals(connection: Any) -> bool:
    """True for a watcher that can answer an approval the chat waits on.

    A socket the dashboard session opened can (the webui's Approve / Reject).
    A watcher that cannot declares ``answers_approvals = False``: a socket
    opened with the static token or with no token, and the API's SSE
    subscriber, whose client may answer a question with a plain message but
    never decides an approval. Such a watcher's decision frame is refused, and
    its presence does not keep an approval waiting."""
    return bool(getattr(connection, "answers_approvals", True))


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
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MAX_AUDIO_PER_MESSAGE = 1
_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
_MAX_DOCUMENTS_PER_MESSAGE = 3
# Grace window before a disconnected voice session is torn down. Long enough to
# ride out a wifi blip / backgrounded tab and let the browser reconnect onto the
# same session; short enough that a truly closed tab frees it promptly.
_VOICE_GRACE_S = 30.0

# Grace window before a turn blocked on the user's answer in a chat nobody is
# watching stops waiting and yields. A page refresh or a network blip
# re-subscribes within seconds and keeps the wait; a closed tab ends it.
_ANSWER_GRACE_S = 30.0

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

# Audio MIME whitelist — accepted as attachments and transcribed server-side.
# Matches the webui composer ``useAttachedAudio`` whitelist.
_AUDIO_MIME_ALLOWED: frozenset[str] = frozenset({
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/aac",
    "audio/flac",
})

# Document MIME whitelist — attachments handed to the agent as a file path
# (NOT inlined like images): the agent reads them with `convert_to_markdown`
# or `memory_ingest`. Matches the webui composer `useAttachedDocuments`
# whitelist and the formats `durin/memory/doc_convert.py` handles.
_DOCUMENT_MIME_ALLOWED: frozenset[str] = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/epub+zip",
    "text/html",
    "text/csv",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/xml",
    "text/xml",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = (
    _IMAGE_MIME_ALLOWED
    | _VIDEO_MIME_ALLOWED
    | _AUDIO_MIME_ALLOWED
    | _DOCUMENT_MIME_ALLOWED
)

# Tolerate media-type parameters between the MIME and the ``;base64`` marker —
# browser ``MediaRecorder`` emits ``data:audio/webm;codecs=opus;base64,...``.
# Capture only the base ``type/subtype`` (group 1); params like ``;codecs=opus``
# are matched and discarded. Without this, recorded audio was rejected as
# "decode" and the upload silently failed.
_DATA_URL_MIME_RE = re.compile(r"^data:([^;,]+)(?:;[\w.+-]+=[^;,]*)*;base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


_LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _peer_is_loopback(peer: Any) -> bool:
    """True when *peer* (a ``(host, port)`` tuple/Address, or None) is loopback.

    The adapter passes Starlette's ``request.client`` (an ``Address`` namedtuple,
    or None when the server omits the peer — then we fail closed → False).
    """
    if not peer:
        return False
    host = peer[0] if isinstance(peer, (tuple, list)) else peer
    if not isinstance(host, str):
        return False
    # ``::ffff:127.0.0.1`` is loopback in IPv6-mapped form.
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _LOCALHOSTS


# Host names that can only mean this machine. A DNS-rebinding page reaches a
# loopback peer under its own name, so the Host header must be one of these
# (any port) for: the bootstrap route without a setup secret, and any
# anonymous socket whenever no token is required — a setup secret changes how
# a token is obtained, not the fact that an anonymous connection carries none.
_LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _host_is_loopback(headers: Any) -> bool:
    """True when the request's ``Host`` header names this machine by a
    loopback name (``localhost``, ``127.0.0.1`` or ``[::1]``, with or
    without a port). A missing or empty header is not."""
    raw = str(headers.get("host") or headers.get("Host") or "").strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        name = raw[: end + 1] if end != -1 else raw
        rest = raw[end + 1:] if end != -1 else ""
    else:
        name, _, port = raw.partition(":")
        rest = f":{port}" if port else ""
    if rest and not (rest.startswith(":") and rest[1:].isdigit()):
        return False
    return name in _LOOPBACK_HOST_NAMES


def _origin_is_loopback(headers: Any) -> bool:
    """True when the request carries no ``Origin`` header (a non-browser
    client), or one whose host is a loopback name. A browser always sends
    ``Origin`` on a socket upgrade, and a page on another site — a
    DNS-rebinding page, or any site opening ``ws://127.0.0.1`` directly, since
    a socket is not bound by the same-origin policy — sends its own there. An
    empty value, ``null`` or a non-web scheme is not loopback."""
    origin = headers.get("origin")
    if origin is None:
        origin = headers.get("Origin")
    if origin is None:
        return True
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(origin).strip())
        parts.port  # noqa: B018 — raises ValueError on a malformed port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname or ""
    return (f"[{host}]" if ":" in host else host) in _LOOPBACK_HOST_NAMES


def _bearer_token(headers: Any) -> str | None:
    """Pull a Bearer token out of standard or query-style headers."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


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
    "audio/mpeg",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/webm",
    "audio/x-m4a",
    "audio/aac",
    "audio/flac",
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


_SESSION_COOKIE = "durin_session"


def _session_cookie_value(headers: Any) -> str | None:
    """Extract the ``durin_session`` cookie value from a request's headers."""
    raw = headers.get("Cookie") or headers.get("cookie")
    if not raw:
        return None
    from http.cookies import CookieError, SimpleCookie

    try:
        jar: SimpleCookie = SimpleCookie()
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(_SESSION_COOKIE)
    return morsel.value if morsel else None


class ConnectionAdapter:
    """Transport-agnostic seam around a single WebSocket connection.

    Step 1 (this commit): wraps a ``websockets.ServerConnection`` and delegates
    all I/O to it.  Step 3 will add a Starlette-``WebSocket``-backed subclass so
    the chat logic (``_connection_loop``, ``_safe_send_to``, ``_send_event``)
    never touches the transport library directly.

    The instance is used as the identity key in ``_subs`` / ``_conn_chats`` /
    ``_conn_default`` — default object identity (``id(self)``) provides the
    stable, hashable key each connection needs.
    """

    __slots__ = ("_conn", "_iter")

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._iter: Any = None

    async def send_text(self, raw: str) -> None:
        """Send a text frame; delegates to the underlying connection."""
        await self._conn.send(raw)

    @property
    def remote(self) -> Any:
        """Remote address of the underlying connection (same shape as ``remote_address``)."""
        return getattr(self._conn, "remote_address", None)

    def __aiter__(self) -> "ConnectionAdapter":
        self._iter = self._conn.__aiter__()
        return self

    async def __anext__(self) -> Any:
        if self._iter is None:
            self._iter = self._conn.__aiter__()
        return await self._iter.__anext__()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the underlying connection (used by the Starlette adapter in step 3)."""
        close_fn = getattr(self._conn, "close", None)
        if close_fn is not None:
            await close_fn(code, reason)


class WebSocketChannel(BaseChannel):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"
    channel_description = (
        "Transport for the web dashboard and external WebSocket clients. "
        "Always on while the dashboard is enabled. "
        "The token is optional, only for external clients."
    )

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        session_manager: "SessionManager | None" = None,
        static_dist_path: Path | None = None,
        runtime_model_name: Callable[[], str | None] | None = None,
        runtime_model_preset: Callable[[], str | None] | None = None,
        runtime_concurrency_snapshot: Callable[[], dict[str, Any]] | None = None,
        cron_service: "CronService | None" = None,
        approval_deps: Callable[[], Any] | None = None,
        session_turn_key: Callable[[str], str] | None = None,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # chat_id -> VoiceSession (conversational voice mode state).
        self._voice: dict[str, Any] = {}
        # chat_id -> pending teardown task. A socket drop schedules a delayed
        # drop here instead of killing the session at once; a reconnect that
        # re-subscribes cancels it, so a transient blip never ends the call.
        self._voice_cleanup: dict[str, asyncio.Task] = {}
        self._voice_grace_s: float = _VOICE_GRACE_S
        # chat_id -> pending release of a turn waiting on that chat's answer,
        # scheduled when its last subscriber leaves and cancelled by a
        # re-subscribe within the grace window.
        self._answer_fallback: dict[str, asyncio.Task] = {}
        self._answer_grace_s: float = _ANSWER_GRACE_S
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        # Single-use tokens consumed at WebSocket handshake.
        self._issued_tokens: dict[str, float] = {}
        self._session_manager = session_manager
        self._static_dist_path: Path | None = (
            static_dist_path.resolve() if static_dist_path is not None else None
        )
        self._runtime_model_name = runtime_model_name
        self._runtime_model_preset = runtime_model_preset
        self._runtime_concurrency_snapshot = runtime_concurrency_snapshot
        # Running CronService (same process). Used only by the run-now
        # endpoint so a manual trigger reaches the live scheduler + its
        # in-process overlap guard. None outside the gateway (tests).
        self._cron_service = cron_service
        # Live handles (exec tool, MCP service) that an approval decided from
        # this socket runs with when its turn is no longer waiting on it.
        # None outside the gateway: such a decision then reports what it lacks.
        self._approval_deps = approval_deps
        # How the agent loop keys a chat's turns (one shared key in unified
        # mode), so an approval frame is matched to the chat that filed it.
        self._session_turn_key = session_turn_key
        # Strong refs to approval decisions still running (else GC'd mid-run).
        self._approval_tasks: set[asyncio.Task] = set()
        # Strong refs to fire-and-forget run-now tasks (else GC'd mid-run).
        self._background_run_tasks: set[asyncio.Task] = set()
        # Persistent HMAC secret for media URL signing.  Lives in the token
        # store so signed URLs survive a process restart (SP2).
        from durin.security.api_tokens import ApiTokenStore as _ApiTokenStore

        self._media_secret: bytes = _ApiTokenStore().get_or_create_media_secret()
        # Service-core registry: domain logic extracted out of this channel
        # (SP1). The HTTP handlers are now thin shims that call these services.
        # Built here so the deps are available; services are registered as each
        # domain is extracted.
        self._services: ServiceRegistry = self._build_services(bus)

    def _build_services(self, bus: MessageBus) -> ServiceRegistry:
        """Construct the registry holding the extracted domain services.

        Delegates to the shared :func:`durin.service.wiring.build_service_registry`
        so the websocket shims and the SP4 Starlette front door serve the SAME
        wired service set.
        """
        from durin.service.wiring import build_service_registry

        return build_service_registry(
            config=self.config,
            session_manager=self._session_manager,
            cron_service=self._cron_service,
            bus=bus,
            subagent_manager=None,
            chat_channel_resolver=lambda: self,
            approval_deps=self._approval_deps,
        )

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
        # A reconnect re-subscribing within the grace window keeps the voice
        # session alive: cancel any pending teardown for this chat.
        task = self._voice_cleanup.pop(chat_id, None)
        if task is not None:
            task.cancel()
        # ...and keeps a turn waiting on this chat's answer waiting, when this
        # viewer could answer what it waits on (see _can_answer_waiter).
        release = self._answer_fallback.get(chat_id)
        if release is not None and (
            _answers_approvals(connection)
            or self._waiting_kind(chat_id) != "approval"
        ):
            self._answer_fallback.pop(chat_id, None)
            release.cancel()

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
                self._schedule_answer_fallback(cid)
            elif _answers_approvals(connection) and not any(
                _answers_approvals(c) for c in subs
            ):
                # The last webui tab left while API watchers remain: they may
                # hold a question, never an approval, which the release
                # decides when it fires.
                self._schedule_answer_fallback(cid)
        # Defer voice-session teardown by a grace period rather than killing it
        # on the disconnect: a transient socket drop (wifi blip, backgrounded
        # tab) would otherwise end the conversation and discard the in-flight
        # reply. A reconnect cancels the pending drop via _attach.
        for cid in list(self._voice):
            if not self._subs.get(cid):
                self._schedule_voice_cleanup(cid)
        self._conn_default.pop(connection, None)

    def _schedule_voice_cleanup(self, chat_id: str) -> None:
        """Drop the voice session for *chat_id* after the grace window, unless a
        client re-subscribes first. Idempotent — a second call is a no-op while a
        teardown is already pending."""
        if chat_id in self._voice_cleanup:
            return

        async def _drop() -> None:
            try:
                await asyncio.sleep(self._voice_grace_s)
            except asyncio.CancelledError:
                return
            self._voice_cleanup.pop(chat_id, None)
            if not self._subs.get(chat_id):
                sess = self._voice.pop(chat_id, None)
                if sess is not None:
                    sess.cancel_speak()

        try:
            self._voice_cleanup[chat_id] = asyncio.ensure_future(_drop())
        except RuntimeError:
            # No running loop (sync teardown path): drop immediately.
            sess = self._voice.pop(chat_id, None)
            if sess is not None:
                sess.cancel_speak()

    def _schedule_answer_fallback(self, chat_id: str) -> None:
        """Stop a turn waiting on *chat_id*'s answer once nobody has watched
        the chat for the grace window, unless a client re-subscribes first.

        Without this, a turn blocked on ask_user in a closed tab waits out
        the whole answer timeout. Falling back makes the tool yield: its
        question stays in the session and the user's next message answers
        it. Idempotent while a release is already scheduled.

        Whether a remaining watcher holds the wait is decided when the release
        fires, since the turn may have moved from a question to an approval in
        the meantime (see _can_answer_waiter).
        """
        if chat_id in self._answer_fallback:
            return
        from durin.agent import pending_answers

        session_key = f"websocket:{chat_id}"

        async def _release() -> None:
            try:
                await asyncio.sleep(self._answer_grace_s)
            except asyncio.CancelledError:
                return
            self._answer_fallback.pop(chat_id, None)
            if not self._can_answer_waiter(chat_id):
                pending_answers.fallback(session_key)

        try:
            self._answer_fallback[chat_id] = asyncio.ensure_future(_release())
        except RuntimeError:
            # No running loop (sync teardown path): release at once.
            if not self._can_answer_waiter(chat_id):
                pending_answers.fallback(session_key)

    def _release_unwatched_ask(self, chat_id: str, blob: dict[str, Any]) -> None:
        """Start the grace window for an ask nobody watching can answer.

        A closing tab starts the window, but a question or approval asked
        after the last tab closed (that window already spent) would otherwise
        hold the turn for the whole answer timeout. The snapshot the turn
        pushes when it starts waiting says what is pending. A tab that loads
        inside the window cancels the release, as a returning tab does; the
        release still decides by what is waiting when it fires."""
        subs = self._subs.get(chat_id) or ()
        if blob.get("pending_approval"):
            watched = any(_answers_approvals(c) for c in subs)
        elif blob.get("pending_question"):
            watched = bool(subs)
        else:
            return
        if not watched:
            self._schedule_answer_fallback(chat_id)

    def _waiting_kind(self, chat_id: str) -> str | None:
        """What a turn waits on in *chat_id* (``question`` / ``approval``), keyed
        the way the unwatched-chat release keys it."""
        from durin.agent import pending_answers

        return pending_answers.waiting_kind(f"websocket:{chat_id}")

    def _can_answer_waiter(self, chat_id: str) -> bool:
        """True while someone watching *chat_id* could answer what its turn
        waits on. Any watcher can answer a question; an approval takes a
        webui tab, since an API watcher's message never decides one."""
        subs = self._subs.get(chat_id) or ()
        if self._waiting_kind(chat_id) == "approval":
            return any(_answers_approvals(c) for c in subs)
        return bool(subs)

    def _goal_state_session_key(self, chat_id: str) -> str:
        """The session key goal-state/pending-approval metadata actually lives
        under: normally "websocket:<chat_id>", but the loop's own turn key
        (e.g. "unified:default") when agents.defaults.unified_session folds
        every channel's conversation into one session — the asker saves
        pending_approval under that same effective key, not this channel's."""
        base = f"websocket:{chat_id}"
        if self._session_turn_key is not None:
            return self._session_turn_key(base)
        return base

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """Replay an active sustained goal from session metadata after *chat_id* is subscribed.

        Goal metadata lives on the session JSONL and survives gateway restarts, but
        connected clients normally see it via ``goal_state`` / ``turn_end`` frames.
        Pushing here makes refresh + reconnect restore the strip without a new model turn.
        """
        if self._session_manager is None:
            return
        session_key = self._goal_state_session_key(chat_id)
        row = self._session_manager.read_session_file(session_key)
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        self._drop_stale_pending_approval(session_key, meta)
        blob = goal_state_ws_blob(meta)
        # An approval the turn waits on is replayed too, so a refresh while
        # the gated tool blocks brings its card back.
        if not blob.get("active") and "pending_approval" not in blob:
            return
        await self.send_goal_state(chat_id, blob)

    def _drop_stale_pending_approval(self, session_key: str, meta: dict[str, Any]) -> None:
        """A crash can kill the turn between the asker writing ``pending_approval``
        into metadata and its ``finally`` popping it back out, so the saved
        record can outlive the waiter that would ever resolve it. Replay it on
        attach only while the approval store still says it is pending; otherwise
        drop it from *meta* (in place) and from the saved session, so a stale
        card does not haunt every future attach.
        """
        pa = meta.get("pending_approval")
        if not isinstance(pa, dict):
            return
        approval_id = str(pa.get("approval_id") or "")
        if not approval_id:
            return
        from durin.agent import approval_store

        record = approval_store.get(self._endpoint_workspace(), approval_id)
        if isinstance(record, dict) and record.get("status") == "pending":
            return
        meta.pop("pending_approval", None)
        session = self._session_manager.get_or_create(session_key)
        stored = session.metadata.get("pending_approval") if session.metadata else None
        if isinstance(stored, dict) and stored.get("approval_id") == approval_id:
            session.metadata.pop("pending_approval", None)
            self._session_manager.save(session)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """Replay ``goal_status: running`` when a turn is still active (same-process refresh)."""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _maybe_push_concurrency_snapshot(self, chat_id: str) -> None:
        """Push the current concurrency snapshot to the just-subscribed chat so a
        fresh or reconnected client shows live lane state without waiting for the
        next turn boundary."""
        if self._runtime_concurrency_snapshot is None:
            return
        try:
            snap = self._runtime_concurrency_snapshot()
        except Exception:  # noqa: BLE001 - hydrate is best-effort
            return
        if not isinstance(snap, dict):
            return
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "concurrency_snapshot", **snap}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" concurrency_snapshot ")

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay goal/run strip + concurrency state after subscribe (same-process refresh)."""
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)
        await self._maybe_push_concurrency_snapshot(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send_text(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=False)

    @classmethod
    def config_model(cls) -> type | None:
        return WebSocketConfig

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

    # -- HTTP endpoints (bootstrap, media) ----------------------------------

    def bootstrap(self, *, peer: Any, headers: Any) -> dict[str, Any]:
        """Mint a short-lived ADMIN token + session metadata for the webui.

        Gates (raised as DomainError → problem+json by the adapter):
        - a configured ``token_issue_secret``/static ``token`` must match the
          request header (secures deployments behind a reverse proxy where every
          connection appears local);
        - with NO secret, only a loopback *peer* may mint (local-dev mode), and
          only under a loopback ``Host`` name: a page in the local browser could
          otherwise reach this route through DNS rebinding (its own name
          resolving to 127.0.0.1) and mint an admin token;
        - the issued-token pool is capped to bound runaway growth.
        """
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        authorized_via_secret = False
        if secret:
            if _issue_route_secret_matches(headers, secret):
                authorized_via_secret = True
            elif not self._session_cookie_valid(headers):
                # Neither the setup secret nor a valid session cookie — reject.
                raise UnauthenticatedError("invalid bootstrap secret")
        elif not _peer_is_loopback(peer):
            raise ForbiddenError("bootstrap is localhost-only")
        elif not _host_is_loopback(headers):
            raise ForbiddenError(
                "without a setup secret, bootstrap answers only localhost, 127.0.0.1 or "
                "[::1]; to reach the dashboard under another name, set "
                "channels.websocket.token_issue_secret")
        # Cap outstanding tokens to avoid runaway growth from a misbehaving client.
        self._purge_expired_issued_tokens()
        if len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS:
            raise TooManyRequestsError("too many outstanding tokens")
        # Mint via the persisted store so this token survives a restart (SP2).
        # The store generates a fresh plaintext; we use that as the token so
        # the hash in the store always matches what we hand to the client.
        auth_svc = self._services.get("auth")
        if auth_svc is not None:
            # kind="webui": this token is the dashboard session, the one
            # credential a route that needs a person (deciding an approval)
            # accepts over HTTP.
            _, token = auth_svc._store.issue(
                [Scope.ADMIN.value],
                label="bootstrap",
                ttl_s=float(self.config.token_ttl_s),
                kind="webui",
            )
        else:
            token = f"nbwt_{secrets.token_urlsafe(32)}"
        # The WS handshake consumes a single-use token from _issued_tokens.
        self._issued_tokens[token] = time.monotonic() + float(self.config.token_ttl_s)
        payload: dict[str, Any] = {
            "token": token,
            "ws_path": self._expected_path(),
            "expires_in": self.config.token_ttl_s,
            "model_name": _resolve_bootstrap_model_name(self._runtime_model_name),
            "model_preset": _resolve_bootstrap_model_preset(self._runtime_model_preset),
            # True when this deploy gates bootstrap on a setup secret
            # (token_issue_secret or static token). The webui uses this to decide
            # whether to expose a "Logout" affordance — without a secret in play,
            # logout would strand the user on an auth form they have nothing to
            # type into (bootstrap auto-mints for localhost).
            "requires_secret": bool(secret),
        }
        # On a fresh secret sign-in (not a cookie re-auth), hand the HTTP adapter
        # an opaque session token to set as an httpOnly cookie. Subsequent
        # bootstraps re-authorize via that cookie, so the secret is never stored
        # client-side. Keys are stripped from the JSON body by the adapter.
        if authorized_via_secret:
            session_token = self._issue_session_token()
            if session_token:
                payload["_session_cookie"] = session_token
                payload["_session_cookie_max_age"] = int(self.config.webui_session_ttl_s)
        return payload

    # -- webui session cookie (httpOnly, opaque, revocable) -----------------

    _SESSION_LABEL = "webui-session"

    def _session_token_store(self) -> Any:
        """The persisted API-token store backing webui session cookies, or None."""
        auth_svc = self._services.get("auth")
        return getattr(auth_svc, "_store", None) if auth_svc is not None else None

    def _session_cookie_valid(self, headers: Any) -> bool:
        """True if the request carries a live ``durin_session`` cookie that maps
        to a non-expired session token in the store."""
        value = _session_cookie_value(headers)
        if not value:
            return False
        store = self._session_token_store()
        if store is None:
            return False
        entry = store.resolve(value)
        return bool(
            entry
            and entry.get("label") == self._SESSION_LABEL
            and Scope.ADMIN.value in (entry.get("scopes") or [])
        )

    def _issue_session_token(self) -> str | None:
        """Mint a fresh opaque session token (None if no store is available)."""
        store = self._session_token_store()
        if store is None:
            return None
        _, token = store.issue(
            [Scope.ADMIN.value],
            label=self._SESSION_LABEL,
            ttl_s=float(self.config.webui_session_ttl_s),
        )
        return token

    def revoke_session(self, *, headers: Any) -> None:
        """Revoke the session token named by the request's ``durin_session``
        cookie (best-effort; a missing/unknown cookie is a no-op)."""
        value = _session_cookie_value(headers)
        if not value:
            return
        store = self._session_token_store()
        if store is None:
            return
        entry = store.resolve(value)
        if entry and entry.get("label") == self._SESSION_LABEL:
            store.revoke(entry["token_id"])

    async def _run_skill_audit(self, connection: Any, name: str) -> None:
        """Stream an on-demand LLM audit of a skill (quarantined OR active):
        reasoning deltas on ``audit:<name>``, then a terminal ``skill_audit_done``
        with the outcome. Quarantine persists .scan.json; an active skill persists
        a review (Revisada) when the judge does not confirm dangerous."""
        import json as _json

        from durin.agent import skills_store as ss
        from durin.agent.skills_surface import _skill_dirs
        from durin.memory.llm_invoke import judge_llm_invoke_astream
        from durin.providers.base import LLMProvider
        from durin.security.skill_judge import JudgeError, judge_skill_astream
        from durin.security.skill_scan import ScanReport, scan_skill

        chat_id = f"audit:{name}"
        workspace = self._endpoint_workspace()
        qdir = Path(workspace) / ".durin" / "import-quarantine" / name
        is_quarantine = (qdir / "SKILL.md").is_file()
        if is_quarantine:
            target = qdir
        else:
            target = _skill_dirs(Path(workspace)).get(name)
            if target is None or not (target / "SKILL.md").is_file():
                await self._send_event(connection, "skill_audit_done", chat_id=chat_id,
                                       name=name, judged=False, error_code="not_found")
                return
        _, model, max_sev = ss._import_judge()
        det = scan_skill(target)

        async def on_reasoning(text: str) -> None:
            await self.send_reasoning_delta(chat_id, text)

        try:
            outcome = await judge_skill_astream(
                target, ainvoke_stream=judge_llm_invoke_astream,
                model=model or "", max_severity=max_sev, on_reasoning=on_reasoning,
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
        verdict = merged.verdict
        if is_quarantine:
            source = name
            sj = qdir / ".scan.json"
            if sj.is_file():
                try:
                    source = _json.loads(sj.read_text()).get("source", name)
                except Exception:  # noqa: BLE001
                    pass
            ss._persist_judge_result(qdir, source, verdict, findings, outcome.summary)
        else:
            # Ack what the inventory reports: a provenance-pinned verdict adds a
            # synthetic finding the recorded review must cover to stay valid.
            from durin.agent.skills_surface import apply_provenance_verdict
            verdict, findings = apply_provenance_verdict(target, verdict, findings)
            ss.record_review_from_judge(
                Path(workspace), name, target, judge_verdict=outcome.verdict,
                merged_findings=findings, summary=outcome.summary, original=verdict)
        await self._send_event(connection, "skill_audit_done", chat_id=chat_id, name=name,
                               judged=True, verdict=verdict, findings=findings,
                               summary=outcome.summary)

    @staticmethod
    def _spawn_gateway_restart() -> None:
        import subprocess
        import sys

        # Detached so it survives this process being killed by the restart.
        subprocess.Popen(
            [sys.executable, "-m", "durin", "gateway", "restart"],
            start_new_session=True,
        )

    def _try_append_webui_transcript(self, chat_id: str, wire: dict[str, Any]) -> None:
        # enqueue serializes immediately (freezing the payload's current
        # state, which the old dumps→loads deep copy existed for) and never
        # raises; the writer batches the disk fsyncs off the event loop.
        get_transcript_writer().enqueue(f"websocket:{chat_id}", wire)

    async def _echo_user_message(
        self,
        chat_id: str,
        user_obj: dict[str, Any],
        media: list[str] | None,
        client_msg_id: Any,
    ) -> None:
        """Show a user message live to everyone watching the conversation.

        A conversation can be driven from the API or from another tab, and a
        watcher must see the question, not only the answer. The sender's own
        client already shows the message it sent, so the frame carries the
        ``client_msg_id`` it reconciles by; a message without one (the
        webui's ``/stop``) has nothing to reconcile with and is not echoed."""
        if not isinstance(client_msg_id, str) or not client_msg_id:
            return
        frame: dict[str, Any] = {
            "event": "user",
            "chat_id": chat_id,
            "text": user_obj.get("text", ""),
            "client_msg_id": client_msg_id[:64],
        }
        if user_obj.get("origin"):
            frame["origin"] = user_obj["origin"]
        if media:
            media_urls = self._augment_transcript_user_media(list(media))
            if media_urls:
                frame["media_urls"] = media_urls
        raw = json.dumps(frame, ensure_ascii=False)
        for connection in list(self._subs.get(chat_id, ())):
            await self._safe_send_to(connection, raw, label=" user ")

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
            # Who sent it: a message from an API token is labelled as such, so
            # the conversation does not present a program's text as the
            # person's own words.
            if meta.get("origin"):
                user_obj["origin"] = meta["origin"]
            self._try_append_webui_transcript(chat_id, user_obj)
            await self._echo_user_message(chat_id, user_obj, media, meta.get("client_msg_id"))
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

    def _write_and_sign_audio(self, audio: Any) -> str | None:
        if not audio.data:
            return None
        media_dir = get_media_dir("websocket")
        path = media_dir / f"voice-{uuid.uuid4().hex[:12]}.{audio.format}"
        try:
            path.write_bytes(audio.data)
        except OSError as e:
            self.logger.warning("failed to write voice audio: {}", e)
            return None
        return self._sign_media_path(path)

    async def _send_voice_audio(self, chat_id: str, url: str, mime: str) -> None:
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        raw = json.dumps(
            {"event": "voice_audio", "chat_id": chat_id, "url": url, "mime": mime},
            ensure_ascii=False,
        )
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" voice_audio ")

    async def _speak(self, chat_id: str, text: str, *, full: bool = False) -> None:
        svc = getattr(self, "speech_synthesis", None)
        if svc is None or chat_id not in self._voice:
            self.logger.info(
                "voice: _speak skipped chat={} has_svc={} svc_enabled={} active_voice={}",
                chat_id, svc is not None, getattr(svc, "enabled", None),
                chat_id in self._voice,
            )
            return
        cfg = getattr(self, "voice_config", None)
        sr = cfg.spoken_render if cfg is not None else None
        try:
            await self.send_voice_state(chat_id, "speaking")
            from durin.voice.rendition import build_spoken_rendition, speakable_transform

            if full or sr is None:
                spoken = speakable_transform(text)
            else:
                rendition = await build_spoken_rendition(
                    text,
                    mode=sr.mode,
                    long_threshold_words=sr.long_threshold_words,
                    pointer=sr.pointer,
                )
                spoken = rendition.spoken
            audio = await svc.synthesize(spoken)
            url = self._write_and_sign_audio(audio)
            self.logger.info(
                "voice: _speak chat={} full={} spoken_len={} audio_bytes={} sent={}",
                chat_id, full, len(spoken), len(audio.data), url is not None,
            )
            if url is not None:
                await self._send_voice_audio(chat_id, url, mime="audio/wav")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.warning("voice synthesis failed: {}", e)
        finally:
            if chat_id in self._voice:
                await self.send_voice_state(chat_id, "listening")

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

    def media_fetch(self, sig: str, payload: str) -> tuple[bytes, str, list[tuple[str, str]]]:
        """Serve a single media file previously signed via :meth:`_sign_media_path`.

        Validates the signature, decodes the payload to a relative path, and
        returns ``(body, content_type, headers)`` with a long-lived immutable
        cache (the URL already encodes the file identity). Raises DomainError on a
        bad signature (401), bad payload (422), or missing file (404) — the
        adapter renders those as problem+json.
        """
        try:
            provided_mac = _b64url_decode(sig)
        except (ValueError, binascii.Error) as exc:
            raise UnauthenticatedError("invalid media signature") from exc
        expected_mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(expected_mac, provided_mac):
            raise UnauthenticatedError("invalid media signature")
        try:
            rel_str = _b64url_decode(payload).decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise ValidationFailedError("invalid media payload") from exc
        # An attacker who somehow bypassed the HMAC check would still need
        # the resolved path to escape the media root; guard defensively.
        try:
            media_root = get_media_dir().resolve()
            candidate = (media_root / rel_str).resolve()
            candidate.relative_to(media_root)
        except (OSError, ValueError) as exc:
            raise NotFoundError("media not found") from exc
        if not candidate.is_file():
            raise NotFoundError("media not found")
        try:
            body = candidate.read_bytes()
        except OSError as exc:
            raise DomainError("media read error") from exc
        mime, _ = mimetypes.guess_type(candidate.name)
        if mime not in _MEDIA_ALLOWED_MIMES:
            mime = "application/octet-stream"
        return body, mime, [
            ("Cache-Control", "private, max-age=31536000, immutable"),
            # Paired with the MIME whitelist above: prevents browsers from
            # MIME-sniffing an octet-stream fallback into executable HTML.
            ("X-Content-Type-Options", "nosniff"),
        ]

    def _ws_auth(self, query: dict[str, list[str]], headers: Any = None) -> str | None:
        """Which credential opened a WebSocket handshake; None when refused.

        ``"webui"``: a single-use token ``/webui/bootstrap`` minted — the
        dashboard session, the one connection whose Approve / Reject decides
        an approval. ``"static"``: the configured static token. ``"anonymous"``:
        no valid token, allowed because none is required. Whenever no token is
        required, an anonymous handshake must also come from this machine: a
        loopback ``Host``, and a loopback ``Origin`` host when the client sends
        one — whether or not a setup secret is configured, since a secret only
        changes how a *token* is obtained and an anonymous connection carries
        none. Otherwise a page in the local browser could open the socket and
        chat with the agent, through DNS rebinding or by connecting to
        ``ws://127.0.0.1`` from its own site. *headers* are the handshake's
        request headers (none: the anonymous check fails). Called by the
        Starlette WebSocket endpoint (``chat_ws_endpoint`` in
        ``durin/api/asgi.py``).
        Side-effect: consumes a single-use issued token when one is accepted.
        """
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return "static"
            if supplied and self._take_issued_token_if_valid(supplied):
                return "webui"
            return None

        if self.config.websocket_requires_token:
            if supplied and self._take_issued_token_if_valid(supplied):
                return "webui"
            return None

        if supplied and self._take_issued_token_if_valid(supplied):
            return "webui"
        request_headers = headers if headers is not None else {}
        if not (_host_is_loopback(request_headers) and _origin_is_loopback(request_headers)):
            return None
        return "anonymous"

    def _ws_auth_ok(self, query: dict[str, list[str]], headers: Any = None) -> bool:
        """Return True if the WebSocket handshake is authorised (``_ws_auth``)."""
        return self._ws_auth(query, headers) is not None

    async def start(self) -> None:
        from durin.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")

        self._running = True

    async def _run_connection(self, adapter: Any, client_id: str) -> None:
        """Transport-agnostic chat loop.

        Accepts a pre-built ConnectionAdapter (websockets or Starlette-backed)
        and a pre-parsed client_id string.  Called by:
        - ``_connection_loop``  — websockets path (step 1).
        - The Starlette WebSocket endpoint  — Starlette path (step 3).
        """
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = str(uuid.uuid4())

        try:
            await adapter.send_text(
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
            self._conn_default[adapter] = default_chat_id
            self._attach(adapter, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in adapter:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(adapter, client_id, envelope)
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
                    metadata={"remote": adapter.remote},
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            self._cleanup_connection(adapter)

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
        audio_count = 0
        document_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _AUDIO_MIME_ALLOWED:
                audio_count += 1
            elif mime in _DOCUMENT_MIME_ALLOWED:
                document_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"
        if audio_count > _MAX_AUDIO_PER_MESSAGE:
            return [], "too_many_audios"
        if document_count > _MAX_DOCUMENTS_PER_MESSAGE:
            return [], "too_many_documents"

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
            is_audio = mime in _AUDIO_MIME_ALLOWED
            is_document = mime in _DOCUMENT_MIME_ALLOWED
            if is_video:
                max_bytes = _MAX_VIDEO_BYTES
            elif is_audio:
                max_bytes = _MAX_AUDIO_BYTES
            elif is_document:
                max_bytes = _MAX_DOCUMENT_BYTES
            else:
                max_bytes = _MAX_IMAGE_BYTES
            # Documents dispatch on their suffix (convert_to_markdown /
            # memory_ingest), so the saved file must keep the original
            # extension — the client name supplies it.
            name_hint = item.get("name") if is_document else None
            try:
                saved = save_base64_data_url(
                    data_url, media_dir, max_bytes=max_bytes,
                    filename_hint=name_hint,
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

    def validate_chat_message(
        self, chat_id: object, content: object, raw_media: object,
    ) -> list[str]:
        """Check one chat message and save its media; return the media paths.

        Shared by the WebSocket ``message`` frame and the HTTP send route, so
        both accept exactly the same messages. Failures raise
        ``ValidationFailedError`` whose ``details`` carry the same tokens the
        WebSocket reports in its ``error`` frames."""
        if not _is_valid_chat_id(chat_id):
            raise ValidationFailedError("invalid chat_id", details={"detail": "invalid chat_id"})
        if not isinstance(content, str):
            raise ValidationFailedError("missing content", details={"detail": "missing content"})
        media_paths: list[str] = []
        if raw_media is not None:
            if not isinstance(raw_media, list):
                raise ValidationFailedError(
                    "image_rejected", details={"detail": "image_rejected", "reason": "malformed"},
                )
            media_paths, reason = self._save_envelope_media(raw_media)
            if reason is not None:
                raise ValidationFailedError(
                    "image_rejected", details={"detail": "image_rejected", "reason": reason},
                )
        # Image-only turns are allowed (content may be empty when media is attached).
        if not content.strip() and not media_paths:
            raise ValidationFailedError("missing content", details={"detail": "missing content"})
        return media_paths

    async def publish_chat_message(
        self,
        *,
        sender_id: str,
        chat_id: str,
        content: str,
        media_paths: list[str],
        remote: Any = None,
        webui: bool = False,
        steer: bool = False,
        client_msg_id: str | None = None,
        origin: str | None = None,
    ) -> None:
        """Hand one validated chat message to the agent (see ``validate_chat_message``).

        ``origin`` names a non-person sender (``"api"`` for an API token); the
        agent treats a turn with such input as having no person to approve
        privileged actions."""
        metadata: dict[str, Any] = {"remote": remote}
        if webui:
            metadata["webui"] = True
        # Mid-turn semantics: a steer injects into the running turn;
        # a plain message defers until the turn's final response.
        if steer:
            metadata["steer"] = True
        if client_msg_id:
            metadata["client_msg_id"] = client_msg_id[:64]
        if origin:
            metadata["origin"] = origin
        await self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=content,
            media=media_paths or None,
            metadata=metadata,
            is_dm=False,
        )

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
            try:
                media_paths = self.validate_chat_message(
                    cid, envelope.get("content"), envelope.get("media"),
                )
            except ValidationFailedError as exc:
                await self._send_event(connection, "error", **exc.details)
                return
            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            client_msg_id = envelope.get("client_msg_id")
            await self.publish_chat_message(
                sender_id=client_id,
                chat_id=cid,
                content=envelope["content"],
                media_paths=media_paths,
                remote=getattr(connection, "remote", None),
                webui=envelope.get("webui") is True,
                steer=envelope.get("steer") is True,
                client_msg_id=client_msg_id if isinstance(client_msg_id, str) else None,
            )
            return
        if t == "audio_transcribe":
            # Store the audio, transcribe server-side, reply with
            # the transcript so the composer can insert editable text before
            # the message is sent. Keeps the existing WS pattern; no polling.
            cid = envelope.get("chat_id")
            request_id = envelope.get("request_id")
            raw_media = envelope.get("media")
            # Rejections MUST be sent as ``audio_transcript`` keyed by
            # ``request_id`` — the composer correlates transcriptions by
            # request_id, so a bare ``error`` event leaves the chip spinning
            # forever instead of surfacing the failure.
            if not isinstance(raw_media, list) or not raw_media:
                await self._send_event(
                    connection, "audio_transcript",
                    chat_id=cid, request_id=request_id, name=None,
                    transcript="", error="missing_media",
                )
                return
            paths, reason = self._save_envelope_media(raw_media)
            if reason is not None:
                first = raw_media[0] if isinstance(raw_media[0], dict) else {}
                await self._send_event(
                    connection, "audio_transcript",
                    chat_id=cid, request_id=request_id,
                    name=first.get("name"),
                    transcript="", error=reason,
                )
                return
            service = getattr(self, "transcription", None)
            for item, path in zip(raw_media, paths):
                name = item.get("name") or Path(path).name if isinstance(item, dict) else Path(path).name
                if service is None:
                    await self._send_event(
                        connection, "audio_transcript",
                        chat_id=cid, request_id=request_id, name=name,
                        transcript="", error="disabled",
                    )
                    continue
                async def _emit_status(phase, done=0, total=0, *, _name=name):
                    try:
                        await self._send_event(
                            connection, "audio_transcript",
                            chat_id=cid, request_id=request_id, name=_name,
                            transcript="", status=phase,
                            **({"bytes": done, "total": total} if total else {}),
                        )
                    except Exception:
                        pass

                loop = asyncio.get_running_loop()

                def on_status(phase, done=0, total=0):
                    # provider may call from a worker thread; hop back to the loop
                    asyncio.run_coroutine_threadsafe(
                        _emit_status(phase, done, total), loop
                    )

                try:
                    result = await service.transcribe_and_cache(path, on_status=on_status)
                    await self._send_event(
                        connection, "audio_transcript",
                        chat_id=cid, request_id=request_id, name=name,
                        transcript=result.text,
                    )
                except Exception:
                    self.logger.exception(
                        "audio_transcribe failed for {}", path
                    )
                    try:
                        await self._send_event(
                            connection, "audio_transcript",
                            chat_id=cid, request_id=request_id, name=name,
                            transcript="", error="failed",
                        )
                    except Exception:
                        pass
            return
        if t == "secret_store":
            await self._handle_secret_store_envelope(connection, client_id, envelope)
            return
        if t == "approval_decision":
            await self._handle_approval_decision_envelope(connection, envelope)
            return
        if t == "skill_judge":
            name = envelope.get("name")
            if not isinstance(name, str) or not name:
                await self._send_event(connection, "error", detail="missing skill name")
                return
            self._attach(connection, f"audit:{name}")
            await self._run_skill_audit(connection, name)
            return
        if t == "voice_start":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            from durin.voice.session import VoiceSession

            # Idempotent: the reconnect self-heal re-sends voice_start, so reuse
            # an existing session (keeping its reply buffer + in-flight TTS)
            # instead of wiping it. Re-affirm the current state for the new
            # connection; a fresh session starts in "listening".
            sess = self._voice.get(cid)
            if sess is None:
                sess = VoiceSession(chat_id=cid)
                self._voice[cid] = sess
            await self.send_voice_state(cid, sess.state if sess.state != "idle" else "listening")
            return
        if t == "voice_stop":
            cid = envelope.get("chat_id")
            sess = self._voice.pop(cid, None)
            if sess is not None:
                sess.cancel_speak()
            await self.send_voice_state(cid, "idle")
            return
        if t == "voice_utterance":
            cid = envelope.get("chat_id")
            if cid not in self._voice:
                await self._send_event(connection, "error", detail="voice not started")
                return
            raw_media = envelope.get("media")
            service = getattr(self, "transcription", None)
            if not isinstance(raw_media, list) or not raw_media or service is None:
                await self.send_voice_state(cid, "listening")
                return
            paths, reason = self._save_envelope_media(raw_media)
            if reason is not None:
                await self._send_event(connection, "error", detail=reason)
                await self.send_voice_state(cid, "listening")
                return
            await self.send_voice_state(cid, "transcribing")
            try:
                result = await service.transcribe_and_cache(paths[0])
            except Exception as e:
                self.logger.warning("voice transcription failed: {}", e)
                await self.send_voice_state(cid, "listening")
                return
            text = (result.text or "").strip()
            if not text:
                await self.send_voice_state(cid, "listening")
                return
            await self.send_voice_state(cid, "thinking")
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=text,
                metadata={"voice": True},
                is_dm=False,
            )
            return
        if t == "voice_barge_in":
            cid = envelope.get("chat_id")
            sess = self._voice.get(cid)
            if sess is not None:
                sess.cancel_speak()
                await self.send_voice_state(cid, "listening")
            return
        if t == "voice_read_all":
            cid = envelope.get("chat_id")
            text = envelope.get("text")
            sess = self._voice.get(cid)
            if sess is None or not isinstance(text, str) or not text.strip():
                return
            sess.cancel_speak()
            sess.speak_task = asyncio.create_task(self._speak(cid, text, full=True))
            return
        if t == "voice_preview":
            svc = getattr(self, "speech_synthesis", None)
            if svc is None:
                await self._send_event(connection, "voice_preview_audio", error="tts_unavailable")
                return
            voice = envelope.get("voice")
            language = envelope.get("language")
            sample = envelope.get("text") or _voice_preview_sample(language)
            try:
                audio = await svc.synthesize(sample, voice=voice, language=language)
            except Exception as e:  # noqa: BLE001
                self.logger.warning("voice preview failed: {}", e)
                await self._send_event(connection, "voice_preview_audio", error="synthesis_failed")
                return
            url = self._write_and_sign_audio(audio) if audio.data else None
            if url is None:
                await self._send_event(connection, "voice_preview_audio", error="synthesis_failed")
                return
            await self._send_event(connection, "voice_preview_audio", url=url, mime="audio/wav")
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
        placed into the agent conversation. The write itself is delegated to
        ``SecretsService.store`` (the single source of truth); this handler owns
        only the wire framing: the ``secret_stored`` event and the agent-resume
        notification on ``chat_id``.
        """
        from durin.service.secrets import SecretStoreCommand, secret_stored_notice

        request_id = str(envelope.get("request_id") or "")

        async def _fail(detail: str) -> None:
            await self._send_event(
                connection, "secret_stored", request_id=request_id, ok=False, detail=detail
            )

        name = str(envelope.get("name") or "").strip()
        service = str(envelope.get("service") or "").strip()
        raw_scope = envelope.get("scope")
        scope = (
            [str(s).strip() for s in raw_scope if str(s).strip()]
            if isinstance(raw_scope, list)
            else []
        )
        try:
            stored = await self._services.get("secrets").store(
                SecretStoreCommand(
                    name=name,
                    value=str(envelope.get("value") or ""),
                    service=service,
                    account=str(envelope.get("account") or "").strip(),
                    description=str(envelope.get("description") or "").strip(),
                    scope=scope,
                    origin="webui",
                    rotate=bool(envelope.get("rotate")),
                ),
                Principal.local(),
            )
        except DomainError as err:
            await _fail(err.message)
            return
        except Exception as exc:  # noqa: BLE001 — disk/IO failures still owe the client an event
            await _fail(f"could not store secret: {exc}")
            return

        await self._send_event(
            connection, "secret_stored", request_id=request_id, ok=True, name=name
        )

        # Tell the agent — metadata only, never the value — so it resumes.
        cid = envelope.get("chat_id")
        if _is_valid_chat_id(cid):
            note = secret_stored_notice(stored)
            self._attach(connection, cid)
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=note,
                # Not the user's reply: a question the agent is waiting on
                # must not take this note as its answer.
                metadata={"webui": True, INBOUND_META_NOT_AN_ANSWER: True},
                is_dm=False,
            )

    def _connection_session_keys(self, connection: Any) -> set[str]:
        """Session keys of the chats *connection* is attached to, keyed the way
        the agent loop keys their turns (one shared key in unified mode)."""
        return {self._goal_state_session_key(cid) for cid in self._conn_chats.get(connection, ())}

    async def _handle_approval_decision_envelope(
        self, connection: Any, envelope: dict[str, Any],
    ) -> None:
        """Resolve an approval from an Approve / Reject click.

        The verdict never becomes a chat message, so the model can neither see
        nor forge it. Only a connection the dashboard session opened may send
        one (``_answers_approvals``): a socket opened with the static token or
        with no token is refused. The record must belong to a chat this
        connection is attached to. ``approval.decide`` hands the verdict to the turn still
        waiting on it. When that turn stopped waiting (the click came after
        its timeout), ``decide`` runs the recorded request here with the
        gateway's live handles, except an exec request, which only its own
        turn can approve: that click is refused and the record left as it
        was. That run can take minutes (an MCP install), so
        it proceeds as a background task and the socket keeps serving frames.
        The ``approval_decided`` event reports the outcome when it lands, and
        a decision the waiting turn did not take posts a note into the chat,
        since that turn was told the request waits and moved on.
        """
        import dataclasses

        from durin.agent import approval, approval_store
        from durin.agent.approval_executors import ExecDeps
        from durin.agent.approval_notify import notify_origin

        request_id = str(envelope.get("request_id") or "")
        approval_id = str(envelope.get("approval_id") or "").strip()
        decision = envelope.get("decision")

        async def _reply(status: str, message: str) -> None:
            await self._send_event(
                connection, "approval_decided", request_id=request_id,
                approval_id=approval_id, ok=status in _APPROVAL_OK,
                status=status, message=message,
            )

        if not _answers_approvals(connection):
            await _reply("refused", (
                "Deciding an approval takes a person's dashboard session; this "
                "connection was not opened with one."))
            return
        if not _APPROVAL_ID_RE.match(approval_id):
            await _reply("refused", "Invalid approval id.")
            return
        if decision not in ("approve", "reject"):
            await _reply("refused", "Invalid decision: expected approve or reject.")
            return
        workspace = self._endpoint_workspace()
        record = approval_store.get(workspace, approval_id)
        if record is None:
            await _reply("refused", f"No approval request {approval_id}.")
            return
        if record.get("requested_by_session") not in self._connection_session_keys(connection):
            await _reply("refused", "This approval belongs to another chat.")
            return
        deps = self._approval_deps() if self._approval_deps is not None else ExecDeps()
        if record.get("kind") == "exec_command":
            # An exec_command runs only inside the turn that asked for it — the
            # literal command lives solely in that turn's ExecDeps.extra, never
            # on disk. Strip the runner so an out-of-turn click cannot run a
            # DIFFERENT live command under the record's approval, even though
            # the gateway's own ExecDeps carries one for in-turn use elsewhere.
            deps = dataclasses.replace(deps, exec_run=None)

        async def _decide() -> None:
            try:
                outcome = await approval.decide(
                    workspace, approval_id, decision,
                    decided_by={"kind": "user", "channel": "websocket"}, deps=deps,
                )
            except Exception as exc:  # noqa: BLE001 — the click still owes the client an answer
                self.logger.exception("approval decision {} failed", approval_id)
                await _reply("failed", f"Could not decide {approval_id}: {exc}")
                return
            await _reply(outcome.status, outcome.message)
            # The record belongs to a chat this socket serves (checked above).
            await notify_origin(self.bus, outcome, serves=lambda channel: channel == self.name)

        task = asyncio.create_task(_decide())
        self._approval_tasks.add(task)
        task.add_done_callback(self._approval_tasks.discard)

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        approval_tasks = list(self._approval_tasks)
        for task in approval_tasks:
            task.cancel()
        if approval_tasks:
            # A decision blocked inside its executor must not abort the rest
            # of shutdown — gather it as one of possibly several cancellations.
            await asyncio.gather(*approval_tasks, return_exceptions=True)
        for task in self._voice_cleanup.values():
            task.cancel()
        self._voice_cleanup.clear()
        for task in self._answer_fallback.values():
            task.cancel()
        self._answer_fallback.clear()
        for sess in self._voice.values():
            sess.cancel_speak()
        self._voice.clear()
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._issued_tokens.clear()
        # Shutdown barrier: any buffered transcript events reach disk before
        # the process exits.
        await get_transcript_writer().aclose()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send_text(raw)
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

        # Dream progress — a global (chat_id="*") frame broadcast to every
        # connection. Checked before the per-chat subscriber lookup below
        # because it has no chat to scope to.
        if msg.metadata.get("_dream_progress"):
            payload = msg.metadata.get("dream")
            await self.send_dream_progress(payload if isinstance(payload, dict) else {})
            return

        # Concurrency snapshot — a global (chat_id="*") frame broadcast to
        # every connection. Checked before the per-chat subscriber lookup.
        if msg.metadata.get("_concurrency_snapshot"):
            payload = msg.metadata.get("snapshot")
            await self.send_concurrency_snapshot(payload if isinstance(payload, dict) else {})
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
            # Nobody watches this chat right now. Fall through anyway: live-state
            # frames are dropped by their senders, but a reply or a turn_end is
            # still persisted, so a client that reattaches rebuilds the turn from
            # the transcript.
            self.logger.debug("no active subscribers for chat_id={}", msg.chat_id)
        if msg.metadata.get("_goal_state_sync"):
            blob = msg.metadata.get("goal_state")
            blob = blob if isinstance(blob, dict) else {"active": False}
            self._release_unwatched_ask(msg.chat_id, blob)
            await self.send_goal_state(msg.chat_id, blob)
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
        if msg.metadata.get("_message_queued"):
            await self.send_message_queued(
                msg.chat_id, msg.metadata.get("client_msg_id"),
            )
            return
        if msg.metadata.get("_queued_consumed"):
            ids = msg.metadata.get("client_msg_ids")
            await self.send_queued_consumed(
                msg.chat_id, ids if isinstance(ids, list) else [],
            )
            return
        # Signal that the agent has fully finished processing the current turn.
        if msg.metadata.get("_turn_end"):
            lat = msg.metadata.get("latency_ms")
            lat_i = int(lat) if isinstance(lat, (int, float)) else None
            gs = msg.metadata.get("goal_state")
            gs_blob = gs if isinstance(gs, dict) else None
            outcome = msg.metadata.get("outcome")
            client_msg_id = msg.metadata.get("client_msg_id")
            await self.send_turn_end(
                msg.chat_id, latency_ms=lat_i, goal_state=gs_blob,
                outcome=outcome if outcome in _TURN_OUTCOMES else None,
                client_msg_id=client_msg_id if isinstance(client_msg_id, str) and client_msg_id else None,
            )
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
        # Stamp a stable server-assigned id on every non-streamed message frame.
        # Command outputs emit no turn_end, so without a persisted id the live
        # row (keyed by a client-random UUID) and the replay row (keyed from the
        # record index) differ — a later refetch wholesale-replaces the array and
        # the row "disappears" then "reappears". Because this same `payload` dict
        # is both serialized to the wire and deep-copied into the transcript, the
        # id is identical in the live frame and the persisted JSONL record, so
        # the replay builder can reuse it and the two rows merge.
        payload["id"] = f"msg-{uuid.uuid4().hex}"
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
        if not _is_live_progress_only(payload):
            self._try_append_webui_transcript(msg.chat_id, payload)
        raw = json.dumps(payload, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")
        # Voice mode: synthesize the spoken rendition for the FINAL reply only.
        final_reply = bool(
            msg.content
            and not msg.metadata.get("_tool_hint")
            and not msg.metadata.get("_progress")
        )
        if final_reply and conns and msg.chat_id in self._voice:
            sess = self._voice[msg.chat_id]
            sess.cancel_speak()
            sess.speak_task = asyncio.create_task(self._speak(msg.chat_id, msg.content))
        elif final_reply and conns and self._voice:
            # A voice session is active but this reply's chat_id isn't one of them —
            # the agent answered on a different chat than the orb is listening on
            # (the class of bug behind "voice replies only in text").
            self.logger.info(
                "voice: final reply NOT spoken — reply chat={} not in active voice {}",
                msg.chat_id, list(self._voice.keys()),
            )

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
        if not delta:
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
        # Persisted even when nobody watches: a client that reattaches rebuilds
        # the turn from the transcript, not from frames it never received.
        self._try_append_webui_transcript(chat_id, body)
        if not conns:
            return
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
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._try_append_webui_transcript(chat_id, body)
        if not conns:
            return
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
        meta = metadata or {}
        # Voice mode: the assistant reply reaches the webui as a stream of deltas,
        # NOT a single send() with content — so accumulate it here and speak the
        # full text when the stream ends (this is the real reply path; the send()
        # hook never fires for streamed replies, which is why voice stayed silent).
        # Nobody listening means nothing to speak; the text is still persisted below.
        sess = self._voice.get(chat_id) if conns else None
        if sess is not None:
            if meta.get("_stream_end"):
                spoken_text = sess.take_reply()
                self.logger.info(
                    "voice: stream_end chat={} reply_len={}", chat_id, len(spoken_text)
                )
                if spoken_text.strip():
                    sess.cancel_speak()
                    sess.speak_task = asyncio.create_task(self._speak(chat_id, spoken_text))
            elif delta:
                sess.reply_buffer.append(delta)
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
        if not conns:
            return
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
        outcome: str | None = None,
        client_msg_id: str | None = None,
    ) -> None:
        """Signal that the agent has fully finished processing the current turn.

        ``outcome`` says how it ended (``completed``, ``stopped``, ``failed``);
        ``client_msg_id`` names the message that opened the turn, so a client
        waiting on its own message can tell this turn from an earlier one."""
        conns = list(self._subs.get(chat_id, ()))
        body: dict[str, Any] = {"event": "turn_end", "chat_id": chat_id}
        if latency_ms is not None:
            body["latency_ms"] = int(latency_ms)
        if goal_state is not None:
            body["goal_state"] = goal_state
        if outcome is not None:
            body["outcome"] = outcome
        if client_msg_id is not None:
            body["client_msg_id"] = client_msg_id
        self._try_append_webui_transcript(chat_id, body)
        if not conns:
            return
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_end ")

    async def send_message_queued(self, chat_id: str, client_msg_id: str | None) -> None:
        """Tell clients a message sent mid-turn was deferred until turn end.

        Ephemeral UI state (a "queued" chip on the message row) — not
        persisted to the transcript.
        """
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "message_queued", "chat_id": chat_id}
        if client_msg_id:
            body["client_msg_id"] = client_msg_id
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" message_queued ")

    async def send_queued_consumed(self, chat_id: str, client_msg_ids: list[str]) -> None:
        """Tell clients their queued messages entered the running turn."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "queued_consumed",
            "chat_id": chat_id,
            "client_msg_ids": client_msg_ids,
        }
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" queued_consumed ")

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """Push persisted goal-state snapshot for *chat_id* (multi-chat isolation)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body = {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_state ")

    async def send_voice_state(self, chat_id: str, state: str) -> None:
        """Push the voice-loop state (listening/thinking/speaking/idle) for a chat."""
        sess = self._voice.get(chat_id)
        if sess is not None:
            sess.state = state
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        raw = json.dumps(
            {"event": "voice_state", "chat_id": chat_id, "state": state},
            ensure_ascii=False,
        )
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" voice_state ")

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

    async def send_dream_progress(self, payload: dict[str, Any]) -> None:
        """Broadcast a dream-progress frame to every open websocket connection.

        The dashboard listens for these to drive the Dream view's live feed and
        its "running" indicator while a (manually triggered) dream run is in
        flight. Like :meth:`send_runtime_model_updated`, it fans out to all
        connections, not a single chat's subscribers.
        """
        conns = list(self._conn_chats)
        if not conns:
            return
        body: dict[str, Any] = {"event": "dream_progress", **payload}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" dream_progress ")

    async def send_concurrency_snapshot(self, payload: dict[str, Any]) -> None:
        """Broadcast a concurrency snapshot to every open websocket connection.

        Drives the sidebar saturation chip, the Concurrency settings card's live
        readouts, and the global Work panel. Like :meth:`send_dream_progress`,
        fans out to all connections. Returns early when none are open — the
        subscriber gate that keeps the monitor free when nobody is watching."""
        conns = list(self._conn_chats)
        if not conns:
            return
        body: dict[str, Any] = {"event": "concurrency_snapshot", **payload}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" concurrency_snapshot ")
