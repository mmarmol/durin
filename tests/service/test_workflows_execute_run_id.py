"""WorkflowsService.execute forwards a caller-reserved run id to the engine.

A loop reserves the workflow run id so it can record it before the run
finishes. If the engine mints its own instead, the loop's manifest names a
run that never existed and the crash sweep misreads every orphan as
never-started.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.service.workflows import WorkflowsService
from durin.session.manager import SessionManager
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


@pytest.mark.asyncio
async def test_execute_uses_the_caller_supplied_run_id(tmp_path):
    _write_script_workflow(tmp_path, "w1")
    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await _svc(tmp_path).execute("w1", "task", run_id="reserved1234")

    assert result.run_id == "reserved1234"


@pytest.mark.asyncio
async def test_execute_without_a_run_id_still_mints_one(tmp_path):
    _write_script_workflow(tmp_path, "w1")
    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await _svc(tmp_path).execute("w1", "task")

    assert result.run_id and result.run_id != "reserved1234"
