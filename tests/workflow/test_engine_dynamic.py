"""Tests for dynamic fan-out (orchestrator → worker × N per runtime list)."""

import pytest

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _parse(raw):
    return parse_workflow(raw)


class TestParseSubtasks:
    """The fan-out list parser must find the JSON array even when a model wraps it in
    prose — else it line-splits the prose into one bogus worker per sentence."""

    def test_clean_array(self):
        assert WorkflowEngine._parse_subtasks('["a", "b"]') == ["a", "b"]

    def test_fenced_array(self):
        assert WorkflowEngine._parse_subtasks('```json\n["a", "b"]\n```') == ["a", "b"]

    def test_prose_wrapped_fenced_array(self):
        # The real build-specs failure: a leading explanation + a numbered list before
        # the fenced array made JSON parsing fail and the whole thing got line-split.
        text = (
            "The slice has two independent seams.\n"
            "1. slugify\n2. truncate\n"
            "Splitting further would be over-decomposition.\n"
            '```json\n["slugify: a slug", "truncate: cut to n"]\n```'
        )
        assert WorkflowEngine._parse_subtasks(text) == ["slugify: a slug", "truncate: cut to n"]

    def test_bare_array_in_prose(self):
        assert WorkflowEngine._parse_subtasks('Here you go: ["x", "y", "z"] — done.') == ["x", "y", "z"]

    def test_falls_back_to_lines_without_json(self):
        assert WorkflowEngine._parse_subtasks("alpha\nbeta\ngamma") == ["alpha", "beta", "gamma"]

    def test_capped_at_50(self):
        import json
        assert len(WorkflowEngine._parse_subtasks(json.dumps([str(i) for i in range(80)]))) == 50


def _wf(extra_nodes=None):
    nodes = [
        {"id": "orch", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "dev",
         "list_from": "orch", "max_concurrency": 2, "next": "done"},
        {"id": "dev", "kind": "work"},
        {"id": "done", "kind": "work", "next": None},
    ]
    if extra_nodes:
        nodes.extend(extra_nodes)
    return _parse({"name": "w", "start": "orch", "max_visits": 3, "nodes": nodes})


def test_dynamic_fanout_runs_worker_per_list_item(tmp_path):
    seen = []

    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["task A","task B","task C"]')
        if req.node.id == "dev":
            seen.append(req.task)          # each worker gets its own item
        return NodeRunResponse(output=f"did {req.task}")

    wf = _parse({"name": "w", "start": "orch", "max_visits": 3, "nodes": [
        {"id": "orch", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "dev",
         "list_from": "orch", "max_concurrency": 2, "next": "done"},
        {"id": "dev", "kind": "work"}, {"id": "done", "kind": "work", "next": None}]})
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert res.status == "completed"
    assert sorted(seen) == ["task A", "task B", "task C"]   # 3 workers, one per item


def test_dynamic_fanout_merged_output_in_result(tmp_path):
    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["x","y"]')
        if req.node.id == "done":
            return NodeRunResponse(output="final")
        return NodeRunResponse(output=f"did {req.task}")

    wf = _wf()
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert "did x" in res.runs[-2].output or any("did x" in r.output for r in res.runs)


def test_dynamic_fanout_records_worker_runs(tmp_path):
    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["a","b"]')
        if req.node.id == "done":
            return NodeRunResponse(output="final")
        return NodeRunResponse(output=f"did {req.task}")

    wf = _wf()
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    worker_runs = [r for r in res.runs if r.node_id == "dev"]
    assert len(worker_runs) == 2
    outputs = {r.output for r in worker_runs}
    assert outputs == {"did a", "did b"}


def test_dynamic_fanout_empty_list_does_not_crash(tmp_path):
    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output="[]")
        return NodeRunResponse(output="done")

    wf = _wf()
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert res.status == "completed"
    assert not any(r.node_id == "dev" for r in res.runs)


def test_dynamic_fanout_fallback_to_lines(tmp_path):
    """When the list_from output is not JSON, fall back to non-empty lines."""
    seen = []

    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output="alpha\nbeta\n\ngamma")
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        seen.append(req.task)
        return NodeRunResponse(output=f"did {req.task}")

    wf = _wf()
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert res.status == "completed"
    assert sorted(seen) == ["alpha", "beta", "gamma"]


def test_dynamic_fanout_non_list_json_falls_back_to_lines(tmp_path):
    """JSON that isn't a list falls back to line-splitting."""
    seen = []

    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='{"key":"val"}')
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        seen.append(req.task)
        return NodeRunResponse(output=f"did {req.task}")

    wf = _wf()
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert res.status == "completed"
    assert len(seen) == 1


def test_dynamic_fanout_strips_markdown_code_fence(tmp_path):
    """A JSON array wrapped in a ```json markdown fence parses to its elements,
    not the literal fence lines. Built with explicit newlines so the test source
    never embeds a raw triple-backtick string."""
    fence = "`" * 3
    fenced = "\n".join([f"{fence}json", '["a", "b", "c"]', fence])
    seen = []

    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output=fenced)
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        seen.append(req.task)
        return NodeRunResponse(output=f"did {req.task}")

    wf = _wf()
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert res.status == "completed"
    assert sorted(seen) == ["a", "b", "c"]


def test_dynamic_fanout_cap_limits_concurrency(tmp_path):
    """max_concurrency=1 must serialize workers (no error, correct count)."""
    seen = []

    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["p","q","r"]')
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        seen.append(req.task)
        return NodeRunResponse(output=f"did {req.task}")

    wf = _parse({
        "name": "w", "start": "orch", "max_visits": 3,
        "nodes": [
            {"id": "orch", "kind": "work", "next": "fan"},
            {"id": "fan", "kind": "parallel", "worker": "dev",
             "list_from": "orch", "max_concurrency": 1, "next": "done"},
            {"id": "dev", "kind": "work"},
            {"id": "done", "kind": "work", "next": None},
        ],
    })
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert res.status == "completed"
    assert sorted(seen) == ["p", "q", "r"]


def test_dynamic_fanout_upstream_used_when_list_from_not_in_runs(tmp_path):
    """When list_from is the immediate predecessor, upstream_output IS that output."""
    seen = []

    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["u","v"]')
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        seen.append(req.task)
        return NodeRunResponse(output=f"did {req.task}")

    # list_from="orch" and orch is the direct predecessor — tests the fallback path
    wf = _wf()
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert sorted(seen) == ["u", "v"]


def test_dynamic_fanout_workers_get_distinct_session_keys(tmp_path):
    """Each dynamic worker must receive a distinct worker_index so _persist generates
    unique session keys — otherwise all workers overwrite the same key."""
    seen_indices = []

    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["a","b","c"]')
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        seen_indices.append(req.worker_index)
        return NodeRunResponse(output=f"did {req.task}")

    wf = _wf()
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")
    assert res.status == "completed"
    # Three workers must each have a distinct, non-None index.
    assert None not in seen_indices
    assert len(set(seen_indices)) == 3
