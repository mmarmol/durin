"""Tests for loading a workflow definition from disk by name."""

import json

import pytest

from durin.workflow.loader import WorkflowNotFound, load_workflow, workflows_dir
from durin.workflow.spec import Workflow


def _write(workspace, name, data):
    d = workflows_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_loads_and_parses_named_workflow(tmp_path):
    _write(tmp_path, "demo", {
        "name": "demo", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    })
    wf = load_workflow(tmp_path, "demo")
    assert isinstance(wf, Workflow)
    assert wf.name == "demo"
    assert "a" in wf.nodes


def test_missing_workflow_raises_not_found(tmp_path):
    with pytest.raises(WorkflowNotFound, match="ghost"):
        load_workflow(tmp_path, "ghost")


def test_malformed_workflow_raises(tmp_path):
    _write(tmp_path, "bad", {"name": "bad", "nodes": []})  # no start, empty nodes
    with pytest.raises(Exception):  # WorkflowError from parse_workflow
        load_workflow(tmp_path, "bad")
