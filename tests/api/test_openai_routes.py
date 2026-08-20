"""Gateway OpenAI-compatible /v1 surface: auth gating and models listing."""

from __future__ import annotations

import asyncio
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


def _build_app(tmp_path, monkeypatch, agent_loop=None, api_request_timeout: float = 5.0):
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
