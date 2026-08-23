"""WorkflowsService.execute's approval-reply interpretation on the resume branch:
reject and approve-on-a-terminal-approval both finalize the manifest directly —
neither touches the engine (see durin/workflow/approval.py and the ask_kind=="approval"
branch of execute())."""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from durin.config.schema import ToolsConfig, WorkflowConfig
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


def _write_approval_workflow(tmp_path, name):
    """A single work node flagged for approval, with no next — approving it
    completes the run instead of resuming into another node."""
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name, "start": "draft",
        "nodes": [{"id": "draft", "kind": "work", "approval": True, "next": None}],
    }), encoding="utf-8")


def _write_approval_manifest(tmp_path, name, run_id, *, final_output):
    """A manifest paused at the approval node, as the engine's own approval pause
    would have written it (schema built by hand — this is a fixture, not a
    round-trip of the engine's writer)."""
    d = run_log.runs_root(tmp_path) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps({
        "run_id": run_id, "workflow": name, "status": "needs_input",
        "ask_kind": "approval", "needs_input_node": "draft",
        "final_output": final_output, "resume_upstream": "chase invoice",
        "work_key": None, "runs": [{"node_id": "draft", "iteration": 1}],
        "root_session_key": None, "started_at": 0.0, "task": "chase invoice",
    }), encoding="utf-8")


@pytest.mark.asyncio
async def test_reject_finalizes_cancelled_without_running_the_engine(tmp_path):
    _write_approval_workflow(tmp_path, "w1")
    _write_approval_manifest(tmp_path, "w1", "r1", final_output="the drafted email")

    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await _svc(tmp_path).execute("w1", "reject", resume_run_id="r1")

    assert result.status == "cancelled"
    assert result.rejected is True
    manifest = run_log.read_manifest(tmp_path, "w1", "r1")
    assert manifest["status"] == "cancelled"
    assert manifest["rejected"] is True


@pytest.mark.asyncio
async def test_approve_on_terminal_approval_finalizes_completed(tmp_path):
    _write_approval_workflow(tmp_path, "w1")
    _write_approval_manifest(tmp_path, "w1", "r1", final_output="the drafted email")

    with patch("durin.providers.factory.make_provider", return_value=SimpleNamespace(
            get_default_model=lambda: "m")):
        result = await _svc(tmp_path).execute("w1", "approve", resume_run_id="r1")

    assert result.status == "completed"
    assert result.final_output == "the drafted email"
    manifest = run_log.read_manifest(tmp_path, "w1", "r1")
    assert manifest["status"] == "completed"
