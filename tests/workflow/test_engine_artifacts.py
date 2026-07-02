from pathlib import Path
from durin.workflow.engine import WorkflowEngine, NodeRunResponse
from durin.workflow.spec import parse_workflow


def _wf(nodes, start):
    return parse_workflow({"name": "w", "start": start, "nodes": nodes})


def test_sequential_nodes_share_one_working_folder(tmp_path):
    seen = {}
    def runner(req):
        seen[req.node.id] = req.output_dir
        return NodeRunResponse(output=f"out-{req.node.id}")
    wf = _wf([
        {"id": "a", "kind": "work", "tools": "default", "next": "b"},
        {"id": "b", "kind": "work", "tools": "default", "next": None},
    ], "a")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    assert seen["a"] == seen["b"]                # one shared working folder for the run
    assert seen["a"].endswith("/work")


def test_loop_back_keeps_one_shared_working_folder(tmp_path):
    calls = []
    def runner(req):
        calls.append((req.node.id, req.output_dir))
        # gate fails once then passes; producer 'make' loops
        if req.node.id == "gate":
            return NodeRunResponse(output="PASS" if [c[0] for c in calls].count("gate") > 1 else "FAIL")
        return NodeRunResponse(output="draft")
    wf = _wf([
        {"id": "make", "kind": "work", "tools": "default", "next": "gate"},
        {"id": "gate", "kind": "work", "tools": "default", "prompt": "ok?", "on_pass": "done", "on_fail": "make"},
        {"id": "done", "kind": "work", "tools": "default", "next": None},
    ], "make")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    dirs = {d for _, d in calls}
    assert len(dirs) == 1                        # make#1, gate, make#2, gate, done → one folder
    assert next(iter(dirs)).endswith("/work")


def test_inert_without_workspace(tmp_path):
    seen = {}
    def runner(req):
        seen["a"] = req.output_dir
        return NodeRunResponse(output="x")
    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    WorkflowEngine(runner).run(wf, "t")         # no workspace=
    assert seen["a"] is None                    # feature inert, no folder


def test_no_tools_node_gets_no_folder_but_keeps_the_shared_dir(tmp_path):
    # A no-tools agent node does no file I/O, so it gets no folder — but it must NOT break
    # the shared working dir: the node after it works in the same folder as the nodes before.
    seen = {}
    def runner(req):
        seen[req.node.id] = req.output_dir
        return NodeRunResponse(output=f"out-{req.node.id}")
    wf = _wf([
        {"id": "a", "kind": "work", "tools": "default", "next": "mid"},
        {"id": "mid", "kind": "work", "next": "b"},          # tools defaults to "none"
        {"id": "b", "kind": "work", "tools": "default", "next": None},
    ], "a")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    assert seen["mid"] is None                   # no-tools node: no folder
    assert seen["a"] == seen["b"]                # ...and a and b still share one folder


# input_files + output_dir

def test_input_files_seeded_into_the_shared_working_folder(tmp_path):
    """input_files are copied into the run's shared working folder, which the start node
    receives as its working directory (output_dir)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "report.txt").write_text("hello")
    (src / "data.csv").write_text("a,b\n1,2")

    seen = {}
    def runner(req):
        seen[req.node.id] = req.output_dir
        return NodeRunResponse(output=f"out-{req.node.id}")

    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(
        wf, "t", input_files=[str(src / "report.txt"), str(src / "data.csv")]
    )

    folder = Path(seen["a"])
    assert folder.is_dir()
    assert (folder / "report.txt").read_text() == "hello"
    assert (folder / "data.csv").read_text() == "a,b\n1,2"


def test_no_input_files_gives_an_empty_shared_working_folder(tmp_path):
    """With a workspace but no input_files, the start node still works in the shared folder,
    which begins empty."""
    seen = {}
    def runner(req):
        seen[req.node.id] = req.output_dir
        return NodeRunResponse(output="x")

    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    assert seen["a"].endswith("/work")
    assert list(Path(seen["a"]).iterdir()) == []   # nothing seeded


def test_output_dir_reflects_the_shared_working_folder(tmp_path):
    """WorkflowResult.output_dir is the run's shared working folder."""
    seen = {}
    def runner(req):
        seen[req.node.id] = req.output_dir
        return NodeRunResponse(output=f"out-{req.node.id}")

    wf = _wf([
        {"id": "a", "kind": "work", "tools": "default", "next": "b"},
        {"id": "b", "kind": "work", "tools": "default", "next": None},
    ], "a")
    result = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    assert result.output_dir == seen["b"]
    assert result.output_dir is not None


def test_output_dir_none_when_no_workspace():
    """When no workspace is configured, output_dir is None."""
    def runner(req):
        return NodeRunResponse(output="x")

    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    result = WorkflowEngine(runner).run(wf, "t")   # no workspace=
    assert result.output_dir is None


# Input validation + declared-file entry contract

def test_missing_input_file_aborts_naming_the_path(tmp_path):
    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    result = WorkflowEngine(lambda req: NodeRunResponse(output="x"),
                            workspace=str(tmp_path)).run(
        wf, "t", input_files=[str(tmp_path / "nope.txt")])
    assert result.status == "aborted"
    assert "nope.txt" in result.final_output
    assert result.runs == []                     # nothing ran


def test_colliding_input_basenames_abort(tmp_path):
    (tmp_path / "d1").mkdir(); (tmp_path / "d2").mkdir()
    (tmp_path / "d1" / "r.txt").write_text("1")
    (tmp_path / "d2" / "r.txt").write_text("2")
    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    result = WorkflowEngine(lambda req: NodeRunResponse(output="x"),
                            workspace=str(tmp_path)).run(
        wf, "t", input_files=[str(tmp_path / "d1" / "r.txt"), str(tmp_path / "d2" / "r.txt")])
    assert result.status == "aborted"
    assert "r.txt" in result.final_output


def test_declared_file_input_with_no_files_ends_needs_input(tmp_path):
    wf = parse_workflow({
        "name": "w", "start": "a",
        "input": {"file": True, "description": "the report to summarize"},
        "nodes": [{"id": "a", "kind": "work", "tools": "default", "next": None}],
    })
    calls = []
    result = WorkflowEngine(lambda req: calls.append(req) or NodeRunResponse(output="x"),
                            workspace=str(tmp_path)).run(wf, "t")
    assert result.status == "needs_input"
    assert calls == []                            # zero LLM cost
    assert "file" in result.final_output.lower()
    assert "the report to summarize" in result.final_output
