"""Gateway OpenAI-compatible /v1 surface: auth gating and models listing."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus


def _make_loop(text: str = "mock response") -> MagicMock:
    loop = MagicMock()
    loop.process_direct = AsyncMock(return_value=SimpleNamespace(content=text))
    return loop


def _build_app(
    tmp_path,
    monkeypatch,
    agent_loop=None,
    api_request_timeout: float = 5.0,
    api_turn_timeout: float = 5.0,
):
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    media_dir = tmp_path / "media"
    media_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "durin.api.openai_routes.get_media_dir", lambda _channel: media_dir
    )

    from durin.api.asgi import build_gateway_http_app
    from durin.channels.websocket import WebSocketChannel

    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    return build_gateway_http_app(
        channel,
        registry,
        auth=registry.get("auth"),
        agent_loop=agent_loop if agent_loop is not None else _make_loop(),
        model_name="test-model",
        api_request_timeout=api_request_timeout,
        api_turn_timeout=api_turn_timeout,
    )


def _mint(scopes: list[str]) -> str:
    from durin.security.api_tokens import ApiTokenStore

    _token_id, plaintext = ApiTokenStore().issue(scopes, label="test")
    return plaintext


@pytest.fixture()
def client(tmp_path, monkeypatch):
    return TestClient(_build_app(tmp_path, monkeypatch))


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_models_requires_token(client):
    r = client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_models_rejects_garbage_token(client):
    r = client.get("/v1/models", headers=_hdr("nbwt_not_a_real_token"))
    assert r.status_code == 401


def test_models_rejects_wrong_scope(client):
    tok = _mint(["sessions:read"])
    r = client.get("/v1/models", headers=_hdr(tok))
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"


def test_models_ok_with_chat_write(client):
    tok = _mint(["chat:write"])
    r = client.get("/v1/models", headers=_hdr(tok))
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert data["data"][0]["id"] == "test-model"
    assert data["data"][0]["owned_by"] == "durin"


def test_models_ok_with_admin(client):
    tok = _mint(["admin"])
    r = client.get("/v1/models", headers=_hdr(tok))
    assert r.status_code == 200


def test_chat_requires_token(client):
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 401


def _chat(client, token, payload):
    return client.post("/v1/chat/completions", json=payload, headers=_hdr(token))


def test_chat_single_message_round_trip(tmp_path, monkeypatch):
    loop = _make_loop("hello from durin")
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    r = _chat(client, tok, {"messages": [{"role": "user", "content": "hola"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello from durin"
    kwargs = loop.process_direct.await_args.kwargs
    assert kwargs["session_key"] == "api:default"
    assert kwargs["channel"] == "api"
    assert kwargs["chat_id"] == "default"
    assert kwargs["content"] == "hola"


def test_chat_session_id_routes_to_api_session(tmp_path, monkeypatch):
    loop = _make_loop()
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    r = _chat(
        client,
        tok,
        {"messages": [{"role": "user", "content": "x"}], "session_id": "agent-7"},
    )
    assert r.status_code == 200
    assert loop.process_direct.await_args.kwargs["session_key"] == "api:agent-7"


def test_chat_rejects_message_history(client):
    tok = _mint(["chat:write"])
    r = _chat(
        client,
        tok,
        {
            "messages": [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ]
        },
    )
    assert r.status_code == 400
    assert "single user message" in r.json()["error"]["message"]


def test_chat_rejects_non_user_message(client):
    tok = _mint(["chat:write"])
    r = _chat(client, tok, {"messages": [{"role": "system", "content": "a"}]})
    assert r.status_code == 400


def test_chat_rejects_client_tools(client):
    tok = _mint(["chat:write"])
    r = _chat(
        client,
        tok,
        {
            "messages": [{"role": "user", "content": "a"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        },
    )
    assert r.status_code == 400
    assert "server-side" in r.json()["error"]["message"]


def test_chat_rejects_model_mismatch(client):
    tok = _mint(["chat:write"])
    r = _chat(
        client,
        tok,
        {"messages": [{"role": "user", "content": "a"}], "model": "gpt-4o"},
    )
    assert r.status_code == 400


def test_chat_accepts_matching_model(tmp_path, monkeypatch):
    client = TestClient(_build_app(tmp_path, monkeypatch))
    tok = _mint(["chat:write"])
    r = _chat(
        client,
        tok,
        {"messages": [{"role": "user", "content": "a"}], "model": "test-model"},
    )
    assert r.status_code == 200


def test_chat_rejects_remote_image_url(client):
    tok = _mint(["chat:write"])
    r = _chat(
        client,
        tok,
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                    ],
                }
            ]
        },
    )
    assert r.status_code == 400


def test_chat_saves_base64_image(tmp_path, monkeypatch):
    loop = _make_loop()
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    data_url = "data:image/png;base64,aGVsbG8="
    r = _chat(
        client,
        tok,
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ]
        },
    )
    assert r.status_code == 200
    media = loop.process_direct.await_args.kwargs["media"]
    assert media and len(media) == 1


def test_chat_reports_real_usage(tmp_path, monkeypatch):
    loop = MagicMock()
    loop.process_direct = AsyncMock(
        return_value=SimpleNamespace(
            content="hi",
            metadata={"usage": {"prompt_tokens": 100, "completion_tokens": 7}},
        )
    )
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    r = _chat(client, tok, {"messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 200
    assert r.json()["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 7,
        "total_tokens": 107,
    }


def test_chat_defaults_usage_to_zero_when_absent(tmp_path, monkeypatch):
    client = TestClient(_build_app(tmp_path, monkeypatch))  # _make_loop: no metadata
    tok = _mint(["chat:write"])
    r = _chat(client, tok, {"messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 200
    assert r.json()["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_chat_retry_sums_usage_across_attempts(tmp_path, monkeypatch):
    """The empty-response retry issues a second real LLM call; both were
    billed, so their usage must both land in the reported total."""
    responses = [
        SimpleNamespace(
            content="",
            metadata={"usage": {"prompt_tokens": 40, "completion_tokens": 0}},
        ),
        SimpleNamespace(
            content="ok",
            metadata={"usage": {"prompt_tokens": 45, "completion_tokens": 5}},
        ),
    ]
    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=responses)
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    r = _chat(client, tok, {"messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 200
    assert r.json()["usage"] == {
        "prompt_tokens": 85,
        "completion_tokens": 5,
        "total_tokens": 90,
    }


def test_chat_empty_response_retries_then_falls_back(tmp_path, monkeypatch):
    from durin.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

    loop = MagicMock()
    loop.process_direct = AsyncMock(return_value=SimpleNamespace(content=""))
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    r = _chat(client, tok, {"messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == EMPTY_FINAL_RESPONSE_MESSAGE
    assert loop.process_direct.await_count == 2


def test_chat_timeout_maps_to_504(tmp_path, monkeypatch):
    async def _hang(**_kwargs):
        await asyncio.sleep(30)

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_hang)
    client = TestClient(
        _build_app(tmp_path, monkeypatch, agent_loop=loop, api_request_timeout=0.05)
    )
    tok = _mint(["chat:write"])
    r = _chat(client, tok, {"messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 504
    assert r.json()["error"]["type"] == "server_error"


def test_multipart_message_files_and_session(tmp_path, monkeypatch):
    loop = _make_loop("got it")
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    r = client.post(
        "/v1/chat/completions",
        data={"message": "analyze this", "session_id": "agent-9"},
        files=[("files", ("notes.txt", b"hello world", "text/plain"))],
        headers=_hdr(tok),
    )
    assert r.status_code == 200
    kwargs = loop.process_direct.await_args.kwargs
    assert kwargs["session_key"] == "api:agent-9"
    assert kwargs["content"] == "analyze this"
    assert kwargs["media"] and kwargs["media"][0].endswith("_notes.txt")


def test_multipart_without_message_uses_fallback_text(tmp_path, monkeypatch):
    loop = _make_loop()
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    r = client.post(
        "/v1/chat/completions",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
        headers=_hdr(tok),
    )
    assert r.status_code == 200
    assert loop.process_direct.await_args.kwargs["content"].strip()


def test_multipart_oversize_file_maps_to_413(tmp_path, monkeypatch):
    monkeypatch.setattr("durin.api.openai_routes.MAX_FILE_SIZE", 1024)
    client = TestClient(_build_app(tmp_path, monkeypatch))
    tok = _mint(["chat:write"])
    r = client.post(
        "/v1/chat/completions",
        data={"message": "hi"},
        files=[("files", ("big.bin", b"x" * 2048, "application/octet-stream"))],
        headers=_hdr(tok),
    )
    assert r.status_code == 413


def _sse_events(raw: str) -> list[str]:
    return [
        line[len("data: ") :] for line in raw.splitlines() if line.startswith("data: ")
    ]


def test_stream_chunks_then_done(tmp_path, monkeypatch):
    async def _fake_process(**kwargs):
        await kwargs["on_stream"]("Hel")
        await kwargs["on_stream"]("lo")
        return SimpleNamespace(content="Hello")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_fake_process)
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "a"}], "stream": True},
        headers=_hdr(tok),
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        raw = "".join(r.iter_text())
    events = _sse_events(raw)
    assert events[-1] == "[DONE]"
    deltas = [
        json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1]
    ]
    assert "".join(deltas) == "Hello"
    finish = json.loads(events[-2])
    assert finish["choices"][0]["finish_reason"] == "stop"


def test_stream_final_chunk_reports_usage(tmp_path, monkeypatch):
    async def _fake_process(**kwargs):
        await kwargs["on_stream"]("Hello")
        return SimpleNamespace(
            content="Hello",
            metadata={"usage": {"prompt_tokens": 50, "completion_tokens": 3}},
        )

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_fake_process)
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "a"}], "stream": True},
        headers=_hdr(tok),
    ) as r:
        raw = "".join(r.iter_text())
    events = _sse_events(raw)
    finish = json.loads(events[-2])
    assert finish["choices"][0]["finish_reason"] == "stop"
    assert finish["usage"] == {
        "prompt_tokens": 50,
        "completion_tokens": 3,
        "total_tokens": 53,
    }


def test_stream_tail_flush_when_no_tokens_emitted(tmp_path, monkeypatch):
    loop = _make_loop("full answer")  # returns content but never calls on_stream
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "a"}], "stream": True},
        headers=_hdr(tok),
    ) as r:
        raw = "".join(r.iter_text())
    events = _sse_events(raw)
    assert events[-1] == "[DONE]"
    deltas = [
        json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1]
    ]
    assert "".join(deltas) == "full answer"


def test_stream_failure_emits_error_frame_and_no_done(tmp_path, monkeypatch):
    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=RuntimeError("boom"))
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    tok = _mint(["chat:write"])
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "a"}], "stream": True},
        headers=_hdr(tok),
    ) as r:
        raw = "".join(r.iter_text())
    events = _sse_events(raw)
    assert events, "expected at least the error frame"
    assert events[-1] != "[DONE]"
    assert "error" in json.loads(events[-1])


def _stream_raw(client, tok) -> str:
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "a"}], "stream": True},
        headers=_hdr(tok),
    ) as r:
        assert r.status_code == 200
        return "".join(r.iter_text())


def test_stream_outlives_the_request_timeout(tmp_path, monkeypatch):
    """A working stream is not cut by api_request_timeout (non-streaming only)."""

    async def _long_turn(**kwargs):
        await asyncio.sleep(0.3)
        await kwargs["on_stream"]("done")
        return SimpleNamespace(content="done")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_long_turn)
    client = TestClient(
        _build_app(tmp_path, monkeypatch, agent_loop=loop, api_request_timeout=0.05)
    )
    events = _sse_events(_stream_raw(client, _mint(["chat:write"])))
    assert events[-1] == "[DONE]"
    deltas = [
        json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1]
    ]
    assert "".join(deltas) == "done"


def test_stream_sends_keepalive_comments_while_silent(tmp_path, monkeypatch):
    """Silence (a tool running) yields SSE comment lines, not data frames."""
    monkeypatch.setattr("durin.api.openai_routes._SSE_KEEPALIVE_S", 0.02)

    async def _silent_then_answer(**kwargs):
        await asyncio.sleep(0.2)
        await kwargs["on_stream"]("ok")
        return SimpleNamespace(content="ok")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_silent_then_answer)
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    raw = _stream_raw(client, _mint(["chat:write"]))
    assert ": keepalive" in raw.splitlines()
    events = _sse_events(raw)
    assert events[-1] == "[DONE]"


def test_stream_ceiling_ends_with_error_frame_and_no_done(tmp_path, monkeypatch):
    async def _runaway(**_kwargs):
        await asyncio.sleep(30)

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_runaway)
    client = TestClient(
        _build_app(tmp_path, monkeypatch, agent_loop=loop, api_turn_timeout=0.05)
    )
    events = _sse_events(_stream_raw(client, _mint(["chat:write"])))
    assert events[-1] != "[DONE]"
    error = json.loads(events[-1])["error"]
    assert "0.05s" in error["message"]
    assert error["type"] == "server_error"


def test_stream_ceiling_zero_disables_it(tmp_path, monkeypatch):
    async def _long_turn(**kwargs):
        await asyncio.sleep(0.2)
        await kwargs["on_stream"]("fine")
        return SimpleNamespace(content="fine")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_long_turn)
    client = TestClient(
        _build_app(
            tmp_path,
            monkeypatch,
            agent_loop=loop,
            api_request_timeout=0.05,
            api_turn_timeout=0,
        )
    )
    events = _sse_events(_stream_raw(client, _mint(["chat:write"])))
    assert events[-1] == "[DONE]"


def _endpoint(loop, *, request_timeout=5.0, turn_timeout=5.0):
    from durin.api.openai_routes import build_openai_routes
    from durin.service.principal import Principal

    routes = build_openai_routes(
        loop,
        model_name="test-model",
        request_timeout=request_timeout,
        turn_timeout=turn_timeout,
        resolve_principal=lambda _h: Principal.remote("t", frozenset({"chat:write"})),
    )
    return next(r for r in routes if r.path == "/v1/chat/completions").endpoint


def _json_req(*, stream: bool, session_id: str | None = None):
    class _Req:
        headers = {"content-type": "application/json"}

        async def json(self):
            body = {"messages": [{"role": "user", "content": "x"}], "stream": stream}
            if session_id:
                body["session_id"] = session_id
            return body

    return _Req()


def test_stream_client_disconnect_leaves_the_turn_running():
    """Closing the body iterator ends the stream only; the turn completes."""
    state = {"finished": False}

    async def _turn(**kwargs):
        await kwargs["on_stream"]("first")
        await asyncio.sleep(0.05)
        state["finished"] = True
        return SimpleNamespace(content="done")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_turn)
    chat_endpoint = _endpoint(loop)

    async def _drive():
        response = await chat_endpoint(_json_req(stream=True))
        body = response.body_iterator
        assert b"first" in await body.__anext__()
        await body.aclose()
        await asyncio.sleep(0.2)

    asyncio.run(_drive())
    assert state["finished"]


def test_non_stream_timeout_answers_504_and_the_turn_completes(tmp_path, monkeypatch):
    state = {"finished": False}

    async def _slow(**_kwargs):
        await asyncio.sleep(0.2)
        state["finished"] = True
        return SimpleNamespace(content="late")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_slow)
    # The context manager keeps one event loop alive across requests; a bare
    # TestClient tears its loop down after each request, killing the turn.
    with TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop, api_request_timeout=0.05)) as client:
        r = _chat(client, _mint(["chat:write"]), {"messages": [{"role": "user", "content": "a"}]})
        assert r.status_code == 504
        import time

        time.sleep(0.4)
    assert state["finished"]


def test_turn_ceiling_applies_to_non_stream(tmp_path, monkeypatch):
    async def _runaway(**_kwargs):
        await asyncio.sleep(30)

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_runaway)
    client = TestClient(_build_app(
        tmp_path, monkeypatch, agent_loop=loop, api_request_timeout=5.0, api_turn_timeout=0.05))
    r = _chat(client, _mint(["chat:write"]), {"messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 504
    assert "0.05s" in r.json()["error"]["message"]


def test_stopped_turn_non_stream_answers_409(tmp_path, monkeypatch):
    async def _stopped(**_kwargs):
        raise asyncio.CancelledError

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_stopped)
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    r = _chat(client, _mint(["chat:write"]), {"messages": [{"role": "user", "content": "a"}]})
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "turn_stopped"


def test_stopped_turn_stream_ends_with_error_frame(tmp_path, monkeypatch):
    async def _stopped(**kwargs):
        await kwargs["on_stream"]("partial")
        raise asyncio.CancelledError

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_stopped)
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    events = _sse_events(_stream_raw(client, _mint(["chat:write"])))
    assert events[-1] != "[DONE]"
    assert json.loads(events[-1])["error"] == {"message": "Turn was stopped", "type": "turn_stopped"}


def test_turn_gets_a_no_op_progress_callback(tmp_path, monkeypatch):
    loop = _make_loop()
    client = TestClient(_build_app(tmp_path, monkeypatch, agent_loop=loop))
    _chat(client, _mint(["chat:write"]), {"messages": [{"role": "user", "content": "a"}]})
    assert loop.process_direct.await_args.kwargs["on_progress"] is not None


def test_a_request_that_times_out_while_queued_drops_its_turn():
    """A turn abandoned before it got its session never runs: a client retry
    would otherwise queue a duplicate, billed turn behind it."""
    calls = []

    async def _turn(**kwargs):
        calls.append(kwargs["content"])
        await asyncio.sleep(0.3)
        return SimpleNamespace(content="ok")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_turn)
    chat_endpoint = _endpoint(loop, request_timeout=0.05)

    async def _drive():
        first = asyncio.create_task(chat_endpoint(_json_req(stream=False, session_id="same")))
        await asyncio.sleep(0.01)  # the first turn holds the session
        second = await chat_endpoint(_json_req(stream=False, session_id="same"))
        assert second.status_code == 504
        await first
        await asyncio.sleep(0.5)  # long enough for a surviving second turn to have run

    asyncio.run(_drive())
    assert len(calls) == 1


def test_a_stream_dropped_while_queued_drops_its_turn():
    calls = []

    async def _turn(**kwargs):
        calls.append(kwargs["content"])
        await asyncio.sleep(0.3)
        return SimpleNamespace(content="ok")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_turn)
    chat_endpoint = _endpoint(loop)

    async def _drive():
        first = asyncio.create_task(chat_endpoint(_json_req(stream=False, session_id="same")))
        await asyncio.sleep(0.01)
        response = await chat_endpoint(_json_req(stream=True, session_id="same"))
        body = response.body_iterator
        reader = asyncio.create_task(body.__anext__())
        await asyncio.sleep(0.02)
        # The client leaves while its turn is still queued for the session.
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
            await reader
        await first
        await asyncio.sleep(0.5)

    asyncio.run(_drive())
    assert len(calls) == 1


def test_a_failure_after_the_request_ended_is_logged():
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR")

    async def _late_failure(**_kwargs):
        await asyncio.sleep(0.1)
        raise RuntimeError("boom after 504")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_late_failure)
    chat_endpoint = _endpoint(loop, request_timeout=0.02)

    async def _drive():
        response = await chat_endpoint(_json_req(stream=False))
        assert response.status_code == 504
        await asyncio.sleep(0.3)

    try:
        asyncio.run(_drive())
    finally:
        logger.remove(sink)
    assert any("failed after its request ended" in line for line in lines)


def test_stream_ceiling_starts_when_the_turn_gets_the_session():
    """Time spent queued behind another turn on the session is not billed."""
    from durin.api.openai_routes import build_openai_routes
    from durin.service.principal import Principal

    async def _turn(**kwargs):
        if kwargs.get("on_stream") is None:  # the blocking, non-streaming turn
            await asyncio.sleep(0.3)
            return SimpleNamespace(content="first")
        await asyncio.sleep(0.05)  # well inside the 0.2s ceiling on its own
        await kwargs["on_stream"]("second")
        return SimpleNamespace(content="second")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_turn)
    routes = build_openai_routes(
        loop,
        model_name="test-model",
        request_timeout=5.0,
        turn_timeout=0.2,
        resolve_principal=lambda _h: Principal.remote("t", frozenset({"chat:write"})),
    )
    chat_endpoint = next(r for r in routes if r.path == "/v1/chat/completions").endpoint

    def _req(stream: bool):
        class _Req:
            headers = {"content-type": "application/json"}

            async def json(self):
                return {
                    "messages": [{"role": "user", "content": "x"}],
                    "session_id": "same",
                    "stream": stream,
                }

        return _Req()

    async def _drive() -> bytes:
        blocking = asyncio.create_task(chat_endpoint(_req(False)))
        await asyncio.sleep(0.05)  # the blocking turn now holds the session
        response = await chat_endpoint(_req(True))
        chunks = [chunk async for chunk in response.body_iterator]
        await blocking
        return b"".join(chunks)

    raw = asyncio.run(_drive()).decode()
    assert _sse_events(raw)[-1] == "[DONE]"


def test_same_session_requests_serialize():
    """Two concurrent turns on one session run one-at-a-time (the lock queues them)."""
    from durin.api.openai_routes import build_openai_routes
    from durin.service.principal import Principal

    running = {"count": 0, "overlap": False}

    async def _slow(**_kwargs):
        running["count"] += 1
        if running["count"] > 1:
            running["overlap"] = True
        await asyncio.sleep(0.05)
        running["count"] -= 1
        return SimpleNamespace(content="ok")

    loop = MagicMock()
    loop.process_direct = AsyncMock(side_effect=_slow)
    routes = build_openai_routes(
        loop,
        model_name="test-model",
        request_timeout=5.0,
        resolve_principal=lambda _h: Principal.remote("t", frozenset({"chat:write"})),
    )
    chat_endpoint = next(
        r for r in routes if r.path == "/v1/chat/completions"
    ).endpoint

    class _Req:
        headers = {"content-type": "application/json"}

        async def json(self):
            return {
                "messages": [{"role": "user", "content": "x"}],
                "session_id": "same",
            }

    async def _drive():
        await asyncio.gather(chat_endpoint(_Req()), chat_endpoint(_Req()))

    asyncio.run(_drive())
    assert loop.process_direct.await_count == 2
    assert not running["overlap"]


def test_v1_wins_over_the_spa_mount(tmp_path, monkeypatch):
    """The SPA is mounted at "/" and would swallow /v1 on a first-match router."""
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    spa = tmp_path / "dist"
    spa.mkdir()
    (spa / "index.html").write_text("<!doctype html><title>durin</title>", encoding="utf-8")

    from durin.api.asgi import build_gateway_http_app
    from durin.channels.websocket import WebSocketChannel

    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
    }
    channel = WebSocketChannel(cfg, MessageBus())
    app = build_gateway_http_app(
        channel,
        channel._services,
        auth=channel._services.get("auth"),
        static_dist_path=spa,
        agent_loop=_make_loop(),
        model_name="test-model",
    )
    client = TestClient(app)
    tok = _mint(["chat:write"])
    r = client.get("/v1/models", headers=_hdr(tok))
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "test-model"


def test_v1_absent_when_no_agent_loop(tmp_path, monkeypatch):
    from durin.api.asgi import build_gateway_http_app
    from durin.channels.websocket import WebSocketChannel

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
    }
    channel = WebSocketChannel(cfg, MessageBus())
    app = build_gateway_http_app(
        channel, channel._services, auth=channel._services.get("auth")
    )
    client = TestClient(app)
    r = client.get("/v1/models")
    assert r.status_code == 404
