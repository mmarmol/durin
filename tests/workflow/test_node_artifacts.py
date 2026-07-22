from pathlib import Path

from durin.workflow import run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _wf():
    return parse_workflow({"name": "wf", "start": "writer", "nodes": [
        {"id": "writer", "kind": "work", "tools": "default", "next": "gate"},
        {"id": "gate", "kind": "work", "next": None},
    ]})


def _run(tmp_path, write):
    def _runner(req):
        if req.node.id == "writer" and req.output_dir:
            write(Path(req.output_dir))
        return NodeRunResponse(output="ok", session_key=None, messages=[])

    engine = WorkflowEngine(node_runner=_runner, workspace=str(tmp_path))
    result = engine.run(_wf(), "t")
    return run_log.read_manifest(tmp_path, "wf", result.run_id)


def test_a_node_records_the_files_it_created(tmp_path):
    """The working folder is shared by every node, so attribution only exists if
    it is captured per node — otherwise every file looks like everyone's."""
    rec = _run(tmp_path, lambda d: (d / "context.json").write_text("{}", encoding="utf-8"))
    writer = next(r for r in rec["runs"] if r["node_id"] == "writer")
    assert writer["artifacts"] == ["context.json"]


def test_a_node_that_writes_nothing_records_an_empty_list(tmp_path):
    rec = _run(tmp_path, lambda d: (d / "context.json").write_text("{}", encoding="utf-8"))
    gate = next(r for r in rec["runs"] if r["node_id"] == "gate")
    assert gate["artifacts"] == []


def test_artifacts_are_capped(tmp_path):
    """A fan-out writing hundreds of files must not bloat every manifest rewrite."""
    def _many(d):
        for i in range(25):
            (d / f"f{i:02d}.txt").write_text("x", encoding="utf-8")

    rec = _run(tmp_path, _many)
    writer = next(r for r in rec["runs"] if r["node_id"] == "writer")
    assert len(writer["artifacts"]) == 20
