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


def test_service_mode_without_discovery_quietly_uses_pool(provider, fastembed_stand_in):
    """No discovery file (no gateway serving) → local pool for this call,
    isolation stays "service" so a later-started server gets picked up. The
    pool's worker loads the fastembed stand-in, not a real model."""
    provider._model = FakeModel()
    # No embed-server.json exists in the isolated DURIN_HOME.
    out = provider.embed(["hola"])
    assert len(out) == 1
    assert provider._isolation == "service"   # no permanent flip


def test_service_mode_broken_server_flips_to_process(provider, monkeypatch, fastembed_stand_in):
    # The fallback pool's worker loads the fastembed stand-in, not a real model.
    from durin.memory import embed_server
    from durin.memory import embedding as embedding_mod

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
    """Full loop: provider(service) → HTTP → embed server app → response.

    Runs the server with the production config, which loads no websocket
    stack: the embed API is plain HTTP, and uvicorn's default websocket
    implementation is built on an API the websockets package deprecated."""
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
    server = uvicorn.Server(embed_server.embed_server_config(app))
    thread = threading.Thread(
        target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "test embed server never started"
        assert server.config.ws_protocol_class is None

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


def _catalog_reads(monkeypatch) -> list[int]:
    """Replace fastembed's model catalog with one that knows only
    `known/model`, counting reads. Reading the real catalog imports
    fastembed and onnxruntime."""
    from durin.memory import embedding as embedding_mod

    reads: list[int] = []

    def catalog():
        reads.append(1)
        return {"known/model": {"model": "known/model", "dim": 3}}

    monkeypatch.setattr(embedding_mod, "list_supported_models", catalog)
    return reads


def test_a_service_consumer_never_reads_the_model_catalog_while_served(
    tmp_path, monkeypatch,
):
    """Every embed of a service consumer goes to the embed server, which
    holds the model, so neither building the provider nor a served embed
    reads the catalog (and so neither loads fastembed or onnxruntime)."""
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    from durin.memory import embed_server
    from durin.memory.embedding import FastembedProvider

    reads = _catalog_reads(monkeypatch)
    monkeypatch.setattr(
        embed_server, "read_discovery",
        lambda: {"port": 1, "token": "t", "model": "m"})
    monkeypatch.setattr(
        embed_server, "service_embed",
        lambda texts, *, rec: [[7.0] for _ in texts])

    provider = FastembedProvider(model="known/model", isolation="service")
    assert provider.embed(["a"]) == [[7.0]]
    assert reads == []


def test_a_service_consumer_checks_the_model_when_it_loads_its_own_copy(
    tmp_path, monkeypatch,
):
    """The embed server builds its provider from the same config and then
    holds the model inline, as a consumer does after falling back: loading
    that copy refuses an unknown model with the catalog's own message."""
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    from durin.memory.embedding import FastembedProvider

    reads = _catalog_reads(monkeypatch)
    provider = FastembedProvider(model="unknown/model", isolation="service")
    assert reads == []
    provider._isolation = "inline"
    with pytest.raises(ValueError, match="unknown/model"):
        provider.embed(["a"])
    assert provider._model is None


@pytest.mark.parametrize("isolation", ["inline", "process"])
def test_other_isolations_check_the_model_when_built(isolation, monkeypatch):
    from durin.memory.embedding import FastembedProvider

    reads = _catalog_reads(monkeypatch)
    with pytest.raises(ValueError, match="unknown/model"):
        FastembedProvider(model="unknown/model", isolation=isolation)
    assert reads == [1]
