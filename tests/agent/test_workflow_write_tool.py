"""workflow_write validates the graph before persisting, refuses overwrites,
defaults improvement_mode to manual, and the result loads back through the loader."""
import asyncio
import json

import pytest

from durin.agent.tools.workflow_write import WorkflowWriteTool


def _valid_definition(description="answer a question"):
    return {
        "description": description,
        "start": "only",
        "input": {"text": True, "description": "a question"},
        "output": {"text": True, "description": "an answer"},
        "nodes": [{"id": "only", "title": "Only", "kind": "work",
                   "mode": "read", "tools": "none", "prompt": "Answer."}],
    }


def _run(tool, **kwargs):
    return json.loads(asyncio.run(tool.execute(**kwargs)))


def test_valid_definition_persists_and_loads(tmp_path):
    tool = WorkflowWriteTool(workspace=tmp_path)
    out = _run(tool, name="qa", definition=_valid_definition(), rationale="recurring QA fan-out")
    assert out.get("ok") is True

    from durin.workflow.loader import load_workflow
    wf = load_workflow(tmp_path, "qa")
    assert wf.name == "qa"
    assert wf.improvement_mode == "manual"          # defaulted, not left unset
    saved = json.loads((tmp_path / "workflows" / "qa.json").read_text(encoding="utf-8"))
    assert saved["name"] == "qa"                    # inner name kept consistent


def test_invalid_graph_is_rejected_verbatim(tmp_path):
    tool = WorkflowWriteTool(workspace=tmp_path)
    bad = _valid_definition()
    bad["start"] = "missing-node"
    out = _run(tool, name="broken", definition=bad, rationale="r")
    assert "invalid workflow" in out["error"]
    assert "missing-node" in out["error"]           # the schema failure, verbatim
    assert not (tmp_path / "workflows" / "broken.json").exists()


def test_overwrite_refused(tmp_path):
    tool = WorkflowWriteTool(workspace=tmp_path)
    assert _run(tool, name="qa", definition=_valid_definition(), rationale="r").get("ok")
    out = _run(tool, name="qa", definition=_valid_definition("v2"), rationale="r")
    assert "already exists" in out["error"]
    saved = json.loads((tmp_path / "workflows" / "qa.json").read_text(encoding="utf-8"))
    assert saved["description"] == "answer a question"   # first write untouched


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "."])
def test_unsafe_names_rejected(tmp_path, name):
    tool = WorkflowWriteTool(workspace=tmp_path)
    out = _run(tool, name=name, definition=_valid_definition(), rationale="r")
    assert out["error"] == "invalid workflow name"
