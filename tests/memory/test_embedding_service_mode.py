"""FastembedProvider isolation="service": discovery, fallback, E2E."""
from __future__ import annotations

import contextlib
import socket
import threading
import time

import pytest

from tests.memory.test_embedding_isolation import (  # reuse the harness
    FakeModel,
    _inject_fake_fastembed,
)


@pytest.fixture()
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    from durin.memory.embedding import FastembedProvider

    with _inject_fake_fastembed():
        p = FastembedProvider(isolation="service")
    return p


def test_service_mode_uses_discovered_server(provider, monkeypatch):
    from durin.memory import embed_server

    monkeypatch.setattr(
        embed_server, "read_discovery",
        lambda: {"port": 1, "token": "t", "model": "m"})
    monkeypatch.setattr(
        embed_server, "service_embed",
        lambda texts, *, rec: [[42.0] for _ in texts])

    out = provider.embed(["a", "b"])
    assert out == [[42.0], [42.0]]
    assert provider._isolation == "service"


def test_service_mode_without_discovery_quietly_uses_pool(provider):
    """No discovery file (no gateway serving) → local pool for this call,
    isolation stays "service" so a later-started server gets picked up."""
    provider._model = FakeModel()
    # No embed-server.json exists in the isolated DURIN_HOME.
    out = provider.embed(["hola"])
    assert len(out) == 1
    assert provider._isolation == "service"   # no permanent flip


def test_service_mode_broken_server_flips_to_process(provider, monkeypatch):
    from durin.memory import embed_server, embedding as embedding_mod

    events: list[str] = []
    monkeypatch.setattr(
        embedding_mod, "emit_tool_event",
        lambda name, payload: events.append(name))
    monkeypatch.setattr(
        embed_server, "read_discovery",
        lambda: {"port": 1, "token": "t", "model": "m"})

    def _boom(texts, *, rec):
        raise ConnectionError("down")

    monkeypatch.setattr(embed_server, "service_embed", _boom)
    provider._model = FakeModel()

    out = provider.embed(["hola"])
    assert len(out) == 1                      # pool/inline served the call
    assert provider._isolation != "service"   # permanent flip
    assert "memory.embedding.service_fallback" in events


def test_service_mode_end_to_end_over_real_http(tmp_path, monkeypatch):
    """Full loop: provider(service) → HTTP → embed server app → response."""
    import uvicorn

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    from durin.memory import embed_server
    from durin.memory.embedding import FastembedProvider

    class _SrvProvider:
        model_name = "fake/test-embed"
        dimensions = 3

        def embed(self, texts):
            return [[float(len(t)), 5.0, 5.0] for t in texts]

        def embed_passages(self, texts):
            return self.embed(texts)

        def embed_query(self, query):
            return self.embed([query])[0]

    cache = embed_server.EmbedResultCache(tmp_path / "c.sqlite")
    app = embed_server.build_embed_app(_SrvProvider(), token="tok", cache=cache)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(
        target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "test embed server never started"

        embed_server.write_discovery(
            port=port, token="tok", model="fake/test-embed")
        with _inject_fake_fastembed():
            client_provider = FastembedProvider(isolation="service")
        out = client_provider.embed(["hola", "mundo!"])
        assert out[0][0] == 4.0 and out[1][0] == 6.0
        assert client_provider._isolation == "service"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        with contextlib.suppress(OSError):
            sock.close()
