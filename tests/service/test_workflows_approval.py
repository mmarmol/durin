"""WorkflowsService.execute's approval-reply interpretation on the resume branch:
reject and approve-on-a-terminal-approval both finalize the EXISTING manifest IN
PLACE (durin.workflow.run_log.finalize_short_circuit) rather than rewriting it from
a fresh, empty-trace WorkflowResult — so the run's per-node trace and work_dir
survive the short-circuit. Neither path touches the engine again."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.providers.base import GenerationSettings, LLMProvider
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


async def _pause_at_approval(tmp_path, name, *, proposal):
    """Run the workflow for real, through the service, up to its approval pause —
    so the manifest carries a GENUINE non-empty per-node trace and work_dir, not a
    hand-built fixture. The only thing faked is the LLM turn itself (AgentRunner.run
    — the same seam tests/workflow/test_run_workflow_tool.py already uses)."""
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    # provider_key is a real (class-level) attribute on the spec, so an
    # unstubbed access returns an auto-MagicMock rather than raising — and
    # that MagicMock would otherwise ride into the manifest's per-node
    # "provider" field and blow up json.dumps. generation is instance-only
    # (set in LLMProvider.__init__, never called on a MagicMock), so it's
    # not strictly required, but stubbed too for a realistic provider shape.
    fake_provider.provider_key = "test-provider"
    fake_provider.generation = GenerationSettings()
    fake_result = AgentRunResult(final_content=proposal,
                                 messages=[{"role": "assistant", "content": proposal}])
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.runner.AgentRunner.run", AsyncMock(return_value=fake_result)):
        result = await _svc(tmp_path).execute(name, "chase invoice")
    assert result.status == "needs_input" and result.ask_kind == "approval"
    return result.run_id


@pytest.mark.asyncio
async def test_reject_finalizes_cancelled_preserving_the_manifest_trace(tmp_path):
    _write_approval_workflow(tmp_path, "w1")
    run_id = await _pause_at_approval(tmp_path, "w1", proposal="the drafted email")
    before = run_log.read_manifest(tmp_path, "w1", run_id)
    assert before["runs"]        # sanity: the pause really recorded a node run
    assert before["work_dir"]    # sanity: the pause really has a working folder

    result = await _svc(tmp_path).execute("w1", "reject", resume_run_id=run_id)

    assert result.status == "cancelled"
    assert result.rejected is True
    manifest = run_log.read_manifest(tmp_path, "w1", run_id)
    assert manifest["status"] == "cancelled"
    assert manifest["rejected"] is True
    assert manifest["needs_input_node"] is None
    assert manifest["ask_kind"] is None
    assert [r["node_id"] for r in manifest["runs"]] == [r["node_id"] for r in before["runs"]]
    assert manifest["work_dir"] == before["work_dir"]


@pytest.mark.asyncio
async def test_approve_on_terminal_approval_finalizes_completed_preserving_the_manifest_trace(tmp_path):
    _write_approval_workflow(tmp_path, "w2")
    run_id = await _pause_at_approval(tmp_path, "w2", proposal="the drafted email")
    before = run_log.read_manifest(tmp_path, "w2", run_id)
    assert before["runs"]
    assert before["work_dir"]

    result = await _svc(tmp_path).execute("w2", "approve", resume_run_id=run_id)

    assert result.status == "completed"
    assert result.final_output == "the drafted email"
    manifest = run_log.read_manifest(tmp_path, "w2", run_id)
    assert manifest["status"] == "completed"
    assert manifest["needs_input_node"] is None
    assert manifest["ask_kind"] is None
    assert [r["node_id"] for r in manifest["runs"]] == [r["node_id"] for r in before["runs"]]
    assert manifest["work_dir"] == before["work_dir"]
