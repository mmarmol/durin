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
import contextlib
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


def _error_json(
    status: int, message: str, err_type: str = "invalid_request_error"
) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": err_type, "code": status}},
        status_code=status,
    )


def _chat_completion_response(content: str, model: str) -> dict[str, Any]:
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
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _response_text(value: Any) -> str:
    """Normalize ``process_direct`` output (OutboundMessage | str | None) to text."""
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)


def _sse_chunk(
    delta: str, model: str, chunk_id: str, finish_reason: str | None = None
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
    resolve_principal: Callable[[Any], Principal | None],
) -> list[Route]:
    """Build the ``/v1`` route list for the gateway app.

    ``resolve_principal`` is injected (headers → Principal | None) so this
    module needs no import from ``asgi`` and stays independently testable.
    """
    session_locks: dict[str, asyncio.Lock] = {}

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

    async def _plain_response(
        text: str, media_paths: list[str], session_key: str, lock: asyncio.Lock
    ) -> Response:
        try:
            async with lock:
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=text,
                        media=media_paths or None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                    ),
                    timeout=request_timeout,
                )
                response_text = _response_text(response)
                if not response_text.strip():
                    logger.warning(
                        "Empty API response for session {}, retrying", session_key
                    )
                    retry = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths or None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                        ),
                        timeout=request_timeout,
                    )
                    response_text = _response_text(retry)
                    if not response_text.strip():
                        response_text = EMPTY_FINAL_RESPONSE_MESSAGE
        except asyncio.TimeoutError:
            return _error_json(
                504, f"Request timed out after {request_timeout}s", "server_error"
            )
        except Exception:
            logger.exception("OpenAI API error for session {}", session_key)
            return _error_json(500, "Internal server error", "server_error")
        return JSONResponse(_chat_completion_response(response_text, model_name))

    async def chat_completions(request: Request) -> Response:
        err = _auth_or_error(request)
        if err is not None:
            return err

        content_type = request.headers.get("content-type", "")
        stream = False
        try:
            if content_type.startswith("multipart/"):
                # Task 6 fills this branch in.
                return _error_json(400, "multipart is not supported yet")
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
            # Task 5 fills this branch in.
            return _error_json(400, "stream is not supported yet")
        return await _plain_response(text, media_paths, session_key, lock)

    return [
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
    ]
