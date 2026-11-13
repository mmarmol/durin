"""The standing embedding service: app, auth, result cache, discovery."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Ending with the process that started the server
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

_WATCHED = textwrap.dedent("""
    import os, sys, time
    from durin.memory.embed_server import exit_when_parent_is_gone
    exit_when_parent_is_gone(int(sys.argv[1]), lambda: os._exit(7), poll_s=0.2)
    open(sys.argv[2], "w").close()
    time.sleep(60)
""")

_INTERMEDIATE = textwrap.dedent("""
    import os, subprocess, sys, time
    child = subprocess.Popen(
        [sys.executable, "-c", sys.argv[1], str(os.getpid()), sys.argv[3]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL)
    print(child.pid, flush=True)
    while not os.path.exists(sys.argv[3]):
        time.sleep(0.05)
    if sys.argv[2] == "wait":
        time.sleep(60)
""")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _start_watched(mode: str, ready: Path):
    """Start an intermediate process that starts a process watching it.

    Returns once the watching process has started its watch (the import of
    durin takes seconds, and a parent killed earlier would go unnoticed
    only because the test was too quick).
    """
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    inter = subprocess.Popen(
        [sys.executable, "-c", _INTERMEDIATE, _WATCHED, mode, str(ready)],
        stdout=subprocess.PIPE, env=env, text=True)
    child_pid = int(inter.stdout.readline())
    deadline = time.monotonic() + 60.0
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert ready.exists(), "the watching process never started its watch"
    return inter, child_pid


def _cleanup(inter, child_pid: int) -> None:
    for pid in (inter.pid, child_pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    inter.wait(timeout=10)
    inter.stdout.close()


def test_watched_process_ends_when_its_parent_is_killed(tmp_path):
    inter, child_pid = _start_watched("wait", tmp_path / "ready")
    try:
        assert _alive(child_pid)
        inter.kill()
        inter.wait(timeout=10)
        assert _wait_gone(child_pid, 8.0), "watched process outlived its parent"
    finally:
        _cleanup(inter, child_pid)


def test_watched_process_ends_when_its_parent_exits_normally(tmp_path):
    inter, child_pid = _start_watched("exit", tmp_path / "ready")
    try:
        inter.wait(timeout=10)
        assert _wait_gone(child_pid, 8.0), "watched process outlived its parent"
    finally:
        _cleanup(inter, child_pid)


def test_watched_process_is_untouched_while_its_parent_lives(tmp_path):
    inter, child_pid = _start_watched("wait", tmp_path / "ready")
    try:
        time.sleep(1.5)  # several poll intervals
        assert _alive(child_pid)
    finally:
        _cleanup(inter, child_pid)


def test_the_watch_fires_at_once_when_the_parent_is_already_gone():
    from durin.memory.embed_server import exit_when_parent_is_gone

    fired = threading.Event()
    exit_when_parent_is_gone(os.getppid() + 12345, fired.set, poll_s=30.0)
    assert fired.wait(2.0)


def test_the_watch_fires_once_and_never_raises():
    from durin.memory.embed_server import exit_when_parent_is_gone

    calls: list[int] = []

    def boom() -> None:
        calls.append(1)
        raise RuntimeError("on_gone failed")

    thread = exit_when_parent_is_gone(os.getppid() + 12345, boom, poll_s=0.05)
    thread.join(2.0)
    assert not thread.is_alive()
    assert calls == [1]


def test_clear_discovery_with_a_token_removes_only_its_own_record(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    path = tmp_path / "embed-server.json"

    write_discovery(port=1, token="other", model="m")
    clear_discovery(token="mine")
    assert path.exists()

    write_discovery(port=1, token="mine", model="m")
    clear_discovery(token="mine")
    assert not path.exists()

    path.write_text("not json", encoding="utf-8")
    clear_discovery(token="mine")
    assert path.exists()

    write_discovery(port=1, token="other", model="m")
    clear_discovery()
    assert not path.exists()


class _FakeProvider:
    model_name = "fake/test-embed"
    _isolation = "service"

    def embed(self, texts):
        return [[0.0] for _ in texts]


@pytest.fixture()
def run_env(tmp_path, monkeypatch):
    """run_embed_server with the model, uvicorn and the watch patched out."""
    import uvicorn

    import durin.config
    import durin.memory.embed_server as es
    import durin.memory.embedding

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    monkeypatch.setattr(durin.config, "load_config", lambda: object())
    monkeypatch.setattr(
        durin.memory.embedding, "provider_from_config",
        lambda config, model=None: _FakeProvider())
    monkeypatch.setattr(es, "embed_server_config", lambda app: object())

    state: dict = {"exits": [], "released": threading.Event()}

    def fake_watch(parent_pid, on_gone, **kw):
        state["parent_pid"] = parent_pid
        state["on_gone"] = on_gone
        return None

    monkeypatch.setattr(es, "exit_when_parent_is_gone", fake_watch)

    def fake_exit(code):
        state["exits"].append(code)
        state["record_at_exit"] = (
            json.loads((tmp_path / "embed-server.json").read_text("utf-8"))
            if (tmp_path / "embed-server.json").exists() else None)
        state["released"].set()

    monkeypatch.setattr(es.os, "_exit", fake_exit)
    monkeypatch.setattr(es, "_PARENT_EXIT_GRACE_S", 0.3)

    class _Server:
        def __init__(self, config):
            state["server"] = self
            self.should_exit = False

        def run(self, sockets=None):
            state["run"](self)

    monkeypatch.setattr(uvicorn, "Server", _Server)
    state["es"] = es
    return state


def test_parent_gone_asks_the_http_server_to_stop(run_env):
    def run(server):
        threading.Thread(target=run_env["on_gone"], daemon=True).start()
        deadline = time.monotonic() + 2.0
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.01)
        run_env["should_exit_seen"] = server.should_exit
        # A server that stops: run returns before the grace runs out.

    run_env["run"] = run
    run_env["es"].run_embed_server(parent_pid=4242)
    assert run_env["parent_pid"] == 4242
    assert run_env["should_exit_seen"] is True
    assert not (Path(os.environ["DURIN_HOME"]) / "embed-server.json").exists()


def test_a_server_that_does_not_stop_is_ended_after_the_grace(run_env):
    def run(server):
        threading.Thread(target=run_env["on_gone"], daemon=True).start()
        run_env["released"].wait(5.0)

    run_env["run"] = run
    run_env["es"].run_embed_server(parent_pid=4242)
    assert run_env["exits"] == [0]
    assert run_env["record_at_exit"] is None


def test_the_hard_exit_leaves_a_newer_servers_record(run_env):
    def run(server):
        # A server started after this one published its own record.
        write_discovery(port=2, token="newer", model="m")
        threading.Thread(target=run_env["on_gone"], daemon=True).start()
        run_env["released"].wait(5.0)

    run_env["run"] = run
    run_env["es"].run_embed_server(parent_pid=4242)
    assert run_env["exits"] == [0]
    assert run_env["record_at_exit"]["token"] == "newer"
    path = Path(os.environ["DURIN_HOME"]) / "embed-server.json"
    assert json.loads(path.read_text("utf-8"))["token"] == "newer"


def test_no_parent_named_starts_no_watch(run_env):
    run_env["run"] = lambda server: None
    run_env["es"].run_embed_server()
    assert "on_gone" not in run_env
