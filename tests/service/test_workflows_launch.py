"""WorkflowsService.launch — the detached run entry behind
``POST /api/v1/workflows/{name}/runs``.

Mirrors test_workflows_execute_run_id.py's fixture style: a single-script-node
workflow exercises the real engine with no LLM provider involved.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.service.principal import Principal, Scope
from durin.service.types import ForbiddenError, NotFoundError
from durin.service.workflows import WorkflowLaunchCommand, WorkflowsService
from durin.session.manager import SessionManager
from durin.workflow import run_log
from durin.workflow.loader import workflows_dir


def _svc(tmp_path):
    app_config = SimpleNamespace(
        resolve_default_preset=lambda: object(),
        tools=ToolsConfig(),
        workflow=WorkflowConfig(),
    )
    return WorkflowsService(workspace=tmp_path, app_config=app_config,
                            sessions=SessionManager(workspace=tmp_path))


def _write_script_workflow(tmp_path, name):
    """A single script node: exercises the engine with no provider involved."""
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name,
        "start": "only",
        "nodes": [{"id": "only", "kind": "script", "command": "echo ok", "next": None}],
    }), encoding="utf-8")


async def _wait_for_manifest(tmp_path, name, run_id, *, timeout=5.0):
    """Poll until the background run finishes and its manifest lands."""
    elapsed = 0.0
    while elapsed < timeout:
        manifest = run_log.read_manifest(tmp_path, name, run_id)
        if manifest is not None and manifest.get("status") != "running":
            return manifest
        await asyncio.sleep(0.01)
        elapsed += 0.01
    raise AssertionError(f"run {run_id} of {name!r} never finished")


@pytest.mark.asyncio
async def test_launch_unknown_workflow_raises_not_found(tmp_path):
    svc = _svc(tmp_path)
    principal = Principal.remote("tok1", frozenset({Scope.WORKFLOWS_WRITE.value}))
    with pytest.raises(NotFoundError):
        await svc.launch(WorkflowLaunchCommand(name="ghost", task="x"), principal)


@pytest.mark.asyncio
async def test_launch_without_scope_raises_forbidden(tmp_path):
    _write_script_workflow(tmp_path, "w1")
    svc = _svc(tmp_path)
    principal = Principal.remote("tok1", frozenset({Scope.WORKFLOWS_READ.value}))
    with pytest.raises(ForbiddenError):
        await svc.launch(WorkflowLaunchCommand(name="w1", task="x"), principal)


@pytest.mark.asyncio
async def test_launch_returns_run_id_before_the_run_finishes(tmp_path):
    """202-style contract: launch() itself must not wait for the engine."""
    _write_script_workflow(tmp_path, "w1")
    svc = _svc(tmp_path)
    principal = Principal.remote("tok1", frozenset({Scope.WORKFLOWS_WRITE.value}))

    release = asyncio.Event()
    started = asyncio.Event()

    async def _slow_execute(name, task, *, run_id=None, root_session_key=None, **kw):
        started.set()
        await release.wait()
        return SimpleNamespace(run_id=run_id)

    with patch.object(WorkflowsService, "execute", _slow_execute):
        result = await svc.launch(WorkflowLaunchCommand(name="w1", task="x"), principal)

    # launch() returned WITHOUT release ever being set — the run is still
    # blocked inside its background task.
    assert result.run_id
    assert not release.is_set()

    release.set()  # let the background task drain so it doesn't leak into other tests
    for _ in range(200):
        if not svc._bg_tasks:
            break
        await asyncio.sleep(0.01)
    assert not svc._bg_tasks


@pytest.mark.asyncio
async def test_launch_manifest_carries_root_session_key_naming_the_principal(tmp_path):
    _write_script_workflow(tmp_path, "w1")
    svc = _svc(tmp_path)
    principal = Principal.remote("tok-abc123", frozenset({Scope.WORKFLOWS_WRITE.value}))

    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await svc.launch(WorkflowLaunchCommand(name="w1", task="x"), principal)
        manifest = await _wait_for_manifest(tmp_path, "w1", result.run_id)

    assert manifest["root_session_key"] == "api:tok-abc123"
