"""workflow_edit: edit-only complement of workflow_write — refuses a missing
name, validates the replacement graph, commits with actor=agent."""
import asyncio
import json

from durin.agent.tools.workflow_edit import WorkflowEditTool
from durin.agent.tools.workflow_write import WorkflowWriteTool


def _definition(prompt="Answer."):
    return {
        "description": "answer a question",
        "start": "only",
        "nodes": [{"id": "only", "title": "Only", "kind": "work",
                   "mode": "read", "tools": "none", "prompt": prompt}],
    }


def _run(tool, **kwargs):
    return json.loads(asyncio.run(tool.execute(**kwargs)))


def test_edit_refuses_missing_workflow(tmp_path):
    out = _run(WorkflowEditTool(workspace=tmp_path),
               name="ghost", definition=_definition(), rationale="r")
    assert "does not exist" in out["error"]
    assert "workflow_write" in out["error"]          # points at the create door


def test_edit_existing_persists_and_versions(tmp_path):
    assert _run(WorkflowWriteTool(workspace=tmp_path),
                name="qa", definition=_definition(), rationale="create").get("ok")
    out = _run(WorkflowEditTool(workspace=tmp_path),
               name="qa", definition=_definition("Answer with sources."), rationale="tighten prompt")
    assert out.get("ok") is True

    from durin.workflow.loader import load_workflow
    from durin.workflow.version_store import WorkflowVersionStore, history_for_dream
    wf = load_workflow(tmp_path, "qa")
    assert wf.nodes["only"].prompt == "Answer with sources."
    hist = history_for_dream(tmp_path, "qa")
    assert any("tighten prompt" in h.get("reason", "") for h in hist)


def test_edit_rejects_invalid_graph_and_leaves_file_untouched(tmp_path):
    assert _run(WorkflowWriteTool(workspace=tmp_path),
                name="qa", definition=_definition(), rationale="create").get("ok")
    bad = _definition()
    bad["start"] = "missing-node"
    out = _run(WorkflowEditTool(workspace=tmp_path),
               name="qa", definition=bad, rationale="r")
    assert "invalid workflow" in out["error"]
    from durin.workflow.loader import load_workflow
    assert load_workflow(tmp_path, "qa").nodes["only"].prompt == "Answer."
