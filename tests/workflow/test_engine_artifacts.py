from pathlib import Path
from durin.workflow.engine import WorkflowEngine, NodeRunResponse
from durin.workflow.spec import parse_workflow


def _wf(nodes, start):
    return parse_workflow({"name": "w", "start": start, "nodes": nodes})


def test_each_agent_node_gets_a_keyed_folder_and_the_consumer_sees_the_producer(tmp_path):
    seen = {}
    def runner(req):
        seen[req.node.id] = (req.output_dir, req.upstream_artifact_dir)
        return NodeRunResponse(output=f"out-{req.node.id}")
    wf = _wf([
        {"id": "a", "kind": "work", "tools": "default", "next": "b"},
        {"id": "b", "kind": "work", "tools": "default", "next": None},
    ], "a")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    a_out, _ = seen["a"]
    b_out, b_prev = seen["b"]
    assert Path(a_out) == tmp_path / ".workflow" / Path(a_out).parts[-3] / "a" / "1"
    assert b_prev == a_out                      # b reads a's folder
    assert b_out != a_out


def test_loop_back_threads_the_right_iteration_folder(tmp_path):
    calls = []
    def runner(req):
        calls.append((req.node.id, req.output_dir, req.upstream_artifact_dir))
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
    make2 = next(c for c in calls if c[0] == "make" and c[1].endswith("/make/2"))
    done = next(c for c in calls if c[0] == "done")
    assert done[2] == make2[1]                  # 'done' (after the passing gate) reads make#2's folder, not the gate's


def test_inert_without_workspace(tmp_path):
    seen = {}
    def runner(req):
        seen["a"] = req.output_dir
        return NodeRunResponse(output="x")
    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    WorkflowEngine(runner).run(wf, "t")         # no workspace=
    assert seen["a"] is None                    # feature inert, no folder


def test_no_tools_node_gets_no_folder_and_nils_the_chain(tmp_path):
    # A no-tools agent node produces no files: it gets no folder, and (like a command
    # node) it nils the threaded artifact dir for the next node.
    seen = {}
    def runner(req):
        seen[req.node.id] = (req.output_dir, req.upstream_artifact_dir)
        return NodeRunResponse(output=f"out-{req.node.id}")
    wf = _wf([
        {"id": "a", "kind": "work", "tools": "default", "next": "mid"},
        {"id": "mid", "kind": "work", "next": "b"},          # tools defaults to "none"
        {"id": "b", "kind": "work", "tools": "default", "next": None},
    ], "a")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    assert seen["mid"][0] is None                # no-tools node: no folder
    assert seen["b"][1] is None                  # the no-tools node nilled the chain
