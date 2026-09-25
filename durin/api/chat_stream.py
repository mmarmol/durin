"""SSE transport for webui conversations.

``GET /api/v1/sessions/{key}/events`` streams what a WebSocket watcher of the
same chat receives: an ``SseSubscriber`` joins the websocket channel's per-chat
fan-out, so the frames are identical and nothing is re-implemented.

``POST /api/v1/sessions/{key}/messages`` is mounted here too, ahead of the
generic ``/api/v1`` route for the same path, because it has a streaming form:
with ``Accept: text/event-stream`` it subscribes, delivers the message, and
streams until the turn answering that message ends. A dropped connection ends
only the stream, never the turn. Without that header it answers ``202`` exactly
like the contract route.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from durin.service.principal import Principal, Scope
from durin.service.types import (
    DomainError,
    ForbiddenError,
    UnauthenticatedError,
    ValidationFailedError,
)

SSE_BUFFER_LIMIT_BYTES = 8 * 1024 * 1024
SSE_KEEPALIVE_S = 15.0

# Text previews: dropping some only costs a client a partial preview, which it
# reconciles from the transcript at turn_end. Every other frame is a state
# change a client must not miss, so running out of room for one ends the stream.
_DROPPABLE = frozenset({"delta", "reasoning_delta"})
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class SseSubscriber:
    """One SSE watcher of a chat, fed by the websocket channel's fan-out.

    ``send_text`` never blocks and never raises: the channel awaits its sends
    one after another, so a slow reader here must not stall the turn or the
    other watchers. Frames wait in a byte-bounded buffer; when it is full, text
    previews are dropped first, and if a state frame still does not fit the
    stream ends with ``lagged`` (the client reattaches and catches up from the
    transcript). Voice frames belong to the WebSocket voice mode and are
    filtered out.
    """

    # The client may answer a question with a plain message, but a message
    # from the API never decides an approval: the channel does not count this
    # watcher as holding a turn that waits on one.
    answers_approvals = False

    def __init__(self, *, limit_bytes: int = SSE_BUFFER_LIMIT_BYTES, remote: Any = None) -> None:
        self._frames: deque[tuple[str, dict[str, Any], bytes]] = deque()
        self._bytes = 0
        self._limit = limit_bytes
        self._ended = False
        self._wakeup = asyncio.Event()
        self.remote = remote

    async def send_text(self, raw: str) -> None:
        if self._ended:
            return
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        name = frame.get("event") if isinstance(frame, dict) else None
        if not isinstance(name, str) or name.startswith("voice_"):
            return
        chunk = f"event: {name}\ndata: {raw}\n\n".encode()
        if self._bytes + len(chunk) > self._limit:
            if name in _DROPPABLE:
                return
            self._ended = True
            lagged = json.dumps({"event": "lagged", "chat_id": frame.get("chat_id")})
            self._frames.append(("lagged", {}, f"event: lagged\ndata: {lagged}\n\n".encode()))
        else:
            self._frames.append((name, frame, chunk))
            self._bytes += len(chunk)
        self._wakeup.set()

    async def frames(
        self, stop_after: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield SSE chunks until ``lagged``, or until ``stop_after(name, frame)``
        is true (after yielding that frame). An idle stream yields a comment
        line so proxies and read timeouts don't take it for dead."""
        while True:
            self._wakeup.clear()
            while self._frames:
                name, frame, chunk = self._frames.popleft()
                if name != "lagged":
                    self._bytes -= len(chunk)
                yield chunk
                if name == "lagged" or (stop_after is not None and stop_after(name, frame)):
                    return
            try:
                async with asyncio.timeout(SSE_KEEPALIVE_S):
                    await self._wakeup.wait()
            except TimeoutError:
                yield b": keepalive\n\n"


def _problem(err: DomainError) -> Response:
    from durin.api.asgi import _problem_response

    return _problem_response(err)


def _too_large(limit: int) -> Response:
    body = json.dumps({
        "type": "urn:durin:error:payload_too_large",
        "title": "Payload Too Large",
        "status": 413,
        "detail": f"message body exceeds {limit} bytes",
    })
    return Response(body, status_code=413, media_type="application/problem+json")


def build_chat_stream_routes(
    channel: Any,
    registry: Any,
    *,
    resolve_principal: Callable[[Any], Principal | None],
) -> list[Route]:
    """Routes for watching webui conversations over SSE and the send route's
    streaming form. List them ahead of the generic ``/api/v1`` table (first
    match wins)."""
    from durin.service.chat import ChatSendCommand, webui_chat_id, with_client_msg_id

    def _authorize(request: Request, *scopes: Scope) -> Principal | Response:
        principal = resolve_principal(request.headers)
        if principal is None:
            return _problem(UnauthenticatedError("Missing or invalid bearer token"))
        for scope in scopes:
            if not principal.has_scope(scope):
                return _problem(ForbiddenError(f"token lacks required scope: {scope.value}"))
        return principal

    def _stream(
        chat_id: str,
        sub: SseSubscriber,
        stop_after: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> StreamingResponse:
        """Drain *sub* (already attached to *chat_id*) and detach it when the
        stream ends, whether it finished or the client left."""

        async def _gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in sub.frames(stop_after):
                    yield chunk
            finally:
                channel._cleanup_connection(sub)

        return StreamingResponse(_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def events(request: Request) -> Response:
        auth = _authorize(request, Scope.SESSIONS_READ)
        if isinstance(auth, Response):
            return auth
        try:
            chat_id = webui_chat_id(request.path_params["key"])
        except DomainError as exc:
            return _problem(exc)
        sub = SseSubscriber(remote=getattr(request, "client", None))
        channel._attach(sub, chat_id)
        try:
            # What a reattaching webui gets: goal state, a turn in flight.
            await channel._hydrate_after_subscribe(chat_id)
        except BaseException:
            channel._cleanup_connection(sub)
            raise
        return _stream(chat_id, sub)

    async def send(request: Request) -> Response:
        wants_stream = "text/event-stream" in request.headers.get("accept", "")
        scopes = (Scope.CHAT_WRITE, Scope.SESSIONS_READ) if wants_stream else (Scope.CHAT_WRITE,)
        auth = _authorize(request, *scopes)
        if isinstance(auth, Response):
            return auth
        # Same ceiling as a WebSocket frame, before the body is read.
        limit = getattr(getattr(channel, "config", None), "max_message_bytes", None)
        declared = request.headers.get("content-length")
        if limit is not None and declared is not None and declared.isdigit() and int(declared) > limit:
            return _too_large(limit)
        raw = await request.body()
        if limit is not None and len(raw) > limit:
            return _too_large(limit)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return _problem(ValidationFailedError("body must be a JSON object"))
        if not isinstance(body, dict):
            return _problem(ValidationFailedError("body must be a JSON object"))
        try:
            cmd = ChatSendCommand.model_validate({**body, "key": request.path_params["key"]})
        except ValidationError as exc:
            from durin.api.asgi import _build_422

            return _build_422(exc)
        service = registry.get("chat")

        if not wants_stream:
            try:
                result = await service.send(cmd, auth)
            except DomainError as exc:
                return _problem(exc)
            return JSONResponse(result.model_dump(), status_code=202)

        # A command answers inline and opens no turn, so nothing would end the
        # stream; commands go through the plain form.
        if cmd.content.lstrip().startswith("/"):
            return _problem(ValidationFailedError(
                "commands answer without a turn; send them without Accept: text/event-stream",
            ))
        cmd = with_client_msg_id(cmd)
        try:
            chat_id, media_paths = service.check(cmd, auth)
        except DomainError as exc:
            return _problem(exc)

        mine = cmd.client_msg_id
        consumed = False

        def _answered(name: str, frame: dict[str, Any]) -> bool:
            # The turn answering this message is the one it opened (turn_end
            # carries the opener's client_msg_id) or, when it was queued behind
            # a running turn, the turn that consumed it. A steer joins the
            # running turn, which is the next to end.
            nonlocal consumed
            if name == "queued_consumed" and mine in (frame.get("client_msg_ids") or []):
                consumed = True
                return False
            if name != "turn_end":
                return False
            return cmd.steer or consumed or frame.get("client_msg_id") == mine

        sub = SseSubscriber(remote=getattr(request, "client", None))
        channel._attach(sub, chat_id)
        try:
            await channel._hydrate_after_subscribe(chat_id)
            # Delivered before the response starts: a client that leaves right
            # away loses the stream, never the message.
            await service.deliver(cmd, auth, chat_id, media_paths)
        except BaseException:
            channel._cleanup_connection(sub)
            raise
        return _stream(chat_id, sub, _answered)

    return [
        Route("/api/v1/sessions/{key}/events", events, methods=["GET"]),
        Route("/api/v1/sessions/{key}/messages", send, methods=["POST"]),
    ]
