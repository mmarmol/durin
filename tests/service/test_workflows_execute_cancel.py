"""WorkflowsService.execute wires cooperative cancellation into the engine —
mirrors run_workflow.py's identical wiring (durin/agent/tools/run_workflow.py)
so tasks(action="stop") can reach an API/loop-launched run, not just an
agent-launched one. Before this, execute() called the engine with no
cancel_check/hard_cancel_check at all, so request_cancel(rid) was a no-op for
every run launched through this service (the HTTP run/launch routes, and any
loop that calls execute() directly).

Mirrors test_workflows_launch.py's fixture style: a single-script-node
workflow exercises the real engine with no LLM provider involved. The script
blocks on a file gate (rather than a fixed sleep) so cancellation is proven
against a genuinely-running subprocess, not just the engine's between-node
poll — and a short node `timeout` bounds the test's worst case if the fix
regresses.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.service.principal import Principal, Scope
from durin.service.workflows import WorkflowLaunchCommand, WorkflowsService
from durin.session.manager import SessionManager
from durin.workflow import cancellation, run_log
from durin.workflow.loader import workflows_dir


def _svc(tmp_path):
    app_config = SimpleNamespace(
        resolve_default_preset=lambda: object(),
        tools=ToolsConfig(),
        workflow=WorkflowConfig(),
    )
    return WorkflowsService(workspace=tmp_path, app_config=app_config,
                            sessions=SessionManager(workspace=tmp_path))


def _write_blocked_workflow(tmp_path, name, started_marker, gate_file):
    """A script node that blocks on a file gate until cancelled — proves the
    cancel flag actually reaches a RUNNING subprocess, not just the engine's
    between-node check (a script node polls the plain cancel_check while its
    subprocess is in flight; see durin/workflow/script_runner.py)."""
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    command = (
        f"touch {started_marker} && "
        f"while [ ! -f {gate_file} ]; do sleep 0.1; done"
    )
    (d / f"{name}.json").write_text(json.dumps({
        "name": name,
        "start": "wait",
        # Short timeout: if cancellation regresses, the node still ends on
        # its own within a few seconds instead of hanging the test/subprocess
        # for the engine's 300s default.
        "nodes": [{"id": "wait", "kind": "script", "command": command,
                   "timeout": 5, "next": None}],
    }), encoding="utf-8")


async def _wait_for(predicate, *, timeout=5.0, interval=0.02):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


async def _wait_for_manifest(tmp_path, name, run_id, *, timeout=5.0):
    elapsed = 0.0
    while elapsed < timeout:
        manifest = run_log.read_manifest(tmp_path, name, run_id)
        if manifest is not None and manifest.get("status") != "running":
            return manifest
        await asyncio.sleep(0.02)
        elapsed += 0.02
    raise AssertionError(f"run {run_id} of {name!r} never finished")


@pytest.mark.asyncio
async def test_stop_cancels_a_service_launched_run(tmp_path):
    """tasks(action='stop') sets the cancel flag by run_id; a run launched
    through the service (not through run_workflow) must obey it."""
    started_marker = tmp_path / "started.marker"
    gate_file = tmp_path / "gate.file"
    _write_blocked_workflow(tmp_path, "blocked", started_marker, gate_file)
    svc = _svc(tmp_path)
    principal = Principal.remote("tok1", frozenset({Scope.WORKFLOWS_WRITE.value}))

    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await svc.launch(WorkflowLaunchCommand(name="blocked", task="go"), principal)
        run_id = result.run_id

        assert await _wait_for(started_marker.exists), "script never started"

        cancellation.request_cancel(run_id)

        manifest = await _wait_for_manifest(tmp_path, "blocked", run_id)

    # Same status the engine reports for cooperative cancellation elsewhere
    # (see durin/workflow/engine.py's ScriptCancelled -> status="cancelled").
    assert manifest["status"] == "cancelled"
    # The flag is cleared once the run ends — the registry does not grow
    # without bound (mirrors run_workflow's identical `finally: clear(rid)`).
    assert not cancellation.is_cancelled(run_id)


@pytest.mark.asyncio
async def test_clear_cancel_runs_in_the_same_thread_as_engine_run(tmp_path, monkeypatch):
    """PR-K round 2 / ITEM 2: root-caused a rare full-suite failure of
    test_stop_cancels_a_service_launched_run — under genuine event-loop
    contention (many concurrent runs), a caller polling the manifest can
    observe status="cancelled" (or any terminal status) before the cancel
    flag is actually cleared. Reproduced with instrumentation (not guessed):
    a concurrent-load repro running 15 simultaneous launch+cancel scenarios
    per round hit the race in 13/150 runs; a serial/isolated repro of the
    same scenario (40 runs) never did — confirming the gap is real but only
    opens under scheduling contention, not a test logic bug.

    Root cause: execute() awaited `asyncio.to_thread(engine.run, ...)` and
    only called `_clear_cancel(rid)` in its OWN `finally`, AFTER that await
    resolves — which requires an asyncio round-trip (the worker thread
    signals completion via `call_soon_threadsafe`, then the event loop must
    actually process that callback) that has no ordering guarantee relative
    to a caller reading the manifest file `engine.run` already wrote
    synchronously, moments earlier, in the same worker thread. Under a busy
    ready queue that round-trip can lag long enough for an external
    poller — a `tasks(action='stop')` caller, or this test — to see the
    manifest as terminal first.

    Fix: clear the flag SYNCHRONOUSLY inside the SAME worker thread as
    engine.run, in engine.run's own immediate wrapper — eliminating the
    async round-trip for this specific ordering, not just narrowing it.
    Proven structurally (deterministic, not timing-dependent): clear() must
    run on the EXACT SAME thread engine.run executed on."""
    import threading

    from durin.workflow.engine import WorkflowEngine

    threads: dict[str, int] = {}
    real_run = WorkflowEngine.run

    def spy_run(self, *a, **kw):
        threads["engine.run"] = threading.get_ident()
        return real_run(self, *a, **kw)

    real_clear = cancellation.clear

    def spy_clear(rid):
        threads.setdefault("clear", threading.get_ident())
        return real_clear(rid)

    monkeypatch.setattr(WorkflowEngine, "run", spy_run)
    monkeypatch.setattr(cancellation, "clear", spy_clear)

    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "quick.json").write_text(json.dumps({
        "name": "quick", "start": "only",
        "nodes": [{"id": "only", "kind": "script", "command": "echo ok", "next": None}],
    }), encoding="utf-8")
    svc = _svc(tmp_path)

    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        await svc.execute("quick", "task")

    assert "engine.run" in threads and "clear" in threads
    assert threads["clear"] == threads["engine.run"], (
        "clear() ran on a different thread than engine.run — there is an "
        "async round-trip between the manifest write and the flag clear, "
        "which is exactly the TOCTOU window a caller can observe"
    )


@pytest.mark.asyncio
async def test_normal_completion_leaves_no_cancel_flag(tmp_path):
    """A run that completes without ever being cancelled must not leave a
    stale flag behind either."""
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "quick.json").write_text(json.dumps({
        "name": "quick", "start": "only",
        "nodes": [{"id": "only", "kind": "script", "command": "echo ok", "next": None}],
    }), encoding="utf-8")
    svc = _svc(tmp_path)

    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await svc.execute("quick", "task")

    assert result.status == "completed"
    assert not cancellation.is_cancelled(result.run_id)
