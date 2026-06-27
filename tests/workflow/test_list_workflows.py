"""Tests for the list_workflows agent tool (workspace discovery)."""

import json
from types import SimpleNamespace

import pytest

from durin.agent.tools.list_workflows import ListWorkflowsTool
from durin.workflow.loader import workflows_dir


def _tool(tmp_path):
    ctx = SimpleNamespace(workspace=str(tmp_path))
    return ListWorkflowsTool.create(ctx)


def _write_workflow(tmp_path, name, description):
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "description": description,
        "start": "a",
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    }
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_tool_metadata():
    tool = ListWorkflowsTool.create(SimpleNamespace(workspace="/tmp"))
    assert tool.name == "list_workflows"
    assert tool.read_only is True
    assert "query" in tool.parameters["properties"]
    assert "core" in ListWorkflowsTool._scopes


@pytest.mark.asyncio
async def test_lists_all_workflows_with_descriptions(tmp_path):
    _write_workflow(tmp_path, "alpha", "does the alpha thing")
    _write_workflow(tmp_path, "beta", "handles the zebra case")
    tool = _tool(tmp_path)

    out = await tool.execute()
    by_name = {w["name"]: w for w in out["workflows"]}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"]["description"] == "does the alpha thing"
    assert by_name["beta"]["description"] == "handles the zebra case"
    assert "run_workflow" in out["note"]


@pytest.mark.asyncio
async def test_query_filters_to_match(tmp_path):
    _write_workflow(tmp_path, "alpha", "does the alpha thing")
    _write_workflow(tmp_path, "beta", "handles the zebra case")
    tool = _tool(tmp_path)

    # matches on description text unique to one workflow
    out = await tool.execute(query="zebra")
    assert [w["name"] for w in out["workflows"]] == ["beta"]

    # matches on the workflow name as well
    out = await tool.execute(query="alpha")
    assert [w["name"] for w in out["workflows"]] == ["alpha"]


@pytest.mark.asyncio
async def test_empty_when_no_workflows_dir(tmp_path):
    tool = _tool(tmp_path)
    out = await tool.execute()
    assert out["workflows"] == []
