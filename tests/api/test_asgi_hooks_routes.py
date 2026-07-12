"""POST /api/v1/hooks/{hook} — the webhook trigger ingress route on the
unified gateway app (``build_gateway_http_app``). Mirrors the harness
``tests/api/test_unified_http.py`` uses for the bootstrap/media routes, with
a real ``HookDispatcher``/``TriggerMatcher``/``LoopsRuntime`` wired in so a
"fired" response leaves a real run manifest on disk."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus
from durin.loops import claims, run_log
from durin.loops.hooks import HookDispatcher
from durin.loops.matcher import TriggerMatcher
from durin.loops.runtime import LoopsRuntime
from durin.loops.spec import parse_loop
from durin.loops.store import save_loop
from durin.security.api_tokens import ApiTokenStore
from durin.workflow.result import WorkflowResult

HEADER = "X-Durin-Hook-Secret"


def _save_loop(ws, name="l1", **over):
    data = {
        "name": name, "workflow": "w1", "goal": {"intent": "it is done"},
        "triggers": [{"source": "webhook", "hook": "orders"}],
    } | over
    save_loop(ws, parse_loop(data))


def _wr(status, **kw):
    return WorkflowResult(status=status, final_output=kw.pop("out", "done"), run_id=kw.pop("run_id", "wf1"), **kw)


def _runtime(ws, results):
    async def workflow_exec(name, task, *, resume_run_id=None):
        return results.pop(0)

    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {a: True for a in assertions}}

    ids = iter([f"lr{i}" for i in range(100)])
    return LoopsRuntime(ws, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                         check_timeout_s=5, run_id_factory=lambda: next(ids))


def _build_app(tmp_path, monkeypatch, *, hook_dispatcher=None):
    # Isolate the persisted token store (the hooks secret + bootstrap live
    # here) to a tmp dir.
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    from durin.api.asgi import build_gateway_http_app
    from durin.channels.websocket import WebSocketChannel

    cfg = {
        "enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 8765,
        "path": "/", "websocketRequiresToken": False,
    }
    channel = WebSocketChannel(cfg, MessageBus())
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(channel, registry, auth=auth, hook_dispatcher=hook_dispatcher)
    return app, data_dir


def _hooks_secret(data_dir) -> str:
    return ApiTokenStore(path=data_dir / "api_tokens.json").get_or_create_hooks_secret()


def _wait_for_runs(ws, loop_name, *, timeout_s=2.0):
    """The matcher schedules `runtime.fire()` via `asyncio.create_task` on
    TestClient's background portal event loop, so the run manifest may not
    exist yet the instant `client.post(...)` returns — poll briefly instead
    of assuming it already landed."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        runs = run_log.list_runs(ws, loop_name, limit=None)
        if runs:
            return runs
        time.sleep(0.01)
    pytest.fail(f"no run manifest appeared for loop {loop_name!r} within {timeout_s}s")


@pytest.fixture()
def loops_ws(tmp_path):
    return tmp_path / "loops_ws"


def test_right_secret_fires_and_writes_a_run_manifest(tmp_path, monkeypatch, loops_ws):
    _save_loop(loops_ws)
    rt = _runtime(loops_ws, [_wr("completed")])
    dispatcher = HookDispatcher(TriggerMatcher(loops_ws, runtime=rt))
    app, data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=dispatcher)
    secret = _hooks_secret(data_dir)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/v1/hooks/orders", json={"text": "new order #42"}, headers={HEADER: secret},
    )

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"result": "fired", "loop": "l1"}

    runs = _wait_for_runs(loops_ws, "l1")
    assert len(runs) == 1
    assert runs[0]["status"] == "done"


def test_wrong_secret_is_401(tmp_path, monkeypatch, loops_ws):
    _save_loop(loops_ws)
    dispatcher = HookDispatcher(TriggerMatcher(loops_ws, runtime=_runtime(loops_ws, [_wr("completed")])))
    app, data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=dispatcher)
    _hooks_secret(data_dir)   # ensure a secret exists so "wrong" really is wrong
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/v1/hooks/orders", json={"text": "hi"}, headers={HEADER: "not-the-secret"}
    )
    assert resp.status_code == 401


def test_missing_secret_header_is_401(tmp_path, monkeypatch, loops_ws):
    _save_loop(loops_ws)
    dispatcher = HookDispatcher(TriggerMatcher(loops_ws, runtime=_runtime(loops_ws, [_wr("completed")])))
    app, _data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=dispatcher)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/v1/hooks/orders", json={"text": "hi"})
    assert resp.status_code == 401


def test_malformed_body_is_400(tmp_path, monkeypatch, loops_ws):
    _save_loop(loops_ws)
    dispatcher = HookDispatcher(TriggerMatcher(loops_ws, runtime=_runtime(loops_ws, [_wr("completed")])))
    app, data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=dispatcher)
    secret = _hooks_secret(data_dir)
    client = TestClient(app, raise_server_exceptions=False)

    # Not valid JSON at all.
    resp = client.post(
        "/api/v1/hooks/orders", headers={HEADER: secret, "Content-Type": "application/json"},
        content=b"not json",
    )
    assert resp.status_code == 400

    # Valid JSON but not an object.
    resp = client.post("/api/v1/hooks/orders", headers={HEADER: secret}, json=["not", "a", "dict"])
    assert resp.status_code == 400


def test_unknown_hook_is_404(tmp_path, monkeypatch, loops_ws):
    _save_loop(loops_ws)   # only registers hook "orders"
    dispatcher = HookDispatcher(TriggerMatcher(loops_ws, runtime=_runtime(loops_ws, [_wr("completed")])))
    app, data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=dispatcher)
    secret = _hooks_secret(data_dir)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/v1/hooks/shipments", json={"text": "hi"}, headers={HEADER: secret})
    assert resp.status_code == 404


def test_no_loops_at_all_is_404(tmp_path, monkeypatch, loops_ws):
    dispatcher = HookDispatcher(TriggerMatcher(loops_ws, runtime=_runtime(loops_ws, [])))
    app, data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=dispatcher)
    secret = _hooks_secret(data_dir)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/v1/hooks/orders", json={"text": "hi"}, headers={HEADER: secret})
    assert resp.status_code == 404


def test_no_dispatcher_wired_is_503(tmp_path, monkeypatch):
    app, data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=None)
    secret = _hooks_secret(data_dir)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/v1/hooks/orders", json={"text": "hi"}, headers={HEADER: secret})
    assert resp.status_code == 503


def test_busy_single_concurrency_queues(tmp_path, monkeypatch, loops_ws):
    _save_loop(loops_ws, concurrency="single")
    run_log.start_run(loops_ws, "l1", "run0", source="cron", task="t")
    rt = _runtime(loops_ws, [_wr("completed")])
    queued = []
    matcher = TriggerMatcher(loops_ws, runtime=rt, enqueue=lambda loop, ev: queued.append((loop, ev)))
    dispatcher = HookDispatcher(matcher)
    app, data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=dispatcher)
    secret = _hooks_secret(data_dir)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/v1/hooks/orders", json={"text": "hi"}, headers={HEADER: secret})

    assert resp.status_code == 202
    assert resp.json() == {"result": "queued", "loop": "l1"}
    assert len(queued) == 1


def test_correlate_wakes_a_waiting_run(tmp_path, monkeypatch, loops_ws):
    _save_loop(loops_ws, triggers=[{"source": "webhook", "hook": "orders",
                                     "correlate": r"ORDER-(\d+)"}])
    run_log.start_run(loops_ws, "l1", "run1", source="channel", task="t")
    run_log.finalize_run(loops_ws, "l1", "run1", status="waiting_info", ask="confirm?")
    claims.register(loops_ws, key="custom:l1:42", loop="l1", run_id="run1")
    rt = _runtime(loops_ws, [_wr("completed")])
    dispatcher = HookDispatcher(TriggerMatcher(loops_ws, runtime=rt))
    app, data_dir = _build_app(tmp_path, monkeypatch, hook_dispatcher=dispatcher)
    secret = _hooks_secret(data_dir)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/v1/hooks/orders", json={"text": "update for ORDER-42: shipped"},
        headers={HEADER: secret},
    )

    assert resp.status_code == 202
    assert resp.json() == {"result": "woken", "loop": "l1", "run_id": "run1"}
