"""Tests for parsing a workflow JSON definition."""

import pytest

from durin.workflow.spec import (
    DecisionNode,
    WorkNode,
    Workflow,
    WorkflowError,
    parse_workflow,
)

_VALID = {
    "name": "demo",
    "start": "build",
    "max_visits": 2,
    "nodes": [
        {"id": "build", "kind": "work", "model": "fast", "context": "own",
         "prompt": "Write the code.", "next": "check"},
        {"id": "check", "kind": "decision", "command": "true",
         "on_pass": None, "on_fail": "build"},
    ],
}


def test_parse_valid_workflow():
    wf = parse_workflow(_VALID)
    assert wf.name == "demo"
    assert wf.start == "build"
    assert wf.max_visits == 2
    assert isinstance(wf.nodes["build"], WorkNode)
    assert wf.nodes["build"].model == "fast"
    assert wf.nodes["build"].context == "own"
    assert wf.nodes["build"].next == "check"
    assert isinstance(wf.nodes["check"], DecisionNode)
    assert wf.nodes["check"].command == "true"
    assert wf.nodes["check"].on_pass is None
    assert wf.nodes["check"].on_fail == "build"


def test_work_node_defaults():
    wf = parse_workflow({"name": "d", "start": "a",
                         "nodes": [{"id": "a", "kind": "work"}]})
    a = wf.nodes["a"]
    assert a.model is None        # None = engine default model
    assert a.context == "own"     # default context
    assert a.prompt == ""
    assert a.next is None         # None = end
    assert wf.max_visits == 3     # default loop cap


def test_unknown_start_raises():
    with pytest.raises(WorkflowError, match="start"):
        parse_workflow({"name": "d", "start": "missing",
                        "nodes": [{"id": "a", "kind": "work"}]})


def test_edge_to_unknown_node_raises():
    with pytest.raises(WorkflowError, match="unknown node"):
        parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "next": "ghost"}]})


def test_unknown_kind_raises():
    with pytest.raises(WorkflowError, match="kind"):
        parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "magic"}]})


def test_invalid_context_raises():
    with pytest.raises(WorkflowError, match="context"):
        parse_workflow({"name": "d", "start": "a",
                        "nodes": [{"id": "a", "kind": "work", "context": "sideways"}]})
