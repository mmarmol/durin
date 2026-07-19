"""The standing embedding service: app, auth, result cache, discovery."""
from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

from durin.memory.embed_server import (
    EmbedResultCache,
    build_embed_app,
    clear_discovery,
    read_discovery,
    write_discovery,
)


class _CountingProvider:
    model_name = "fake/test-embed"
    dimensions = 4

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t)), 1.0, 2.0, 3.0] for t in texts]

    def embed_query(self, query: str) -> list[float]:
        self.calls.append([query])
        return [float(len(query)), 9.0, 9.0, 9.0]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t)), 7.0, 7.0, 7.0] for t in texts]


@pytest.fixture()
def served(tmp_path):
    provider = _CountingProvider()
    cache = EmbedResultCache(tmp_path / "embed-cache.sqlite", max_rows=100)
    app = build_embed_app(provider, token="sekret", cache=cache)
    return TestClient(app), provider


def test_embeddings_endpoint_openai_shape(served):
    client, provider = served
    r = client.post(
        "/v1/embeddings",
        headers={"Authorization": "Bearer sekret"},
        json={"input": ["hola", "mundo!"], "kind": "passage"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "fake/test-embed"
    assert [d["index"] for d in body["data"]] == [0, 1]
    assert body["data"][0]["embedding"][0] == 4.0
    assert body["data"][1]["embedding"][0] == 6.0


def test_embeddings_requires_token(served):
    client, _ = served
    r = client.post("/v1/embeddings", json={"input": ["x"]})
    assert r.status_code == 401
    r = client.post(
        "/v1/embeddings",
        headers={"Authorization": "Bearer wrong"},
        json={"input": ["x"]},
    )
    assert r.status_code == 401


def test_health_open_and_reports_model(served):
    client, _ = served
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["model"] == "fake/test-embed"


def test_result_cache_avoids_recompute(served):
    client, provider = served
    headers = {"Authorization": "Bearer sekret"}
    for _ in range(2):
        r = client.post(
            "/v1/embeddings", headers=headers,
            json={"input": ["repetido"], "kind": "passage"},
        )
        assert r.status_code == 200
    assert len(provider.calls) == 1  # second hit came from the cache

    client.post(
        "/v1/embeddings", headers=headers,
        json={"input": ["nuevo"], "kind": "passage"},
    )
    assert len(provider.calls) == 2


def test_query_kind_not_cached_with_passages(served):
    """query vs passage use different E5 prefixes — same text must not
    collide across kinds in the cache."""
    client, provider = served
    headers = {"Authorization": "Bearer sekret"}
    client.post("/v1/embeddings", headers=headers,
                json={"input": ["texto"], "kind": "passage"})
    client.post("/v1/embeddings", headers=headers,
                json={"input": ["texto"], "kind": "query"})
    assert len(provider.calls) == 2


def test_cache_lru_bound(tmp_path):
    cache = EmbedResultCache(tmp_path / "c.sqlite", max_rows=3)
    for i in range(5):
        cache.put("m", "passage", f"t{i}", [float(i)])
    assert cache.get("m", "passage", "t0") is None   # evicted
    assert cache.get("m", "passage", "t4") == [4.0]
    assert cache.rows() <= 3


def test_discovery_roundtrip_and_staleness(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    write_discovery(port=12345, token="sekret", model="fake/test-embed")
    d = read_discovery()
    assert d is not None
    assert d["port"] == 12345 and d["token"] == "sekret"
    assert d["owner"]["pid"] == os.getpid()

    # A discovery file whose owner process is dead reads as None (stale).
    path = tmp_path / "embed-server.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec["owner"] = {"pid": 2**22 + 777, "started": "never"}
    path.write_text(json.dumps(rec), encoding="utf-8")
    assert read_discovery() is None

    clear_discovery()
    assert not path.exists()


def test_raw_kind_uses_plain_embed(served):
    client, provider = served
    r = client.post(
        "/v1/embeddings",
        headers={"Authorization": "Bearer sekret"},
        json={"input": ["passage: ya-prefijado"], "kind": "raw"},
    )
    assert r.status_code == 200
    assert r.json()["data"][0]["embedding"][1] == 7.0  # plain embed() path
