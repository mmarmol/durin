"""SessionsService — list, read, delete, and rename sessions across all channels.

Wraps durin's ``SessionManager``.  Returns unsigned data only: the
``_handle_session_messages`` and ``_handle_webui_thread_get`` shims apply
``_augment_media_urls`` / ``_augment_transcript_user_media`` after the call
(HMAC-signed URLs are an adapter concern that must not live in a service).

Extracted from ``durin/channels/websocket.py`` in SP1; the channel keeps
wire-identical shims.

``dict[str, Any]`` escape hatches
----------------------------------
``SessionMessagesResult.data`` and ``WebuiThreadResult.data`` are typed as
``dict[str, Any]`` because the session JSONL payload (message lists, tool
events, metadata) is a large dynamic structure defined by the session manager.
These can tighten to a proper submodel if the wire shape is ever frozen.
"""

from __future__ import annotations

from typing import Any

from durin.service.principal import Principal, Scope
from durin.service.registry import route
from durin.service.types import (
    Command,
    ConflictError,
    NotFoundError,
    Query,
    Result,
    UnavailableError,
    ValidationFailedError,
)
from durin.session.turn_lease import session_turn_lease

# ---------------------------------------------------------------------------
# DTOs — sessions list
# ---------------------------------------------------------------------------


class SessionsListQuery(Query):
    """No inputs — lists all sessions across every channel."""


class SessionsListResult(Result):
    sessions: list[dict[str, Any]]  # escape hatch — dynamic per-session metadata


# ---------------------------------------------------------------------------
# DTOs — session messages
# ---------------------------------------------------------------------------


class SessionMessagesQuery(Query):
    key: str


class SessionMessagesResult(Result):
    data: dict[str, Any]  # escape hatch — full session JSONL payload (open by design)


# ---------------------------------------------------------------------------
# DTOs — webui thread
# ---------------------------------------------------------------------------


class WebuiThreadQuery(Query):
    key: str
    before: int | None = None


class WebuiThreadResult(Result):
    data: dict[str, Any]  # escape hatch — webui thread response dict (open by design)


# ---------------------------------------------------------------------------
# DTOs — session delete
# ---------------------------------------------------------------------------


class SessionDeleteCommand(Command):
    key: str


class SessionDeleteResult(Result):
    deleted: bool


# ---------------------------------------------------------------------------
# DTOs — session rename
# ---------------------------------------------------------------------------


class SessionRenameCommand(Command):
    key: str
    title: str


class SessionRenameResult(Result):
    title: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SessionsService:
    """Read and mutate sessions across all channels.

    ``session_manager`` is the live ``SessionManager`` instance (from the
    gateway's ``_session_manager``).  It may be ``None`` when tests construct
    the service without a real manager; the service raises ``UnavailableError``
    in that case (matching the original handler behaviour).

    Media signing (``_augment_media_urls``, ``_augment_transcript_user_media``)
    is NOT performed here — the shim applies it after receiving the unsigned
    result, because it needs the channel's per-process ``_media_secret``.
    """

    def __init__(self, session_manager: Any | None = None) -> None:
        self._session_manager = session_manager

    def _require_manager(self) -> Any:
        if self._session_manager is None:
            raise UnavailableError("session manager unavailable")
        return self._session_manager

    @route(
        "GET",
        "/api/v1/sessions",
        scope=Scope.SESSIONS_READ.value,
        request_model=SessionsListQuery,
        response_model=SessionsListResult,
        summary="List all sessions across channels (path field stripped, channel field added)",
    )
    async def list(
        self, query: SessionsListQuery, principal: Principal
    ) -> SessionsListResult:
        principal.require(Scope.SESSIONS_READ)
        sm = self._require_manager()
        sessions = sm.list_sessions()
        cleaned = []
        for s in sessions:
            key = s.get("key")
            if not isinstance(key, str):
                continue
            # Derive the channel from the key prefix (e.g. "telegram:123" → "telegram").
            colon = key.find(":")
            channel = key[:colon] if colon != -1 else ""
            row = {k: v for k, v in s.items() if k != "path"}
            row["channel"] = channel
            cleaned.append(row)
        return SessionsListResult(sessions=cleaned)

    @route(
        "GET",
        "/api/v1/sessions/{key}/messages",
        scope=Scope.SESSIONS_READ.value,
        request_model=SessionMessagesQuery,
        response_model=SessionMessagesResult,
        summary="Return unsigned session JSONL payload (shim applies media signing)",
    )
    async def messages(
        self, query: SessionMessagesQuery, principal: Principal
    ) -> SessionMessagesResult:
        """Return the raw (unsigned) session data.

        The shim must call ``_augment_media_urls(result.data)`` before sending
        the response so that media paths become signed fetch URLs.
        """
        principal.require(Scope.SESSIONS_READ)
        sm = self._require_manager()
        data = sm.read_session_file(query.key)
        if data is None:
            raise NotFoundError("session not found", details={"key": query.key})
        from durin.utils.subagent_channel_display import scrub_subagent_messages_for_channel

        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        return SessionMessagesResult(data=data)

    @route(
        "GET",
        "/api/v1/sessions/{key}/webui-thread",
        scope=Scope.SESSIONS_READ.value,
        request_model=WebuiThreadQuery,
        response_model=WebuiThreadResult,
        summary="Validate key; shim calls build_webui_thread_response with signing callback",
    )
    async def webui_thread(
        self, query: WebuiThreadQuery, principal: Principal
    ) -> WebuiThreadResult:
        """Validate the key; raise if invalid.  Return a sentinel empty result.

        The signing callback (``_augment_transcript_user_media``) HMAC-signs
        file paths using the channel's per-process ``_media_secret``.  It must
        be passed directly to ``build_webui_thread_response``, which cannot be
        done from a pure service method.  The shim therefore:

        1. Calls this method to validate the key and enforce scope.
        2. Discards the sentinel result.
        3. Calls ``build_webui_thread_response(decoded_key,
           augment_user_media=self._augment_transcript_user_media)`` directly.
        4. Returns the signed payload as the JSON response.

        This mirrors the cron ``run`` pattern: service decides + validates,
        shim performs the adapter-specific side effect.
        """
        principal.require(Scope.SESSIONS_READ)
        # Sentinel — actual data is built by the shim (see docstring above).
        return WebuiThreadResult(data={})

    @route(
        "DELETE",
        "/api/v1/sessions/{key}",
        scope=Scope.SESSIONS_WRITE.value,
        request_model=SessionDeleteCommand,
        response_model=SessionDeleteResult,
        summary="Delete a session and its webui thread",
    )
    async def delete(
        self, cmd: SessionDeleteCommand, principal: Principal
    ) -> SessionDeleteResult:
        principal.require(Scope.SESSIONS_WRITE)
        sm = self._require_manager()
        deleted = sm.delete_session(cmd.key)
        from durin.utils.webui_thread_disk import delete_webui_thread

        delete_webui_thread(cmd.key)
        return SessionDeleteResult(deleted=bool(deleted))

    @route(
        "POST",
        "/api/v1/sessions/{key}/rename",
        scope=Scope.SESSIONS_WRITE.value,
        request_model=SessionRenameCommand,
        response_model=SessionRenameResult,
        summary="Set a user-edited title for a session",
    )
    async def rename(
        self, cmd: SessionRenameCommand, principal: Principal
    ) -> SessionRenameResult:
        principal.require(Scope.SESSIONS_WRITE)
        sm = self._require_manager()
        from durin.utils.webui_titles import (
            TITLE_MAX_CHARS,
            WEBUI_TITLE_METADATA_KEY,
            WEBUI_TITLE_USER_EDITED_METADATA_KEY,
            clean_generated_title,
        )

        title = clean_generated_title(cmd.title)
        if not title:
            raise ValidationFailedError("title is required")
        if len(title) > TITLE_MAX_CHARS:
            title = title[: TITLE_MAX_CHARS - 1].rstrip() + "…"
        if not sm.exists(cmd.key):
            raise NotFoundError("session not found", details={"key": cmd.key})

        # Acquire the per-session turn lease before writing so a concurrent
        # agent turn cannot clobber the rename's whole-file write (and vice
        # versa).  Reload under the lease to pick up any turns that committed
        # between the exists() check and now.  On TimeoutError (session busy)
        # raise ConflictError — the caller retries; no partial write is applied.
        # Reload under the lease to pick up any commits between exists() and now.
        session_path = sm._get_session_path(cmd.key)
        try:
            async with session_turn_lease(session_path, timeout=30.0):
                session = sm.reload(cmd.key)
                session.metadata[WEBUI_TITLE_METADATA_KEY] = title
                session.metadata[WEBUI_TITLE_USER_EDITED_METADATA_KEY] = True
                sm.save(session)
        except TimeoutError as exc:
            raise ConflictError(
                "session is busy, rename not applied",
                details={"key": cmd.key},
            ) from exc
        return SessionRenameResult(title=title)
