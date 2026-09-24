"""OpenAI-compatible ``/v1`` routes for the gateway HTTP app.

``POST /v1/chat/completions`` and ``GET /v1/models``, bearer-gated by the
``chat:write`` scope. The contract is deliberately session-oriented: exactly
one user message per request; conversation history lives server-side under
``api:{session_id}`` session keys (``api:default`` when no id is sent), so a
standard OpenAI client works by setting ``base_url`` to the gateway and using
a durin token as its ``api_key``.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from durin.config.paths import get_media_dir
from durin.service.principal import Principal, Scope
from durin.utils.helpers import safe_filename
from durin.utils.media_decode import (
    MAX_FILE_SIZE,
    FileSizeExceeded,
    save_base64_data_url,
)
from durin.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

__all__ = ("build_openai_routes",)

API_SESSION_PREFIX = "api:"
API_DEFAULT_SESSION_KEY = "api:default"
API_CHAT_ID = "default"

# Client-side tool calling is not part of this surface: durin runs its own
# tools server-side inside the turn. Rejecting loudly beats the silent-ignore
# a caller would misread as "tools accepted".
_UNSUPPORTED_TOOL_FIELDS = ("tools", "tool_choice", "functions", "function_call")

_SSE_DONE = b"data: [DONE]\n\n"

# While a tool runs (or the turn waits its turn on the session) no content
# flows; proxies and HTTP clients read a long-silent connection as dead and cut
# it, which would cancel the turn. An SSE comment line keeps bytes moving and
# is ignored by every conforming SSE parser.
_SSE_KEEPALIVE = b": keepalive\n\n"
_SSE_KEEPALIVE_S = 15.0


def _error_json(
    status: int, message: str, err_type: str = "invalid_request_error"
) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": err_type, "code": status}},
        status_code=status,
    )


def _chat_completion_response(
    content: str, model: str, usage: dict[str, int]
) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def _response_text(value: Any) -> str:
    """Normalize ``process_direct`` output (OutboundMessage | str | None) to text."""
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)


def _response_usage(value: Any) -> dict[str, int]:
    """Shape ``process_direct`` output into the OpenAI 3-key usage contract.

    The agent loop accumulates real per-turn token counts across every LLM
    call in ``OutboundMessage.metadata["usage"]`` (see ``_assemble_outbound``
    in durin/agent/loop.py). ``total_tokens`` is always recomputed as the sum
    here rather than trusted from upstream, per the OpenAI contract. Missing
    or malformed metadata (a bare string response, a fake with no usage)
    reports zero instead of raising.
    """
    metadata = getattr(value, "metadata", None) or {}
    usage = metadata.get("usage") or {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _add_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Sum two usage dicts shaped by ``_response_usage``."""
    prompt = a["prompt_tokens"] + b["prompt_tokens"]
    completion = a["completion_tokens"] + b["completion_tokens"]
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _sse_chunk(
    delta: str,
    model: str,
    chunk_id: str,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    # OpenAI convention: usage rides the terminal chunk. This gateway doesn't
    # parse stream_options.include_usage yet, so it always includes it here
    # rather than gating on an opt-in the caller has no way to send.
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n".encode()


def _parse_json_content(body: dict) -> tuple[str, list[str]]:
    """Validate the OpenAI body and return ``(text, media_paths)``.

    Raises ValueError on contract violations (single-user-message rule,
    client-side tools, remote image URLs, malformed content).
    """
    for field in _UNSUPPORTED_TOOL_FIELDS:
        if body.get(field):
            raise ValueError(
                "durin runs its own tools server-side; "
                "client-defined tools are not supported"
            )
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Only a single user message is supported")
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        raise ValueError("Only a single user message is supported")

    user_content = message.get("content", "")
    media_dir = get_media_dir("api")
    media_paths: list[str] = []

    if isinstance(user_content, list):
        text_parts: list[str] = []
        for part in user_content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    saved = save_base64_data_url(url, media_dir)
                    if saved:
                        media_paths.append(saved)
                elif url:
                    raise ValueError(
                        "Remote image URLs are not supported. Use base64 data "
                        "URLs or upload files via multipart/form-data."
                    )
        text = " ".join(text_parts)
    elif isinstance(user_content, str):
        text = user_content
    else:
        raise ValueError("Invalid content format")

    return text, media_paths


def build_openai_routes(
    agent_loop: Any,
    *,
    model_name: str,
    request_timeout: float,
    turn_timeout: float = 3600.0,
    resolve_principal: Callable[[Any], Principal | None],
) -> list[Route]:
    """Build the ``/v1`` route list for the gateway app.

    ``resolve_principal`` is injected (headers → Principal | None) so this
    module needs no import from ``asgi`` and stays independently testable.

    A turn runs in its own task and outlives the request that started it: a
    client that disconnects, or a non-streaming request that times out, gets
    no answer, but the turn finishes and is saved to its session, where the
    next request on that ``session_id`` finds it. ``turn_timeout`` bounds every
    turn; ``request_timeout`` is only how long a non-streaming request waits.
    """
    session_locks: dict[str, asyncio.Lock] = {}
    # Strong references: a detached turn must not be garbage-collected.
    running: set[asyncio.Task] = set()

    async def _no_progress(*_a: Any, **_kw: Any) -> None:
        # The OpenAI format has no place for progress. Without a callback the
        # loop would publish it for a nonexistent "api" channel, which logs a
        # warning per tool call.
        return None

    def _start_turn(
        text: str,
        media_paths: list[str],
        session_key: str,
        lock: asyncio.Lock,
        **stream_callbacks: Any,
    ) -> tuple[asyncio.Task, dict[str, Any]]:
        """Run one turn in its own task. ``state`` records whether it got its
        session (``started``), its ceiling (to tell a ceiling hit from a
        failure), and whether its request was abandoned."""
        state: dict[str, Any] = {"started": False, "ceiling": None, "abandoned": False}

        async def _turn() -> Any:
            async with lock:
                state["started"] = True
                # Built once the session lock is held: asyncio.timeout fixes its
                # deadline at construction, and queueing behind another turn on
                # the session must not spend this turn's budget.
                state["ceiling"] = asyncio.timeout(turn_timeout if turn_timeout > 0 else None)
                async with state["ceiling"]:
                    return await agent_loop.process_direct(
                        content=text,
                        media=media_paths or None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                        on_progress=_no_progress,
                        **stream_callbacks,
                    )

        task = asyncio.create_task(_turn())
        running.add(task)
        task.add_done_callback(running.discard)

        def _read_outcome(t: asyncio.Task) -> None:
            # Always retrieve the result, so a turn nobody awaits any more never
            # ends as "Task exception was never retrieved"; a failure after the
            # request gave up is logged here, since no response will carry it.
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None and state["abandoned"]:
                logger.opt(exception=exc).error(
                    "OpenAI API turn for session {} failed after its request ended",
                    session_key,
                )

        task.add_done_callback(_read_outcome)
        return task, state

    def _abandon(task: asyncio.Task, state: dict[str, Any]) -> None:
        """The request is gone. A started turn keeps running and is saved; one
        still queued for its session is dropped, so a client that retries does
        not pile duplicate, billed turns up behind it."""
        state["abandoned"] = True
        if not state["started"] and not task.done():
            task.cancel()

    def _turn_error(
        task: asyncio.Task, state: dict[str, Any], session_key: str,
    ) -> tuple[int, str, str] | None:
        """``(status, message, type)`` for a finished turn with no answer."""
        if task.cancelled():
            return 409, "Turn was stopped", "turn_stopped"
        exc = task.exception()
        if exc is None:
            return None
        ceiling = state["ceiling"]
        if ceiling is not None and ceiling.expired():
            logger.warning(
                "OpenAI API turn for session {} hit the {}s ceiling", session_key, turn_timeout,
            )
            return 504, f"Turn exceeded {turn_timeout:g}s limit", "server_error"
        logger.opt(exception=exc).error("OpenAI API turn failed for session {}", session_key)
        return 500, "Internal server error", "server_error"

    def _auth_or_error(request: Request) -> JSONResponse | None:
        principal = resolve_principal(request.headers)
        if principal is None:
            return _error_json(
                401, "missing or invalid bearer token", "authentication_error"
            )
        if not principal.has_scope(Scope.CHAT_WRITE):
            return _error_json(
                403, "token lacks required scope: chat:write", "permission_error"
            )
        return None

    async def models(request: Request) -> Response:
        err = _auth_or_error(request)
        if err is not None:
            return err
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": model_name,
                        "object": "model",
                        "created": 0,
                        "owned_by": "durin",
                    }
                ],
            }
        )

    async def _wait_for_turn(
        task: asyncio.Task, state: dict[str, Any], session_key: str,
    ) -> Any | JSONResponse:
        """Wait up to ``request_timeout`` for a turn; the answer, or an error
        response. The turn is never cancelled by the wait itself."""
        try:
            done, _ = await asyncio.wait({task}, timeout=request_timeout)
        except asyncio.CancelledError:
            _abandon(task, state)
            raise
        if not done:
            _abandon(task, state)
            return _error_json(
                504, f"Request timed out after {request_timeout}s", "server_error"
            )
        err = _turn_error(task, state, session_key)
        if err is not None:
            return _error_json(*err)
        return task.result()

    async def _plain_response(
        text: str, media_paths: list[str], session_key: str, lock: asyncio.Lock
    ) -> Response:
        response = await _wait_for_turn(
            *_start_turn(text, media_paths, session_key, lock), session_key,
        )
        if isinstance(response, JSONResponse):
            return response
        response_text = _response_text(response)
        usage = _response_usage(response)
        if not response_text.strip():
            logger.warning("Empty API response for session {}, retrying", session_key)
            retry = await _wait_for_turn(
                *_start_turn(text, media_paths, session_key, lock), session_key,
            )
            if isinstance(retry, JSONResponse):
                return retry
            response_text = _response_text(retry)
            # The first call was a real, billed LLM round-trip even though its
            # content was empty — its usage still counts.
            usage = _add_usage(usage, _response_usage(retry))
            if not response_text.strip():
                response_text = EMPTY_FINAL_RESPONSE_MESSAGE
        return JSONResponse(_chat_completion_response(response_text, model_name, usage))

    def _stream_response(
        text: str, media_paths: list[str], session_key: str, lock: asyncio.Lock
    ) -> StreamingResponse:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        emitted = {"any": False}
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        async def _on_stream(token: str) -> None:
            if token:
                emitted["any"] = True
            await queue.put(token)

        async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
            # Stream-end callbacks mark generation-segment boundaries (e.g.
            # before a tool call). Tool-backed turns continue after a segment,
            # so the HTTP stream closes only when the turn ends.
            return None

        task, state = _start_turn(
            text, media_paths, session_key, lock,
            on_stream=_on_stream, on_stream_end=_on_stream_end,
        )

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is None:
                response = t.result()
                usage.update(_response_usage(response))
                if not emitted["any"]:
                    tail = _response_text(response)
                    if tail.strip():
                        queue.put_nowait(tail)
            queue.put_nowait(None)

        task.add_done_callback(_on_done)

        async def _gen():
            finished = False
            try:
                while True:
                    # While a tool runs no text flows; a comment line keeps
                    # proxies and read timeouts from dropping the connection.
                    try:
                        async with asyncio.timeout(_SSE_KEEPALIVE_S):
                            token = await queue.get()
                    except TimeoutError:
                        yield _SSE_KEEPALIVE
                        continue
                    if token is None:
                        break
                    yield _sse_chunk(token, model_name, chunk_id)
                err = _turn_error(task, state, session_key)
                if err is not None:
                    _status, message, err_type = err
                    frame = {"error": {"message": message, "type": err_type}}
                    yield f"data: {json.dumps(frame)}\n\n".encode()
                else:
                    yield _sse_chunk(
                        "", model_name, chunk_id, finish_reason="stop", usage=usage,
                    )
                    yield _SSE_DONE
                finished = True
            finally:
                if not finished:
                    # The client left: the stream ends, the turn does not.
                    _abandon(task, state)

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    async def _parse_multipart(
        request: Request,
    ) -> tuple[str, list[str], str | None, str | None]:
        media_dir = get_media_dir("api")
        form = await request.form()
        text = str(form.get("message") or "")
        raw_session = form.get("session_id")
        session_id = (str(raw_session).strip() or None) if raw_session else None
        raw_model = form.get("model")
        model = (str(raw_model).strip() or None) if raw_model else None
        media_paths: list[str] = []
        for upload in form.getlist("files"):
            if isinstance(upload, str):
                continue
            raw = await upload.read()
            if len(raw) > MAX_FILE_SIZE:
                raise FileSizeExceeded(
                    f"File '{upload.filename}' exceeds "
                    f"{MAX_FILE_SIZE // (1024 * 1024)}MB limit"
                )
            base = safe_filename(upload.filename or "upload.bin")
            dest = media_dir / f"{uuid.uuid4().hex[:12]}_{base}"
            dest.write_bytes(raw)
            media_paths.append(str(dest))
        if not text:
            text = "Analyze the uploaded file(s)."
        return text, media_paths, session_id, model

    async def chat_completions(request: Request) -> Response:
        err = _auth_or_error(request)
        if err is not None:
            return err

        content_type = request.headers.get("content-type", "")
        stream = False
        try:
            if content_type.startswith("multipart/"):
                text, media_paths, session_id, requested_model = await _parse_multipart(
                    request
                )
            else:
                try:
                    body = await request.json()
                except Exception:
                    return _error_json(400, "Invalid JSON body")
                stream = bool(body.get("stream", False))
                requested_model = body.get("model")
                text, media_paths = _parse_json_content(body)
                session_id = body.get("session_id")
        except ValueError as e:
            return _error_json(400, str(e))
        except FileSizeExceeded as e:
            return _error_json(413, str(e))

        if requested_model and requested_model != model_name:
            return _error_json(
                400, f"Only configured model '{model_name}' is available"
            )

        session_key = (
            f"{API_SESSION_PREFIX}{session_id}"
            if session_id
            else API_DEFAULT_SESSION_KEY
        )
        lock = session_locks.setdefault(session_key, asyncio.Lock())

        logger.info(
            "API request session_key={} media={} stream={}",
            session_key,
            len(media_paths),
            stream,
        )
        if stream:
            return _stream_response(text, media_paths, session_key, lock)
        return await _plain_response(text, media_paths, session_key, lock)

    return [
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
    ]
