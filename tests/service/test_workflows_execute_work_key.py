"""WorkflowsService.execute forwards an optional work_key to the engine — the
production entrance for the reuse gate reached through the HTTP launch route and
the loops runtime (both call through execute()), not only the agent's own
run_workflow tool.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.service.types import ValidationFailedError
from durin.service.workflows import WorkflowsService
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
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name,
        "start": "only",
        "nodes": [{"id": "only", "kind": "script", "command": "echo ok", "next": None}],
    }), encoding="utf-8")


@pytest.mark.asyncio
async def test_execute_forwards_work_key_to_the_engine_manifest(tmp_path):
    _write_script_workflow(tmp_path, "w1")
    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await _svc(tmp_path).execute("w1", "task", work_key="ticket-1")

    manifest = run_log.read_manifest(tmp_path, "w1", result.run_id)
    assert manifest["work_key"] == "ticket-1"


@pytest.mark.asyncio
async def test_execute_without_work_key_leaves_it_none(tmp_path):
    _write_script_workflow(tmp_path, "w1")
    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await _svc(tmp_path).execute("w1", "task")

    manifest = run_log.read_manifest(tmp_path, "w1", result.run_id)
    assert manifest["work_key"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", ["../..", "a/b", ""])
async def test_execute_rejects_an_unsafe_work_key_before_the_engine_starts(tmp_path, bad_key):
    """Unlike the HTTP command's plain str field (where empty is
    indistinguishable from omitted), this parameter already has a clean
    "no key" sentinel — None — so an explicitly empty string here is a real
    key attempt, not an omission, matching WorkflowEngine.run's own
    is-not-None check for the same value (see test_work_key.py's
    test_work_key_rejects_empty_string at the engine layer). All three must
    raise BEFORE the engine ever runs (asyncio.to_thread is never reached),
    never as a bare ValueError surfacing from inside it."""
    _write_script_workflow(tmp_path, "w1")
    with patch("durin.providers.factory.make_provider") as fake_make_provider:
        with pytest.raises(ValidationFailedError) as exc_info:
            await _svc(tmp_path).execute("w1", "task", work_key=bad_key)
    assert "work_key" in str(exc_info.value)
    fake_make_provider.assert_not_called()   # rejected before any provider/engine setup
    assert run_log.list_runs(tmp_path, "w1") == []
