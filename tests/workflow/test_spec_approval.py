import pytest
from durin.workflow.spec import parse_workflow, WorkflowError


def _wf(nodes):
    return {"name": "d", "start": nodes[0]["id"], "nodes": nodes}


def test_approval_parses_on_linear_work_node():
    wf = parse_workflow(_wf([
        {"id": "draft", "kind": "work", "approval": True, "next": "send"},
        {"id": "send", "kind": "work", "next": None},
    ]))
    assert wf.nodes["draft"].approval is True
    assert wf.nodes["send"].approval is False


@pytest.mark.parametrize("extra", [
    {"on_pass": None, "on_fail": "x"},
    {"cases": {"A": None}},
    {"detached": True},
    {"context": "shared"},
])
def test_approval_rejects_incompatible_shapes(extra):
    node = {"id": "draft", "kind": "work", "approval": True, "next": None} | extra
    nodes = [node] + ([{"id": "x", "kind": "work", "next": None}] if "on_fail" in extra else [])
    with pytest.raises(WorkflowError):
        parse_workflow(_wf(nodes))


def test_approval_rejected_on_script_node():
    with pytest.raises(WorkflowError):
        parse_workflow(_wf([
            {"id": "s", "kind": "script", "command": "true", "approval": True, "next": None},
        ]))


def test_approval_rejected_on_parallel_unit():
    with pytest.raises(WorkflowError):
        parse_workflow(_wf([
            {"id": "p", "kind": "parallel", "branches": ["b"], "next": None},
            {"id": "b", "kind": "work", "approval": True},
        ]))
