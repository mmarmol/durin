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

from durin.utils.process_tree import process_identity, process_state
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
    except PermissionError:
        return True  # a process exists under that pid, though not ours
    return True


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return not _alive(pid)


class _Watched:
    """An intermediate process that starts a process watching it.

    ``start`` returns once the watching process has started its watch (the
    import of durin takes seconds, and a parent killed earlier would go
    unnoticed only because the test was too quick). ``close`` ends whatever
    is left and signals the watching process only while it is still the
    process first seen under that pid.
    """

    def __init__(self, mode: str, ready: Path) -> None:
        self.mode = mode
        self.ready = ready
        self.inter: subprocess.Popen | None = None
        self.child_pid: int | None = None
        self.child_started: str | None = None

    def start(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
        self.inter = subprocess.Popen(
            [sys.executable, "-c", _INTERMEDIATE, _WATCHED, self.mode,
             str(self.ready)],
            stdout=subprocess.PIPE, env=env, text=True)
        self.child_pid = int(self.inter.stdout.readline())
        self.child_started = process_identity(self.child_pid)["started"]
        deadline = time.monotonic() + 60.0
        while not self.ready.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert self.ready.exists(), "the watching process never started its watch"

    def close(self) -> None:
        if self.child_pid is not None and self.child_started is not None:
            if process_state(self.child_pid, self.child_started) == "alive":
                try:
                    os.kill(self.child_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self.inter is not None:
            if self.inter.poll() is None:
                self.inter.kill()
            self.inter.wait(timeout=10)
            if self.inter.stdout is not None:
                self.inter.stdout.close()


def test_watched_process_ends_when_its_parent_is_killed(tmp_path):
    w = _Watched("wait", tmp_path / "ready")
    try:
        w.start()
        assert _alive(w.child_pid)
        w.inter.kill()
        w.inter.wait(timeout=10)
        assert _wait_gone(w.child_pid, 8.0), "watched process outlived its parent"
    finally:
        w.close()


def test_watched_process_ends_when_its_parent_exits_normally(tmp_path):
    w = _Watched("exit", tmp_path / "ready")
    try:
        w.start()
        w.inter.wait(timeout=10)
        assert _wait_gone(w.child_pid, 8.0), "watched process outlived its parent"
    finally:
        w.close()


def test_watched_process_is_untouched_while_its_parent_lives(tmp_path):
    w = _Watched("wait", tmp_path / "ready")
    try:
        w.start()
        time.sleep(1.5)  # several poll intervals
        assert _alive(w.child_pid)
    finally:
        w.close()


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

    path.write_bytes(b"\xff\xfe\x00 not utf-8")
    clear_discovery(token="mine")
    assert path.exists()

    write_discovery(port=1, token="other", model="m")
    clear_discovery()
    assert not path.exists()


class _FakeProvider:
    model_name = "fake/test-embed"
    _isolation = "service"
    on_embed = None

    def embed(self, texts):
        if self.on_embed is not None:
            self.on_embed()
        return [[0.0] for _ in texts]


@pytest.fixture()
def run_env(tmp_path, monkeypatch):
    """run_embed_server with the model, uvicorn and the watch patched out.

    ``fire()`` runs the parent-gone handler on a thread the fixture keeps;
    at teardown, before any patch is undone, every such thread is joined
    and must have ended, so a handler can never reach the real
    ``os._exit`` or another test's state.
    """
    import uvicorn

    import durin.config
    import durin.memory.embed_server as es
    import durin.memory.embedding

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    record = tmp_path / "embed-server.json"
    provider = _FakeProvider()
    state: dict = {
        "exits": [], "released": threading.Event(), "events": [],
        "threads": [], "provider": provider, "record": record,
    }

    monkeypatch.setattr(durin.config, "load_config", lambda: object())

    def fake_provider(config, model=None):
        state["events"].append("provider")
        return provider

    monkeypatch.setattr(durin.memory.embedding, "provider_from_config",
                        fake_provider)
    monkeypatch.setattr(es, "embed_server_config", lambda app: object())

    def fake_watch(parent_pid, on_gone, **kw):
        state["events"].append("watch")
        state["parent_pid"] = parent_pid
        state["on_gone"] = on_gone
        return None

    monkeypatch.setattr(es, "exit_when_parent_is_gone", fake_watch)

    def fake_exit(code):
        state["exits"].append(code)
        state["record_at_exit"] = (
            json.loads(record.read_text("utf-8")) if record.exists() else None)
        state["released"].set()

    monkeypatch.setattr(es.os, "_exit", fake_exit)
    monkeypatch.setattr(es, "_PARENT_EXIT_GRACE_S", 0.3)

    class _Server:
        def __init__(self, config):
            state["server"] = self
            self._should_exit = False

        @property
        def should_exit(self):
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value):
            state["record_when_stop_asked"] = (
                json.loads(record.read_text("utf-8"))
                if record.exists() else None)
            self._should_exit = value

        def run(self, sockets=None):
            state["ran"] = True
            state["run"](self)

    monkeypatch.setattr(uvicorn, "Server", _Server)

    def fire():
        t = threading.Thread(target=state["on_gone"], daemon=True)
        state["threads"].append(t)
        t.start()
        return t

    state["fire"] = fire
    state["es"] = es
    yield state
    stuck = []
    for t in state["threads"]:
        t.join(5.0)
        if t.is_alive():
            stuck.append(t)
    assert not stuck, "a parent-gone handler thread is still running"


def test_parent_gone_asks_the_http_server_to_stop(run_env):
    def run(server):
        run_env["fire"]()
        deadline = time.monotonic() + 2.0
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.01)
        run_env["should_exit_seen"] = server.should_exit
        # A server that stops: run returns before the grace runs out.

    run_env["run"] = run
    run_env["es"].run_embed_server(parent_pid=4242)
    assert run_env["parent_pid"] == 4242
    assert run_env["should_exit_seen"] is True
    assert not run_env["record"].exists()


def test_the_own_record_goes_before_the_http_server_is_asked_to_stop(run_env):
    def run(server):
        run_env["fire"]().join(5.0)

    run_env["run"] = run
    run_env["es"].run_embed_server(parent_pid=4242)
    assert run_env["record_when_stop_asked"] is None


def test_a_server_that_does_not_stop_is_ended_after_the_grace(run_env):
    def run(server):
        run_env["fire"]()
        run_env["released"].wait(5.0)

    run_env["run"] = run
    run_env["es"].run_embed_server(parent_pid=4242)
    assert run_env["exits"] == [0]
    assert run_env["record_at_exit"] is None


def test_the_hard_exit_leaves_a_newer_servers_record(run_env):
    def run(server):
        # A server started after this one published its own record.
        write_discovery(port=2, token="newer", model="m")
        run_env["fire"]()
        run_env["released"].wait(5.0)

    run_env["run"] = run
    run_env["es"].run_embed_server(parent_pid=4242)
    assert run_env["exits"] == [0]
    assert run_env["record_at_exit"]["token"] == "newer"
    assert json.loads(run_env["record"].read_text("utf-8"))["token"] == "newer"
    assert run_env["record_when_stop_asked"]["token"] == "newer"


def test_the_exit_happens_even_when_clearing_the_record_fails(
        run_env, monkeypatch):
    def run(server):
        run_env["fire"]()
        run_env["released"].wait(5.0)

    def boom(*, token=None):
        raise RuntimeError("clear failed")

    run_env["run"] = run
    monkeypatch.setattr(run_env["es"], "clear_discovery", boom)
    try:
        run_env["es"].run_embed_server(parent_pid=4242)
    except RuntimeError:
        pass  # the failing clear in the main thread's finally
    assert run_env["exits"] == [0]


def test_a_parent_gone_during_the_warm_up_ends_the_process_at_once(run_env):
    newer = run_env["record"]
    write_discovery(port=2, token="newer", model="m")
    run_env["provider"].on_embed = lambda: run_env["fire"]().join(5.0)
    run_env["run"] = lambda server: pytest.fail("the HTTP server must not run")

    run_env["es"].run_embed_server(parent_pid=4242)

    assert run_env["exits"] == [0]
    assert "ran" not in run_env
    assert json.loads(newer.read_text("utf-8"))["token"] == "newer"


def test_nothing_is_published_once_the_parent_is_known_to_be_gone(
        run_env, monkeypatch):
    es = run_env["es"]
    real_build = es.build_embed_app

    def build_then_parent_gone(*args, **kwargs):
        app = real_build(*args, **kwargs)
        run_env["fire"]().join(5.0)
        return app

    monkeypatch.setattr(es, "build_embed_app", build_then_parent_gone)
    write_discovery(port=2, token="newer", model="m")
    run_env["run"] = lambda server: pytest.fail("the HTTP server must not run")

    es.run_embed_server(parent_pid=4242)

    assert run_env["exits"] == [0]
    assert "ran" not in run_env
    assert json.loads(run_env["record"].read_text("utf-8"))["token"] == "newer"


def test_the_watch_starts_before_the_provider_is_built(run_env):
    run_env["run"] = lambda server: None
    run_env["es"].run_embed_server(parent_pid=4242)
    assert run_env["events"][:2] == ["watch", "provider"]


def test_no_parent_named_starts_no_watch(run_env):
    run_env["run"] = lambda server: None
    run_env["es"].run_embed_server()
    assert "on_gone" not in run_env
