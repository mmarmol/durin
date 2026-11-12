"""Tests for the sequential flow-graph engine (graph logic, mocked node runner)."""

from pathlib import Path

import pytest

from durin.workflow.engine import (
    _SHARED_CONTEXT_MAX_MESSAGES,
    NodeRunRequest,
    NodeRunResponse,
    WorkflowEngine,
)
from durin.workflow.spec import parse_workflow


def _engine(node_outputs):
    """Engine with a scripted node runner.

    node_outputs: dict node_id -> output string.
    """
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req)
        return NodeRunResponse(
            output=node_outputs[req.node.id],
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[{"role": "assistant", "content": node_outputs[req.node.id]}],
        )

    eng = WorkflowEngine(
        node_runner=node_runner,
        run_id_factory=lambda: "r1",
    )
    return eng, calls


def test_linear_two_nodes_complete():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"})
    res = eng.run(wf, "do it")
    assert res.status == "completed"
    assert res.final_output == "out-b"
    assert [r.node_id for r in res.runs] == ["a", "b"]
    assert res.runs[0].session_key == "workflow:r1:a:1"


def test_output_passes_downstream():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"})
    eng.run(wf, "do it")
    # b received a's output as upstream_output
    b_call = [c for c in calls if c.node.id == "b"][0]
    assert b_call.upstream_output == "out-a"


def test_io_descriptions_frame_the_task():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "input": {"text": True, "description": "a CSV of sales"},
        "output": {"text": True, "description": "a markdown report"},
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    })
    eng, calls = _engine({"a": "out-a"})
    eng.run(wf, "do it")
    task = calls[0].task
    assert "a CSV of sales" in task
    assert "a markdown report" in task
    assert "do it" in task


def test_task_unchanged_without_io_descriptions():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "input": {"text": True},  # declared, but no description
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    })
    eng, calls = _engine({"a": "out-a"})
    eng.run(wf, "do it")
    assert calls[0].task == "do it"


def test_file_input_workflow_with_files_keeps_task_unchanged(tmp_path):
    """A file-input workflow that receives input files should not frame the task.
    The node receives the task unchanged when no I/O descriptions are present."""
    src = tmp_path / "in.txt"
    src.write_text("data")
    wf = parse_workflow({
        "name": "w", "start": "a",
        "input": {"file": True},
        "nodes": [{"id": "a", "kind": "work", "tools": "default", "next": None}],
    })
    calls = []
    def runner(req):
        calls.append(req)
        return NodeRunResponse(output="x")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "the task", input_files=[str(src)])
    assert calls and calls[0].task == "the task"


def test_the_seeds_declare_only_io_descriptors_durin_reads():
    """An I/O descriptor key nothing reads is a promise the workflow does not
    keep: debug and review-changes declared "files", which neither the engine
    (it reads "file", as required input) nor the launch form sees."""
    import json
    from pathlib import Path

    import durin

    known = {"text", "file", "description", "artifacts"}
    seeds = Path(durin.__file__).parent / "templates" / "workflows"
    for path in sorted(seeds.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        for side in ("input", "output"):
            unknown = set(spec.get(side) or {}) - known
            assert not unknown, f"{path.name} {side}: {sorted(unknown)}"


def test_per_node_max_visits_overrides_workflow_default():
    # workflow default max_visits=5, but node 'a' caps itself at 2.
    wf = parse_workflow({
        "name": "d", "start": "a", "max_visits": 5,
        "nodes": [
            {"id": "a", "kind": "work", "max_visits": 2, "on_pass": None, "on_fail": "a"},
        ],
    })
    eng, calls = _engine({"a": "FAIL keep going"})  # always FAIL → loops back to itself
    res = eng.run(wf, "t")
    assert res.status == "exhausted"
    assert res.exhausted_node == "a"
    assert len([c for c in calls if c.node.id == "a"]) == 2  # ran exactly its 2-visit budget


def test_global_ceiling_clamps_a_higher_per_node_value():
    wf = parse_workflow({
        "name": "d", "start": "a", "max_visits": 50,
        "nodes": [{"id": "a", "kind": "work", "max_visits": 50, "on_pass": None, "on_fail": "a"}],
    })
    eng, calls = _engine({"a": "FAIL"})
    eng2 = WorkflowEngine(node_runner=eng._node_runner, run_id_factory=lambda: "r1",
                          max_node_visits=3)
    res = eng2.run(wf, "t")
    assert res.status == "exhausted"
    assert len([c for c in calls if c.node.id == "a"]) == 3  # clamped to the ceiling of 3


def test_shared_vs_own_context():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "own", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"})
    eng.run(wf, "t")
    a_call = [c for c in calls if c.node.id == "a"][0]
    b_call = [c for c in calls if c.node.id == "b"][0]
    # a (shared) starts with empty shared context; b (own) also sees no shared
    # context but a's message was appended to the shared buffer after a ran, so
    # b — being 'own' — must NOT receive it.
    assert a_call.shared_context == []
    assert b_call.shared_context == []   # own node: isolated from the shared buffer


def test_shared_buffer_accumulates_across_shared_nodes():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "shared", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"})
    eng.run(wf, "t")
    b_call = [c for c in calls if c.node.id == "b"][0]
    # b is the second shared node: it must receive a's appended message
    assert b_call.shared_context == [{"role": "assistant", "content": "out-a"}]


def test_shared_buffer_does_not_duplicate_across_three_shared_nodes():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "shared", "next": "c"},
            {"id": "c", "kind": "work", "context": "shared", "next": None},
        ],
    })
    seen = {}

    def runner(req):
        seen[req.node.id] = list(req.shared_context)
        # Faithful to the FIXED contract: only this node's own contribution.
        return NodeRunResponse(output=f"out-{req.node.id}", messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": f"out-{req.node.id}"},
        ])

    WorkflowEngine(runner).run(wf, "t")
    c_ctx = seen["c"]
    assert len(c_ctx) == 4                                     # a's 2 + b's 2, no dupes
    assert [m["role"] for m in c_ctx].count("system") == 0
    assert [m["content"] for m in c_ctx].count("out-a") == 1   # each turn appears once


def test_shared_buffer_is_capped_dropping_oldest(tmp_path):
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "shared", "next": None},
        ],
    })
    overflow = _SHARED_CONTEXT_MAX_MESSAGES + 50
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req)
        if req.node.id == "a":
            # Emit more messages than the cap so the buffer must be trimmed.
            msgs = [{"role": "assistant", "content": f"m{i}"} for i in range(overflow)]
        else:
            msgs = []
        return NodeRunResponse(output="o", session_key=None, messages=msgs)

    WorkflowEngine(node_runner, workspace=str(tmp_path)).run(wf, "t")
    b_call = [c for c in calls if c.node.id == "b"][0]
    # b sees only the most recent N of a's messages; the oldest are dropped.
    assert len(b_call.shared_context) == _SHARED_CONTEXT_MAX_MESSAGES
    assert b_call.shared_context[0] == {"role": "assistant", "content": "m50"}
    assert b_call.shared_context[-1] == {"role": "assistant", "content": f"m{overflow - 1}"}


def test_agent_routing_node_routes_on_its_own_verdict():
    """A routing work node (with on_pass/on_fail) runs as an agent and routes on its
    own PASS/FAIL output — no external judge runner needed."""
    seq = []

    def runner(req):
        seq.append(req.node.id)
        if req.node.id == "gate":
            # fail on first visit, pass on second
            output = "PASS\nok" if seq.count("gate") > 1 else "FAIL\nfix it"
            return NodeRunResponse(output=output)
        return NodeRunResponse(output={"prod": "draft v1", "done": "done-out"}[req.node.id])

    wf = parse_workflow({"name": "w", "start": "prod", "nodes": [
        {"id": "prod", "kind": "work", "next": "gate"},
        {"id": "gate", "kind": "work", "prompt": "good?", "on_pass": "done", "on_fail": "prod"},
        {"id": "done", "kind": "work", "next": None},
    ]})
    res = WorkflowEngine(runner).run(wf, "task")
    assert res.status == "completed"
    assert seq == ["prod", "gate", "prod", "gate", "done"]   # looped once on FAIL


def test_agent_routing_node_fail_threads_feedback():
    """On FAIL, the routing node's output is threaded into the upstream for the next run."""
    seen_inputs = []

    def runner(req):
        if req.node.id == "prod":
            seen_inputs.append(req.upstream_output)
            return NodeRunResponse(output="attempt")
        # gate: fail first, then pass
        output = "FAIL\nneed more detail" if seen_inputs and seen_inputs[-1] is None else "PASS"
        return NodeRunResponse(output=output)

    wf = parse_workflow({"name": "w", "start": "prod", "max_visits": 3, "nodes": [
        {"id": "prod", "kind": "work", "next": "gate"},
        {"id": "gate", "kind": "work", "prompt": "eval", "on_pass": None, "on_fail": "prod"},
    ]})
    WorkflowEngine(runner).run(wf, "t")
    # second run of 'prod' must have received reviewer feedback in its upstream_output
    assert any(inp and "Reviewer feedback (address this):\nFAIL\nneed more detail" in inp
               for inp in seen_inputs)



def test_subworkflow_node_runs_and_threads_output():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "after"},
        {"id": "after", "kind": "work", "next": None},
    ]})
    calls = []

    def subworkflow_runner(name, task, root_session_key=None, work_dir=None, parent_run_id=None, **_kwargs):
        calls.append((name, task, root_session_key, work_dir, parent_run_id))
        return "child-output"

    seen = []

    def node_runner(req):
        seen.append(req.upstream_output)
        return NodeRunResponse(output="after-out", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                         subworkflow_runner=subworkflow_runner)
    res = eng.run(wf, "the task", root_session_key="conv:1")
    assert res.status == "completed"
    # the sub-workflow is invoked with the run's root session key, so its nested
    # node sessions anchor to the invoking conversation (no orphan subtrees); the
    # engine's OWN run_id is passed as the child's parent_run_id.
    assert calls == [("child", "the task", "conv:1", None, "r1")]
    # the work node after the subworkflow saw the child's output as upstream
    assert "child-output" in seen


def test_subworkflow_node_without_runner_raises():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": None},
    ]})

    def node_runner(req):
        return NodeRunResponse(output="x", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    with pytest.raises(RuntimeError, match="subworkflow"):
        eng.run(wf, "t")


def test_subworkflow_receives_the_parent_work_dir(tmp_path):
    calls = {}
    def sub_runner(name, task, root_key, work_dir=None, parent_run_id=None, **_kwargs):
        calls["work_dir"] = work_dir
        return "sub-out"
    wf = parse_workflow({
        "name": "w", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "tools": "default", "next": "sub"},
            {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": None},
        ],
    })
    seen = {}
    def runner(req):
        seen["a"] = req.output_dir
        return NodeRunResponse(output="x")
    WorkflowEngine(runner, subworkflow_runner=sub_runner, workspace=str(tmp_path)).run(wf, "t")
    assert calls["work_dir"] == seen["a"]          # the parent's shared folder, verbatim


def test_work_dir_override_replaces_the_run_folder(tmp_path):
    override = tmp_path / "parent-work"
    override.mkdir()
    seen = {}
    def runner(req):
        seen["a"] = req.output_dir
        return NodeRunResponse(output="x")
    wf = parse_workflow({"name": "w", "start": "a",
                         "nodes": [{"id": "a", "kind": "work", "tools": "default", "next": None}]})
    result = WorkflowEngine(runner, workspace=str(tmp_path)).run(
        wf, "t", work_dir_override=str(override))
    assert seen["a"] == str(override)
    assert result.output_dir == str(override)
    assert not (tmp_path / ".workflow").exists()   # no own run folder created


def test_parallel_runs_all_branches_and_merges():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b", "c"], "next": "join"},
        {"id": "a", "kind": "work"},
        {"id": "b", "kind": "work"},
        {"id": "c", "kind": "work"},
        {"id": "join", "kind": "work", "next": None},
    ]})
    outputs = {"a": "out-A", "b": "out-B", "c": "out-C", "join": "joined"}
    seen_inputs = []

    def node_runner(req):
        if req.node.id == "join":
            seen_inputs.append(req.upstream_output)
        return NodeRunResponse(output=outputs[req.node.id], session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    res = eng.run(wf, "the task")
    assert res.status == "completed"
    # the join node received the merged output of all three branches
    merged = seen_inputs[0]
    assert "out-A" in merged and "out-B" in merged and "out-C" in merged
    # the parallel node recorded a run whose output is the merge
    fan_run = [r for r in res.runs if r.node_id == "fan"][0]
    assert "out-A" in fan_run.output and "out-B" in fan_run.output and "out-C" in fan_run.output


def test_parallel_branches_get_the_parallel_input():
    from durin.workflow.spec import parse_workflow as _pw
    wf = _pw({"name": "d", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["a"], "next": None},
        {"id": "a", "kind": "work"},
    ]})
    seen = {}

    def node_runner(req):
        seen[req.node.id] = req.upstream_output
        return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    eng.run(wf, "t")
    # branch 'a' saw 'pre's output (the input flowing into the parallel node)
    assert seen["a"] == "pre-out"


def _writing_wf(reconcile, **extra):
    nodes = [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"],
         "reconcile": reconcile, "next": None, **extra},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "work", "tools": "default"},
    ]
    return parse_workflow({"name": "d", "start": "fan", "nodes": nodes})


def test_parallel_choose_applies_only_the_winner(tmp_path):
    # each branch writes the same artifact in its own private copy of the run's
    # shared working folder; the winner's write lands in the REAL working folder
    def node_runner(req):
        Path(req.workspace_override, "result.txt").write_text(f"from {req.node.id}")
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])

    eng = WorkflowEngine(
        node_runner=node_runner, run_id_factory=lambda: "r1",
        workspace=str(tmp_path), pick_runner=lambda criteria, options, model: 1,  # pick b
    )
    res = eng.run(_writing_wf("choose", criteria="best"), "t")
    assert res.status == "completed"
    work = tmp_path / ".workflow" / "r1" / "work"
    assert (work / "result.txt").read_text() == "from b"   # only the winner applied
    assert not (tmp_path / "result.txt").exists()          # never at the workspace root
    assert "chosen: b" in res.final_output


def test_parallel_union_applies_disjoint_branches(tmp_path):
    def node_runner(req):
        fname = "x.txt" if req.node.id == "a" else "y.txt"
        Path(req.workspace_override, fname).write_text(req.node.id)
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(_writing_wf("union"), "t")
    assert res.status == "completed"
    work = tmp_path / ".workflow" / "r1" / "work"
    assert (work / "x.txt").read_text() == "a"
    assert (work / "y.txt").read_text() == "b"


def test_parallel_union_conflict_aborts_and_applies_nothing(tmp_path):
    def node_runner(req):
        Path(req.workspace_override, "same.txt").write_text(req.node.id)   # both touch same path
        return NodeRunResponse(output="o", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(_writing_wf("union"), "t")
    assert res.status == "aborted"
    assert "conflict" in res.final_output and "same.txt" in res.final_output
    assert not (tmp_path / ".workflow" / "r1" / "work" / "same.txt").exists()
    assert not (tmp_path / "same.txt").exists()   # nothing applied when there is a conflict


def test_parallel_fork_copies_the_working_folder_not_the_workspace(tmp_path):
    """The fork a writing branch runs in is a copy of the run's shared working
    folder ONLY. Forking the whole durin workspace copied sessions/, memory/ and
    every other state dir per branch — ruinous on a real workspace and never
    branch output (live finding, mxHero box 2026-07-22)."""
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "big.jsonl").write_text("x" * 1000)
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "graph.json").write_text("{}")

    seen: dict[str, bool] = {}

    def node_runner(req):
        fork_root = Path(req.workspace_override)
        seen[req.node.id] = (fork_root / "sessions").exists() or (fork_root / "memory").exists()
        Path(fork_root, f"{req.node.id}.txt").write_text(req.node.id)
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(_writing_wf("union"), "t")
    assert res.status == "completed"
    assert seen and not any(seen.values()), f"state dirs leaked into branch forks: {seen}"


# ── where a writing branch's private copy lives and when it goes ──

def _copies_wf(reconcile, **extra):
    """pre -> fan(a, b): `pre` seeds the working folder before the parallel step."""
    return parse_workflow({"name": "d", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "tools": "default", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"], "reconcile": reconcile,
         "next": None, **extra},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "work", "tools": "default"},
    ]})


def _branches(tmp_path, run="r1"):
    return tmp_path / ".workflow" / run / "branches" / run


def _copy_engine(tmp_path, runner):
    return WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1",
                          workspace=str(tmp_path), pick_runner=lambda c, o, m: 0)


def test_a_branch_copy_lives_under_the_runs_folder_and_not_in_the_system_temp(tmp_path, monkeypatch):
    import tempfile
    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(sys_tmp))
    monkeypatch.setattr(tempfile, "tempdir", None)
    seen: dict[str, Path] = {}

    def node_runner(req):
        if req.workspace_override:
            copy = Path(req.workspace_override)
            seen[req.node.id] = copy
            assert copy.parent == tmp_path / ".workflow" / "r1" / "branches" / "r1"
            assert copy.name.startswith("b-")
            (copy / f"{req.node.id}.txt").write_text(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "completed" and set(seen) == {"a", "b"}
    assert list(sys_tmp.iterdir()) == []
    assert not _branches(tmp_path).exists()


def test_copies_are_removed_when_a_branch_fails(tmp_path):
    def node_runner(req):
        if req.node.id == "b":
            raise RuntimeError("boom")
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "aborted"
    assert not _branches(tmp_path).exists()


def test_copies_are_removed_on_a_union_conflict(tmp_path):
    def node_runner(req):
        if req.workspace_override:
            Path(req.workspace_override, "same.txt").write_text(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "aborted" and "conflict" in res.final_output
    assert not _branches(tmp_path).exists()


def test_copies_are_removed_when_the_step_is_cancelled(tmp_path):
    from durin.workflow.engine import NodeExecutionError, WorkInterruptedError

    def node_runner(req):
        if req.node.id == "b":
            raise NodeExecutionError("b", 1, None, WorkInterruptedError("stopped"))
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "cancelled"
    assert not _branches(tmp_path).exists()


def test_copies_are_removed_after_a_choose_too(tmp_path):
    def node_runner(req):
        if req.workspace_override:
            Path(req.workspace_override, "x.txt").write_text(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("choose", criteria="any"), "t")
    assert res.status == "completed"
    assert not _branches(tmp_path).exists()


def test_a_copy_whose_creation_failed_midway_is_removed(tmp_path, monkeypatch):
    import shutil
    import threading

    from durin.workflow import workspace_fork
    real = shutil.copy2
    lock, calls = threading.Lock(), [0]

    def flaky(src, dst, *a, **kw):
        with lock:
            calls[0] += 1
            n = calls[0]
        if n == 2:
            raise OSError("disk full")
        return real(src, dst, *a, **kw)

    def node_runner(req):
        if req.node.id == "pre":
            (Path(req.output_dir) / "one.txt").write_text("1")
            (Path(req.output_dir) / "two.txt").write_text("2")
        return NodeRunResponse(output="o", session_key=None, messages=[])

    monkeypatch.setattr(workspace_fork.shutil, "copy2", flaky)
    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "aborted" and calls[0] >= 2
    assert not _branches(tmp_path).exists()


def test_entering_the_step_removes_what_an_earlier_attempt_left(tmp_path):
    planted = tmp_path / ".workflow" / "r1" / "branches" / "old-run" / "left-zzz" / "junk"
    retired = tmp_path / ".workflow" / "r1" / "branches" / ".dead-old-run-q1" / "junk"
    seen: list[tuple[bool, bool]] = []

    def node_runner(req):
        if req.node.id == "pre":
            for p in (planted, retired):
                p.parent.mkdir(parents=True)
                p.write_text("junk")
        elif req.workspace_override:
            seen.append((planted.parent.exists(), retired.parent.exists()))
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "completed"
    assert seen == [(False, False), (False, False)]


def test_a_branchs_file_tools_work_inside_its_copy(tmp_path):
    """The real tool set a branch gets, run on a copy whose path contains `.workflow`."""
    import asyncio

    from durin.workflow import workspace_fork

    work = tmp_path / "ws" / ".workflow" / "r1" / "work"
    work.mkdir(parents=True)
    (work / "seed.txt").write_text("seeded")
    copy = workspace_fork.fork(work, into=workspace_fork.branches_root(work) / "r1")
    assert ".workflow" in copy.parts

    registry = _branch_tools(tmp_path, copy)

    async def use():
        wrote = await registry.execute("write_file", {"path": "made.txt", "content": "needle-42"})
        read = await registry.execute("read_file", {"path": "made.txt"})
        grep = await registry.execute("grep", {"pattern": "needle-42", "path": "."})
        listing = await registry.execute("list_dir", {"path": "."})
        return wrote, read, grep, listing

    wrote, read, grep, listing = asyncio.run(use())
    assert (copy / "made.txt").read_text() == "needle-42", wrote
    assert "needle-42" in str(read)
    assert "made.txt" in str(grep)
    assert "made.txt" in str(listing) and "seed.txt" in str(listing)


@pytest.mark.parametrize("where", ["none", "outside"])
def test_a_writing_parallel_step_without_a_run_folder_aborts(tmp_path, where):
    # Where a fallback to the workspace root would act: a `branches` beside the workspace.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sentinels = [tmp_path / "branches" / "mine.txt"]
    work_dir = None
    if where == "outside":
        work_dir = str(tmp_path / "project" / "work")
        sentinels.append(tmp_path / "project" / "branches" / "mine.txt")
    for keep in sentinels:
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text("x")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    ran = []

    def node_runner(req):
        ran.append(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    eng = _copy_engine(workspace, node_runner)
    wf = _writing_wf("union")
    merged, abort = eng._run_parallel(wf, wf.nodes["fan"], "t", "r1", 1, None, "", [],
                                      work_dir=work_dir)
    assert merged == ""
    assert abort == "parallel node 'fan': reconcile='union' needs the run's working folder"
    assert ran == [] and all(k.read_text() == "x" for k in sentinels)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def _branch_tools(tmp_path, copy):
    from unittest.mock import MagicMock

    from durin.agent.runner import AgentRunner  # noqa: F401 - completes the config models
    from durin.session.manager import SessionManager
    from durin.workflow.node_runner import AgentNodeRunner

    runner = AgentNodeRunner(MagicMock(), SessionManager(workspace=tmp_path / "ws"),
                             default_model="test-model")
    node = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "tools": "default"}]}).nodes["a"]
    return runner._build_tools(node, str(copy), str(copy))


def test_a_branch_id_never_reaches_its_copys_path_or_the_shell_deny_rules(tmp_path):
    import asyncio

    wf = parse_workflow({"name": "d", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "tools": "default", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["reboot-check", "shutdown"],
         "reconcile": "union", "next": None},
        {"id": "reboot-check", "kind": "work", "tools": "default"},
        {"id": "shutdown", "kind": "work", "tools": "default"}]})
    paths: dict[str, str] = {}

    def node_runner(req):
        if req.workspace_override:
            paths[req.node.id] = req.workspace_override
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(wf, "t")
    assert res.status == "completed" and set(paths) == {"reboot-check", "shutdown"}
    for bid, path in paths.items():
        assert bid not in path.replace(str(tmp_path), "")

    copy = Path(paths["reboot-check"])
    copy.mkdir(parents=True, exist_ok=True)
    out = asyncio.run(_branch_tools(tmp_path, copy).execute(
        "exec", {"command": f"ls {copy}"}))
    assert "blocked" not in str(out).lower() and "denied" not in str(out).lower(), out


def test_pruning_a_runs_folder_removes_its_copies(tmp_path):
    import os
    import time

    from durin.workflow.artifacts import KEYED_WORK_MAX_AGE_DAYS, prune_runs

    for run in ("old1", "new1"):
        (tmp_path / ".workflow" / run / "work").mkdir(parents=True)
        (tmp_path / ".workflow" / run / "branches" / run / "left-x").mkdir(parents=True)
    past = time.time() - 1000
    for p in (tmp_path / ".workflow" / "old1").rglob("*"):
        os.utime(p, (past, past))
    os.utime(tmp_path / ".workflow" / "old1", (past, past))
    prune_runs(tmp_path, keep=1)
    assert not (tmp_path / ".workflow" / "old1").exists()
    assert (tmp_path / ".workflow" / "new1" / "branches" / "new1" / "left-x").is_dir()

    key_dir = tmp_path / ".workflow" / "keys" / "wf" / "k1"
    (key_dir / "work").mkdir(parents=True)
    (key_dir / "branches" / "r9" / "left-x").mkdir(parents=True)
    stale = time.time() - (KEYED_WORK_MAX_AGE_DAYS + 1) * 86400
    for p in [*key_dir.rglob("*"), key_dir]:
        os.utime(p, (stale, stale))
    prune_runs(tmp_path, keep=5)
    assert not key_dir.exists()


# ── a write-back of a parallel step's result is undone when it was cut ──

def _files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _cut_writes_at(monkeypatch, n):
    """Make the n-th real-file write of a write-back raise."""
    from durin.workflow import workspace_fork
    real, calls = workspace_fork._write_file, [0]

    def write(target, content, *rest):
        calls[0] += 1
        if calls[0] == n:
            raise OSError("disk full")
        return real(target, content, *rest)

    monkeypatch.setattr(workspace_fork, "_write_file", write)
    return calls


def _seeding_runner(seen_before=None):
    """`pre` seeds the folder; each branch edits the seed and adds a file of its own."""
    def node_runner(req):
        if req.node.id == "pre":
            (Path(req.output_dir) / "seed.txt").write_text("s1")
            (Path(req.output_dir) / "keep.txt").write_text("k")
        elif req.workspace_override:
            copy = Path(req.workspace_override)
            if req.node.id == "a":
                (copy / "seed.txt").write_text("s-a")
            (copy / f"{req.node.id}" / "made.txt").parent.mkdir()
            (copy / f"{req.node.id}" / "made.txt").write_text(req.node.id)
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])
    return node_runner


def _cut_write_back_leaves_the_folder_as_it_was(tmp_path, monkeypatch, wf):
    from durin.workflow import workspace_fork
    calls = _cut_writes_at(monkeypatch, 2)
    res = _copy_engine(tmp_path, _seeding_runner()).run(wf, "t")
    assert res.status == "aborted" and calls[0] == 2
    work = tmp_path / ".workflow" / "r1" / "work"
    assert _files(work) == {"keep.txt": b"k", "seed.txt": b"s1"}
    assert not (work / "a").exists() and not (work / "b").exists()
    assert not workspace_fork.pending_dir(work).exists()


def test_a_union_that_fails_while_writing_back_leaves_the_folder_as_it_was(tmp_path, monkeypatch):
    _cut_write_back_leaves_the_folder_as_it_was(tmp_path, monkeypatch, _copies_wf("union"))


def test_a_choose_winner_is_written_back_through_the_journal(tmp_path, monkeypatch):
    _cut_write_back_leaves_the_folder_as_it_was(
        tmp_path, monkeypatch, _copies_wf("choose", criteria="any"))


def test_a_parallel_step_undoes_a_cut_write_back_before_it_snapshots_or_copies(tmp_path,
                                                                               monkeypatch):
    from durin.workflow import workspace_fork
    work = tmp_path / ".workflow" / "r1" / "work"
    copies: dict[str, dict] = {}
    written: list[set[str]] = []

    def node_runner(req):
        if req.node.id == "pre":
            # What a dead attempt of `fan` would have left: new.txt already
            # written, a.txt not yet, and the journal saying how to undo it.
            (work / "a.txt").write_text("a1")
            cs = workspace_fork.ChangeSet(created={"new.txt": b"n"},
                                          modified={"a.txt": b"half"}, deleted=set())
            with monkeypatch.context() as m:
                _cut_writes_at(m, 2)
                with pytest.raises(OSError):
                    workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
            assert (work / "new.txt").exists()
        elif req.workspace_override:
            copies[req.node.id] = _files(Path(req.workspace_override))
            (Path(req.workspace_override) / f"{req.node.id}-made.txt").write_text(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    real_apply = workspace_fork.apply_all

    def recording_apply(changesets, root, **kw):
        written.append({p for cs in changesets for p in cs.paths})
        return real_apply(changesets, root, **kw)

    monkeypatch.setattr(workspace_fork, "apply_all", recording_apply)
    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "completed"
    # The copies were made from the folder as the cut attempt found it ...
    assert copies == {"a": {"a.txt": b"a1"}, "b": {"a.txt": b"a1"}}
    # ... and so was the snapshot: the write-back holds only the branches' own files.
    assert written[-1] == {"a-made.txt", "b-made.txt"}
    assert _files(work) == {"a.txt": b"a1", "a-made.txt": b"a", "b-made.txt": b"b"}


def _cut_keyed_write_back(tmp_path, monkeypatch, name="d", key="k1"):
    """A keyed folder holding a.txt="a1", left mid write-back by a dead run."""
    from durin.workflow import workspace_fork
    from durin.workflow.artifacts import keyed_work_dir
    work = keyed_work_dir(tmp_path, name, key)       # made by the call
    (work / "a.txt").write_text("a1")
    cs = workspace_fork.ChangeSet(created={"new.txt": b"n"}, modified={"a.txt": b"a2"},
                                  deleted=set())
    with monkeypatch.context() as m:
        _cut_writes_at(m, 2)
        with pytest.raises(OSError):
            workspace_fork.apply_all([cs], work, run_id="old", node_id="fan")
    assert (work / "new.txt").exists() or (work / "a.txt").read_text() == "a2"
    assert (workspace_fork.pending_dir(work) / "journal.json").is_file()
    return work


def test_a_run_on_the_same_key_starts_from_the_folder_before_the_dead_write_back(tmp_path,
                                                                                 monkeypatch):
    from durin.workflow import workspace_fork
    work = _cut_keyed_write_back(tmp_path, monkeypatch)
    seen: dict[str, dict] = {}

    def node_runner(req):
        seen[req.node.id] = _files(Path(req.output_dir))
        return NodeRunResponse(output="o", session_key=None, messages=[])

    wf = parse_workflow({"name": "d", "start": "look", "nodes": [
        {"id": "look", "kind": "work", "tools": "default", "next": None}]})
    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r2",
                         workspace=str(tmp_path))
    res = eng.run(wf, "t", work_key="k1")
    assert res.status == "completed"
    assert {k: v for k, v in seen["look"].items() if not k.startswith(".")} == {"a.txt": b"a1"}
    assert not workspace_fork.pending_dir(work).exists()


def _shape_wf_runner(case):
    """pre seeds the folder; the branches change it so the union's result would turn
    a file into a folder (x/y under x) or a folder into a file (d), or two branches
    write p and p/q."""
    import shutil

    def node_runner(req):
        if req.node.id == "pre":
            (Path(req.output_dir) / "x").write_text("the person's x")
            (Path(req.output_dir) / "d").mkdir()
            (Path(req.output_dir) / "d" / "a").write_text("a")
            (Path(req.output_dir) / "same.txt").write_text("s")
        elif req.workspace_override:
            copy = Path(req.workspace_override)
            if case == "file-to-folder":
                (copy / "x").unlink()              # a: deletes x; b: makes x a folder
                if req.node.id == "b":
                    (copy / "x").mkdir()
                    (copy / "x" / "y").write_text("y")
            elif case == "folder-to-file" and req.node.id == "a":
                shutil.rmtree(copy / "d")
                (copy / "d").write_text("now a file")
            elif case == "file-and-file-inside":
                (copy / ("p" if req.node.id == "a" else "p/q")).parent.mkdir(exist_ok=True)
                (copy / ("p" if req.node.id == "a" else "p/q")).write_text(req.node.id)
            elif case == "conflict":
                (copy / "same.txt").write_text(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])
    return node_runner


def _record_shape(tmp_path, res):
    from durin.workflow import run_log
    from durin.workflow.engine import resumable_kind
    rec = run_log.read_manifest(tmp_path, "d", "r1")
    return {"status": res.status, "failed_node": res.failed_node,
            "record_failed_node": rec["failed_node"], "resumable": resumable_kind(rec),
            "rows": sorted((r["node_id"], r["status"], r.get("branch_id")) for r in rec["runs"])}


@pytest.mark.parametrize("case,text", [
    ("file-to-folder", "parallel node 'fan': the branches' result cannot be written back: "
                       "'x/y' would turn a file into a folder or a folder into a file"),
    ("folder-to-file", "parallel node 'fan': the branches' result cannot be written back: "
                       "'d' would turn a file into a folder or a folder into a file"),
    ("file-and-file-inside", "parallel node 'fan': union conflict on ['p', 'p/q']"),
])
def test_a_union_whose_result_changes_a_shape_ends_like_a_union_conflict(tmp_path, case, text):
    from durin.workflow import workspace_fork
    conflict_ws = tmp_path / "conflict"
    conflict = _copy_engine(conflict_ws, _shape_wf_runner("conflict")).run(
        _copies_wf("union"), "t")
    assert conflict.status == "aborted" and "union conflict on ['same.txt']" in conflict.final_output

    ws = tmp_path / "shape"
    work = ws / ".workflow" / "r1" / "work"
    seen = {}

    def runner(req):
        out = _shape_wf_runner(case)(req)
        if req.node.id == "pre":
            seen["before"] = _files(work)
        return out

    res = _copy_engine(ws, runner).run(_copies_wf("union"), "t")
    assert res.final_output == text
    assert _record_shape(ws, res) == _record_shape(conflict_ws, conflict)
    assert _files(work) == seen["before"]                     # nothing applied
    assert not (work / "d").is_file() and (work / "x").is_file()
    assert not (workspace_fork.run_area(work) / "pending-apply").exists()


def test_a_write_back_that_fails_beside_a_detached_steps_file_is_retried(tmp_path, monkeypatch):
    import threading
    import time

    from durin.workflow import run_log, workspace_fork
    from durin.workflow.engine import build_resume_state, resumable_kind

    wf = parse_workflow({"name": "d", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "tools": "default", "next": "det"},
        {"id": "det", "kind": "work", "tools": "default", "detached": True, "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"], "reconcile": "union",
         "next": None},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "work", "tools": "default"}]})
    work = tmp_path / ".workflow" / "r1" / "work"
    det_wrote = threading.Event()

    def node_runner(req):
        if req.node.id == "pre":
            (Path(req.output_dir) / "seed.txt").write_text("s")
        elif req.node.id == "det":
            # A side-effect step writing its report under out/ once the folder exists.
            for _ in range(500):
                if (work / "out").is_dir():
                    break
                time.sleep(0.01)
            (work / "out").mkdir(exist_ok=True)
            (work / "out" / "det-report.txt").write_text("detached step output")
            det_wrote.set()
        elif req.workspace_override:
            (Path(req.workspace_override) / "out").mkdir(exist_ok=True)
            (Path(req.workspace_override) / "out" / f"{req.node.id}.txt").write_text(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    real = workspace_fork._write_file
    full = [True]

    def write(target, content, *rest):
        real(target, content, *rest)
        if full[0]:
            det_wrote.wait(5)
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(workspace_fork, "_write_file", write)
    eng = _copy_engine(tmp_path, node_runner)
    res = eng.run(wf, "t")
    assert res.status == "aborted" and res.failed_node == "fan", res.final_output
    assert "writing the branches' result back failed: [Errno 28] No space left on device" \
        in res.final_output
    assert (work / "out" / "det-report.txt").read_text() == "detached step output"
    assert not workspace_fork.pending_dir(work).exists()      # the undo left out/ and finished
    rec = run_log.read_manifest(tmp_path, "d", "r1")
    assert resumable_kind(rec) == "failed"

    full[0] = False                                             # the disk has room again
    retry = eng.run(wf, "t", resume=build_resume_state(rec, "", workspace=tmp_path))
    assert retry.status == "completed", retry.final_output
    assert _files(work) == {"seed.txt": b"s", "out/a.txt": b"a", "out/b.txt": b"b",
                            "out/det-report.txt": b"detached step output"}


def test_a_read_only_folder_fails_the_write_back_the_same_way_on_every_retry(tmp_path):
    import os

    from durin.workflow import run_log, workspace_fork
    from durin.workflow.engine import build_resume_state, resumable_kind
    if os.geteuid() == 0:
        pytest.skip("permissions do not bind the superuser")
    work = tmp_path / ".workflow" / "r1" / "work"

    def node_runner(req):
        if req.node.id == "pre":
            (work / "ro").mkdir()
            (work / "ro" / "f.txt").write_text("f1")
            (work / "keep.txt").write_text("k")
            os.chmod(work / "ro", 0o555)
        elif req.workspace_override and req.node.id == "a":
            (Path(req.workspace_override) / "ro" / "f.txt").write_text("f2")
            (Path(req.workspace_override) / "keep.txt").unlink()
        return NodeRunResponse(output="o", session_key=None, messages=[])

    eng = _copy_engine(tmp_path, node_runner)
    wf = _copies_wf("union")
    try:
        res = eng.run(wf, "t")
        before = _files(work)
        for attempt in range(2):
            assert res.status == "aborted" and res.failed_node == "fan", res.final_output
            assert "writing the branches' result back failed: [Errno 13] Permission denied" \
                in res.final_output
            assert "could not be undone" not in res.final_output
            assert not workspace_fork.pending_dir(work).exists()
            assert _files(work) == before == {"ro/f.txt": b"f1", "keep.txt": b"k"}
            rec = run_log.read_manifest(tmp_path, "d", "r1")
            assert resumable_kind(rec) == "failed"
            if attempt == 0:
                res = eng.run(wf, "t", resume=build_resume_state(rec, "", workspace=tmp_path))
        os.chmod(work / "ro", 0o755)                 # the cause is gone
        done = eng.run(wf, "t", resume=build_resume_state(rec, "", workspace=tmp_path))
        assert done.status == "completed", done.final_output
        assert _files(work) == {"ro/f.txt": b"f2"}
    finally:
        if (work / "ro").is_dir():
            os.chmod(work / "ro", 0o755)


def test_a_branch_file_under_a_folder_link_of_the_real_folder_is_refused(tmp_path):
    import os
    import stat

    from durin.workflow import workspace_fork
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.txt").write_text("outside x")
    os.chmod(outside / "x.txt", 0o640)
    work = tmp_path / ".workflow" / "r1" / "work"

    def node_runner(req):
        if req.node.id == "pre":
            (work / "dl").symlink_to(outside)
        elif req.workspace_override and req.node.id == "a":
            copy = Path(req.workspace_override)
            assert not os.path.lexists(copy / "dl")      # the copy skips the folder link
            (copy / "dl").mkdir()
            (copy / "dl" / "new.txt").write_text("n")
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "aborted" and res.failed_node is None
    assert res.final_output == ("parallel node 'fan': the branches' result cannot be written "
                                "back: 'dl/new.txt' is reached through a link")
    assert sorted(p.name for p in outside.iterdir()) == ["x.txt"]
    assert (outside / "x.txt").read_text() == "outside x"
    assert stat.S_IMODE(os.stat(outside / "x.txt").st_mode) == 0o640
    assert not workspace_fork.pending_dir(work).exists()


def _linked_work(tmp_path, keyed: bool):
    """The run's working folder made a link to a project folder outside the run area
    holding one file; returns (working folder, project folder)."""
    from durin.workflow.artifacts import keyed_work_dir
    project = tmp_path / "project"
    project.mkdir()
    (project / "existing.txt").write_text("mine")
    if keyed:
        work = keyed_work_dir(tmp_path, "d", "k1")
        work.rmdir()
    else:
        work = tmp_path / ".workflow" / "r1" / "work"
        work.parent.mkdir(parents=True)
    work.symlink_to(project)
    return work, project


def _snapshot(folder: Path) -> dict[str, tuple]:
    import os
    import stat
    out = {}
    for p in sorted(folder.rglob("*")):
        st = os.lstat(p)
        out[p.relative_to(folder).as_posix()] = (
            p.read_bytes() if stat.S_ISREG(st.st_mode) else stat.S_IFMT(st.st_mode),
            stat.S_IMODE(st.st_mode))
    return out


@pytest.mark.parametrize("keyed", [False, True], ids=["unkeyed", "keyed"])
def test_a_linked_working_folder_does_not_stop_a_run_without_a_parallel_step(tmp_path, keyed):
    from durin.workflow import workspace_fork
    work, project = _linked_work(tmp_path, keyed)

    def node_runner(req):
        (Path(req.output_dir) / f"{req.node.id}.txt").write_text(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    wf = parse_workflow({"name": "d", "start": "only", "nodes": [
        {"id": "only", "kind": "work", "tools": "default", "next": None}]})
    res = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                         workspace=str(tmp_path)).run(wf, "t", work_key="k1" if keyed else None)
    assert res.status == "completed", res.final_output
    files = _files(project)                  # the step wrote through the link
    assert files["existing.txt"] == b"mine" and b"only" in files.values()
    assert not (workspace_fork.run_area(work) / "pending-apply").exists()


@pytest.mark.parametrize("why,text", [
    ("link", "the run's working folder is a link"),
    ("volume", "the run's working folder is on another volume than the run's area"),
])
def test_a_write_back_into_a_linked_or_mounted_working_folder_is_refused(tmp_path, monkeypatch,
                                                                         why, text):
    import os

    from durin.workflow import workspace_fork
    if why == "link":
        work, project = _linked_work(tmp_path, keyed=False)
    else:
        work = project = tmp_path / ".workflow" / "r1" / "work"
        real_lstat = os.lstat

        def other_device(path, *a, **kw):
            st = real_lstat(path, *a, **kw)
            if os.fspath(path) == str(work):
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:10]))
            return st

        monkeypatch.setattr(os, "lstat", other_device)
    seen = {}

    def node_runner(req):
        if req.node.id == "pre":
            (Path(req.output_dir) / "seed.txt").write_text("s")
            seen["before"] = _snapshot(project)
        elif req.workspace_override:
            (Path(req.workspace_override) / f"{req.node.id}.txt").write_text(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    assert res.status == "aborted" and res.failed_node is None
    assert res.final_output == f"parallel node 'fan': the branches' result cannot be written back: {text}"
    monkeypatch.undo()
    assert _snapshot(project) == seen["before"]
    assert not os.path.lexists(workspace_fork.run_area(work) / "pending-apply")


def test_a_folder_that_loses_its_search_permission_while_the_branches_run_is_refused(tmp_path):
    import os

    from durin.workflow import workspace_fork
    if os.geteuid() == 0:
        pytest.skip("permissions do not bind the superuser")
    import threading
    work = tmp_path / ".workflow" / "r1" / "work"
    # A branch's runner is called once its copy of the folder exists; the real
    # folder loses its permission only after both runners were entered, so neither
    # copy is made from a folder that can no longer be read. A barrier that times
    # out breaks, and the branch fails loudly instead of hanging.
    both_copied = threading.Barrier(2, timeout=10)

    def node_runner(req):
        if req.node.id == "pre":
            (work / "ns").mkdir()
            (work / "ns" / "f.txt").write_text("f1")
        elif req.workspace_override:
            both_copied.wait()
            if req.node.id == "a":
                (Path(req.workspace_override) / "ns" / "f.txt").write_text("f2")
                os.chmod(work / "ns", 0o600)      # the real folder loses its search permission
        return NodeRunResponse(output="o", session_key=None, messages=[])

    try:
        res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t")
    finally:
        os.chmod(work / "ns", 0o755)
    assert res.status == "aborted" and res.failed_node is None
    assert res.final_output.startswith("parallel node 'fan': the branches' result cannot be "
                                       "written back: 'ns/f.txt' cannot be examined: [Errno 13]")
    assert (work / "ns" / "f.txt").read_text() == "f1"
    assert not workspace_fork.pending_dir(work).exists()


def test_a_journal_beside_a_linked_working_folder_names_the_journal(tmp_path, monkeypatch):
    import shutil

    from durin.workflow import workspace_fork
    work = _cut_keyed_write_back(tmp_path, monkeypatch)
    journal = workspace_fork.pending_dir(work) / "journal.json"
    outside = tmp_path / "outside"
    for rel in ("a.txt", "new.txt"):
        (outside / rel).parent.mkdir(parents=True, exist_ok=True)
        (outside / rel).write_bytes(b"OUTSIDE " + rel.encode())
    shutil.move(str(work), str(tmp_path / "kept-work"))
    work.symlink_to(outside)
    before = _snapshot(outside)
    started = []

    def node_runner(req):
        started.append(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t", work_key="k1")
    assert res.status == "aborted" and started == []
    assert res.final_output == _FRESH_UNUSABLE.format(
        path=journal, reason="the working folder is a link", pre=journal.parent / "pre")
    assert _snapshot(outside) == before and journal.is_file()


def test_a_journal_folder_that_is_a_link_names_the_link_and_where_the_content_is(
        tmp_path, monkeypatch):
    import shutil

    from durin.workflow import workspace_fork
    work = _cut_keyed_write_back(tmp_path, monkeypatch)
    link = workspace_fork.run_area(work) / "pending-apply"
    shutil.move(str(link), str(tmp_path / "kept-pending"))
    link.symlink_to(tmp_path / "kept-pending")
    started = []

    def node_runner(req):
        started.append(req.node.id)
        return NodeRunResponse(output="o", session_key=None, messages=[])

    res = _copy_engine(tmp_path, node_runner).run(_copies_wf("union"), "t", work_key="k1")
    assert res.status == "aborted" and started == []
    # Removing the link would not discard what it leads to: the text does not say so.
    assert res.final_output == (
        "the working folder holds changes of an earlier parallel step that could not be "
        f"undone ({link}): the journal folder is a symlink. The earlier content of the files "
        f"that step changed is kept in {link / 'pre'}, where that link leads. A new run can "
        "start once a person has checked the folder and removed that file.")
    assert link.is_symlink() and (tmp_path / "kept-pending" / "journal.json").is_file()


_KEPT = ("The earlier content of the files that step changed is kept in {pre}; removing "
         "the journal file discards it when a run next starts in that folder.")
_UNUSABLE = ("the working folder holds changes of an earlier parallel step that could not "
             "be undone ({path}): {reason}. " + _KEPT + " It can run once a person has checked "
             "the folder and removed that file.")
_UNDONE_YET = ("the working folder holds changes of an earlier parallel step that could not "
               "be undone yet ({path}): {error}. " + _KEPT + " A retry undoes them once the "
               "cause is gone.")
_FRESH_UNUSABLE = ("the working folder holds changes of an earlier parallel step that could "
                   "not be undone ({path}): {reason}. " + _KEPT + " A new run can start once a "
                   "person has checked the folder and removed that file.")
_FRESH_UNDONE_YET = ("the working folder holds changes of an earlier parallel step that could "
                     "not be undone yet ({path}): {error}. " + _KEPT + " A new run undoes them "
                     "once the cause is gone.")


def _unreadable(text):
    """The reason a journal holding ``text`` cannot be used: it does not parse."""
    import json
    try:
        json.loads(text)
    except ValueError as exc:
        return f"it cannot be read ({exc})"
    raise AssertionError(f"{text!r} parses")


def _fail_undo(monkeypatch):
    """Make every undo of a write-back stop with a full disk."""
    from durin.workflow import workspace_fork

    def full(*args):
        raise OSError("disk full")

    monkeypatch.setattr(workspace_fork, "_put_back", full)


@pytest.mark.parametrize("where,kind", [
    ("fresh-start", "unusable"), ("fresh-start", "undo-fails"),
    ("resumed-start", "unusable"), ("resumed-start", "undo-fails"),
    ("step-entry", "unusable"), ("step-entry", "undo-fails"),
    ("after-write-back", "undo-fails"),
])
def test_a_journal_that_cannot_be_used_or_undone_aborts_the_run_with_the_file_named(
        tmp_path, monkeypatch, where, kind):
    from durin.workflow import run_log, workspace_fork
    from durin.workflow.artifacts import keyed_work_dir
    from durin.workflow.engine import build_resume_state, resumable_kind, resume_refusal

    wf = _copies_wf("union")
    keyed = where in ("fresh-start", "resumed-start")
    work = (keyed_work_dir(tmp_path, "d", "k1") if keyed
            else tmp_path / ".workflow" / "r1" / "work")
    journal = workspace_fork.run_area(work) / "pending-apply" / "journal.json"
    started: list[tuple[str, str | None]] = []
    branch_fails = [where == "resumed-start"]

    def plant():
        (work / "a.txt").write_text("a1")
        if kind == "unusable":
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text("{")
            (work / "a.txt").write_text("half")
            return
        # new.txt and a.txt written, the third write cut: the undo has a.txt to put back.
        cs = workspace_fork.ChangeSet(created={"new.txt": b"n"},
                                      modified={"a.txt": b"half", "late.txt": b"l"},
                                      deleted=set())
        with monkeypatch.context() as m:
            _cut_writes_at(m, 3)
            with pytest.raises(OSError):
                workspace_fork.apply_all([cs], work, run_id="old", node_id="fan")

    def node_runner(req):
        started.append((req.node.id, req.upstream_output))
        if req.node.id == "pre":
            (work / "a.txt").write_text("a1")
            if where == "step-entry":
                plant()                       # what a dead attempt of `fan` would have left
        elif req.workspace_override:
            if req.node.id == "b" and branch_fails[0]:
                from durin.workflow.engine import NodeExecutionError
                raise NodeExecutionError("b", req.iteration, None, RuntimeError("flaky"))
            (Path(req.workspace_override) / f"{req.node.id}.txt").write_text(req.node.id)
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])

    ids = iter(["r1", "r2"])
    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: next(ids),
                         workspace=str(tmp_path), pick_runner=lambda c, o, m: 0)
    if where == "resumed-start":
        first = eng.run(wf, "t", work_key="k1")
        assert first.status == "aborted" and first.failed_node == "fan"
        branch_fails[0] = False
    if where in ("fresh-start", "resumed-start"):
        plant()
    with monkeypatch.context() as m:
        if kind == "undo-fails":
            _fail_undo(m)
        if where == "after-write-back":
            _cut_writes_at(m, 2)
        del started[:]
        if where == "resumed-start":
            rec = run_log.read_manifest(tmp_path, "d", "r1")
            res = eng.run(wf, "t", resume=build_resume_state(rec, "", workspace=tmp_path))
        else:
            res = eng.run(wf, "t", work_key="k1" if keyed else None)

    fresh = where == "fresh-start"
    error = "'a.txt': disk full"                # the path the undo could not put back
    pre = journal.parent / "pre"
    text = ((_FRESH_UNUSABLE if fresh else _UNUSABLE).format(
                path=journal, reason=_unreadable("{"), pre=pre) if kind == "unusable"
            else (_FRESH_UNDONE_YET if fresh else _UNDONE_YET).format(
                path=journal, error=error, pre=pre))
    if where == "after-write-back":
        text = f"writing the branches' result back failed: disk full; {text}"
    assert res.status == "aborted"
    assert journal.exists()                     # left for the person, or for a retry
    if kind == "undo-fails":
        # The folder the text names holds what a.txt held before the write-back.
        assert b"a1" in [f.read_bytes() for f in pre.iterdir()]
    rec = run_log.read_manifest(tmp_path, "d", "r1")
    if where == "fresh-start":
        # A fresh run that cannot start is not retried: a new run is started.
        assert res.final_output == text and res.failed_node is None
        assert rec["failed_node"] is None and resumable_kind(rec) is None
        assert resume_refusal(rec, workspace=tmp_path, workflow=wf) is None   # not resumable:
        assert started == []                                                   # nothing to refuse
    else:
        assert text in res.final_output and res.failed_node == "fan"
        assert rec["status"] == "aborted" and rec["failed_node"] == "fan"
        assert resumable_kind(rec) == "failed"
        assert resume_refusal(rec, workspace=tmp_path, workflow=wf) is None
        # The branches run concurrently: their start order is not fixed.
        assert sorted(started) == sorted(
            [] if where == "resumed-start"
            else [("pre", None)] + ([("a", "out-pre"), ("b", "out-pre")]
                                    if where == "after-write-back" else []))

    # The cause goes: the person removes the unusable file, or the disk has room.
    if kind == "unusable":
        journal.unlink()
    del started[:]
    if where == "fresh-start":
        again = eng.run(wf, "t", work_key="k1")
    else:
        again = eng.run(wf, "t", resume=build_resume_state(rec, "", workspace=tmp_path))
    assert again.status == "completed", again.final_output
    assert not journal.exists()
    assert not (work / "new.txt").exists() or where == "fresh-start" and kind == "unusable"
    if where in ("step-entry", "after-write-back", "resumed-start"):
        assert sorted(s for s, _ in started) == ["a", "b"]       # the retry enters `fan`


def test_node_exception_aborts_with_partial_trace():
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": None},
    ]})

    def node_runner(req):
        if req.node.id == "b":
            raise RuntimeError("provider exploded")
        return NodeRunResponse(output="out-a", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    res = eng.run(wf, "t")
    assert res.status == "aborted"                  # does not propagate the raise
    assert "provider exploded" in res.final_output
    assert [r.node_id for r in res.runs] == ["a"]   # partial trace: 'a' ran before 'b' failed


def test_parallel_choose_without_pick_runner_aborts(tmp_path):
    def node_runner(req):
        return NodeRunResponse(output="o", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    res = eng.run(_writing_wf("choose", criteria="best"), "t")
    assert res.status == "aborted"


def test_parallel_respects_max_concurrency(tmp_path):
    import threading
    import time

    live = []
    lock = threading.Lock()
    peak = [0]

    def runner(req):
        with lock:
            live.append(req.node.id)
            peak[0] = max(peak[0], len(live))
        time.sleep(0.05)
        with lock:
            live.remove(req.node.id)
        return NodeRunResponse(output="x")

    nodes = [{"id": "f", "kind": "parallel", "branches": ["a", "b", "c", "d"],
              "max_concurrency": 2, "next": None}]
    nodes += [{"id": n, "kind": "work"} for n in "abcd"]
    wf = parse_workflow({"name": "w", "start": "f", "nodes": nodes})
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    assert peak[0] <= 2


# ── Multi-way routing (cases) engine tests ────────────────────────────────────


def _cases_engine(node_outputs: dict) -> tuple:
    """Engine with a scripted agent runner for cases tests."""
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req)
        return NodeRunResponse(
            output=node_outputs[req.node.id],
            session_key=f"workflow:r1:{req.node.id}:{req.iteration}",
            messages=[{"role": "assistant", "content": node_outputs[req.node.id]}],
        )

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1")
    return eng, calls


def _3way_wf():
    """A 3-outcome workflow: verify routes GROUNDED→end, MISSING→plan, MISUSED→synthesize."""
    return parse_workflow({
        "name": "w", "start": "verify",
        "nodes": [
            {"id": "verify", "kind": "work",
             "cases": {"GROUNDED": None, "MISSING": "plan", "MISUSED": "synthesize"}},
            {"id": "plan", "kind": "work", "next": None},
            {"id": "synthesize", "kind": "work", "next": None},
        ],
    })


def test_cases_routes_to_grounded_end():
    """A matched label with a null target ends the run as completed."""
    wf = _3way_wf()
    eng, calls = _cases_engine({"verify": "analysis done\nGROUNDED"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["verify"]


def test_cases_routes_to_missing_target():
    """A matched label with a node target routes to that node."""
    wf = _3way_wf()
    eng, calls = _cases_engine({"verify": "MISSING", "plan": "plan output"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["verify", "plan"]
    assert res.final_output == "plan output"


def test_cases_routes_to_misused_target():
    """A third label routes to its distinct target."""
    wf = _3way_wf()
    eng, calls = _cases_engine({"verify": "MISUSED", "synthesize": "synth output"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["verify", "synthesize"]
    assert res.final_output == "synth output"


def test_cases_default_catches_unmatched_verdict():
    """When the label doesn't match but 'default' is in cases, route to it."""
    wf = parse_workflow({
        "name": "w", "start": "classify",
        "nodes": [
            {"id": "classify", "kind": "work",
             "cases": {"DONE": None, "default": "fallback"}},
            {"id": "fallback", "kind": "work", "next": None},
        ],
    })
    eng, calls = _cases_engine({"classify": "something unrecognized", "fallback": "handled"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["classify", "fallback"]


def test_cases_no_match_no_default_aborts():
    """Unmatched verdict with no default case aborts the run with a clear message."""
    wf = _3way_wf()
    eng, _ = _cases_engine({"verify": "I cannot decide"})
    res = eng.run(wf, "t")
    assert res.status == "aborted"
    assert "verify" in res.final_output
    # All expected labels should be mentioned
    for label in ["GROUNDED", "MISSING", "MISUSED"]:
        assert label in res.final_output


def test_cases_route_label_recorded_in_trace():
    """The matched case label is recorded in the NodeRun trace."""
    wf = _3way_wf()
    eng, _ = _cases_engine({"verify": "MISSING", "plan": "plan done"})
    res = eng.run(wf, "t")
    verify_run = next(r for r in res.runs if r.node_id == "verify")
    assert verify_run.route_label == "MISSING"
    assert verify_run.passed is None  # multi-way: pass/fail does not apply


def test_cases_feedback_threaded_on_loopback():
    """When a cases node routes to a non-terminal (non-null) target, its output is
    threaded into upstream_output so the target node sees why it was re-invoked."""
    seen_upstream: list = []

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        if req.node.id == "plan":
            seen_upstream.append(req.upstream_output)
            return NodeRunResponse(output="plan output")
        # verify: emit MISSING on first call, GROUNDED on second
        output = "MISSING" if len(seen_upstream) == 0 else "GROUNDED"
        return NodeRunResponse(output=output)

    wf = parse_workflow({
        "name": "w", "start": "verify", "max_visits": 5,
        "nodes": [
            {"id": "verify", "kind": "work",
             "cases": {"GROUNDED": None, "MISSING": "plan"}},
            {"id": "plan", "kind": "work", "next": "verify"},
        ],
    })
    res = WorkflowEngine(runner, run_id_factory=lambda: "r1").run(wf, "t")
    assert res.status == "completed"
    # plan must have received the verify output as reviewer feedback
    assert any("MISSING" in (u or "") for u in seen_upstream)


def test_cases_visit_cap_still_bounds_loop():
    """A cases loop-back is subject to the same visit cap as binary routing."""
    wf = parse_workflow({
        "name": "w", "start": "check", "max_visits": 2,
        "nodes": [
            {"id": "check", "kind": "work", "cases": {"RETRY": "check", "DONE": None}},
        ],
    })
    eng, calls = _cases_engine({"check": "RETRY"})  # always loops back
    res = eng.run(wf, "t")
    assert res.status == "exhausted"
    assert res.exhausted_node == "check"
    assert len(calls) == 2  # ran exactly max_visits times


def test_exhausted_node_final_output_names_the_last_producer():
    """When a routing node exhausts, final_output_node names the last non-routing
    producer before exhaustion (whose output became final_output)."""
    wf = parse_workflow({
        "name": "w", "start": "produce", "max_visits": 3,
        "nodes": [
            {"id": "produce", "kind": "work", "next": "review"},
            {"id": "review", "kind": "work", "cases": {"RETRY": "review", "DONE": None}, "max_visits": 2},
        ],
    })
    eng, _ = _cases_engine({"produce": "the draft output", "review": "RETRY"})
    res = eng.run(wf, "t")
    assert res.status == "exhausted"
    assert res.exhausted_node == "review"
    assert res.final_output == "the draft output"
    assert res.final_output_node == "produce"


def test_cases_binary_routing_regression():
    """Binary on_pass/on_fail nodes still work exactly as before (regression guard)."""
    wf = parse_workflow({
        "name": "w", "start": "prod", "max_visits": 5,
        "nodes": [
            {"id": "prod", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "on_pass": "done", "on_fail": "prod"},
            {"id": "done", "kind": "work", "next": None},
        ],
    })
    outputs = {"prod": "draft", "gate": "PASS\nok", "done": "final"}
    eng, calls = _cases_engine(outputs)
    res = eng.run(wf, "t")
    assert res.status == "completed"
    gate_run = next(r for r in res.runs if r.node_id == "gate")
    assert gate_run.passed is True
    assert gate_run.route_label is None  # binary node: no route_label


def test_cases_null_target_completes_and_final_output_is_producer():
    """A cases node routing to null (end) ends the run as 'completed'.

    final_output is the last non-routing producer's output — the cases node itself
    is the routing judge; the upstream producer ('build') is the last real producer.
    This locks the terminal-route semantics so a refactor cannot silently change them.
    """
    wf = parse_workflow({
        "name": "w", "start": "build",
        "nodes": [
            {"id": "build", "kind": "work", "next": "check"},
            {"id": "check", "kind": "work",
             "cases": {"DONE": None, "RETRY": "build"}},
        ],
    })
    eng, _ = _cases_engine({"build": "producer-output", "check": "DONE"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    # The cases node routed to null; 'build' was the last non-routing producer.
    assert res.final_output == "producer-output"


def test_frame_task_output_format_overrides_the_workflow_output_description():
    # A call-time output_format replaces the workflow's default deliverable hint for this run.
    wf = parse_workflow({
        "name": "w", "start": "a",
        "output": {"text": True, "description": "a cited prose answer"},
        "nodes": [{"id": "a", "kind": "work"}],
    })
    framed = WorkflowEngine._frame_task(wf, "the question", output_format="a 3-bullet list")
    assert "a 3-bullet list" in framed
    assert "a cited prose answer" not in framed   # the override wins for this run


def test_frame_task_without_override_uses_the_workflow_output_description():
    wf = parse_workflow({
        "name": "w", "start": "a",
        "output": {"text": True, "description": "a cited prose answer"},
        "nodes": [{"id": "a", "kind": "work"}],
    })
    framed = WorkflowEngine._frame_task(wf, "q")
    assert "a cited prose answer" in framed


def test_frame_task_output_format_works_without_a_declared_output():
    # Even a workflow with no output descriptor honors a call-time delivery instruction.
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [{"id": "a", "kind": "work"}]})
    framed = WorkflowEngine._frame_task(wf, "q", output_format="JSON with fields x,y")
    assert "JSON with fields x,y" in framed


def test_engine_passes_the_visit_budget_to_the_runner():
    wf = parse_workflow({
        "name": "d", "start": "make", "max_visits": 4,
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate", "max_visits": 2},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": None, "on_fail": "make"},
        ],
    })
    eng, calls = _engine({"make": "draft", "gate": "PASS"})
    eng.run(wf, "t")
    make_call = [c for c in calls if c.node.id == "make"][0]
    gate_call = [c for c in calls if c.node.id == "gate"][0]
    assert make_call.budget == 2      # per-node override
    assert gate_call.budget == 4      # workflow default


def test_node_run_records_the_effective_budget():
    wf = parse_workflow({
        "name": "d", "start": "make", "max_visits": 4,
        "nodes": [
            {"id": "make", "kind": "work", "next": None, "max_visits": 2},
        ],
    })
    eng, _calls = _engine({"make": "draft"})
    res = eng.run(wf, "t")
    assert res.runs[0].node_id == "make"
    assert res.runs[0].budget == 2


def test_gate_learns_when_a_fail_would_exhaust_the_producer():
    wf = parse_workflow({
        "name": "d", "start": "make", "max_visits": 2,
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": None, "on_fail": "make"},
        ],
    })
    eng, calls = _engine({"make": "draft", "gate": "FAIL nope"})
    eng.run(wf, "t")
    gate_calls = [c for c in calls if c.node.id == "gate"]
    # 1st gate visit: producer has 1 visit of 2 → a FAIL still loops (False).
    # 2nd gate visit: producer consumed 2 of 2 → a FAIL would exhaust (True).
    assert gate_calls[0].fail_would_exhaust is False
    assert gate_calls[1].fail_would_exhaust is True


def test_forward_fail_route_threads_neutral_context_not_reviewer_feedback():
    """A FAIL whose target never leads back to the gate is a forward route ("no note
    yet → go draft it"), not a revision request: the target gets the gate's output as
    neutral context. A FAIL from judge into that same target leads back to judge, so
    it is a real loop-back and carries the reviewer framing."""
    seen: list = []
    judged = []

    def runner(req):
        if req.node.id == "draft":
            seen.append(req.upstream_output or "")
            return NodeRunResponse(output="note v1")
        if req.node.id == "has-note":
            return NodeRunResponse(output="FAIL\nnote.json not present")
        judged.append(1)
        return NodeRunResponse(output="FAIL\ncite the log line" if len(judged) == 1 else "PASS")

    wf = parse_workflow({"name": "w", "start": "has-note", "max_visits": 3, "nodes": [
        {"id": "has-note", "kind": "work", "prompt": "note there?",
         "on_pass": "judge", "on_fail": "draft"},
        {"id": "draft", "kind": "work", "next": "judge"},
        {"id": "judge", "kind": "work", "prompt": "good?", "on_pass": None, "on_fail": "draft"},
    ]})
    res = WorkflowEngine(runner).run(wf, "t")
    assert res.status == "completed"
    first, second = seen
    assert "Context from 'has-note':\nFAIL\nnote.json not present" in first
    assert "Reviewer feedback" not in first
    assert "Reviewer feedback (address this):\nFAIL\ncite the log line" in second


def test_gate_learns_whether_its_fail_route_loops_back():
    wf = parse_workflow({"name": "w", "start": "has-note", "max_visits": 3, "nodes": [
        {"id": "has-note", "kind": "work", "prompt": "note there?",
         "on_pass": "judge", "on_fail": "draft"},
        {"id": "draft", "kind": "work", "next": "judge"},
        {"id": "judge", "kind": "work", "prompt": "good?", "on_pass": None, "on_fail": "draft"},
    ]})
    eng, calls = _engine({"has-note": "FAIL none", "draft": "note", "judge": "PASS"})
    eng.run(wf, "t")
    by_id = {c.node.id: c for c in calls}
    assert by_id["has-note"].fail_loops_back is False   # draft never leads back to has-note
    assert by_id["judge"].fail_loops_back is True       # draft leads back to judge


def test_fail_loops_back_follows_the_graph_even_before_the_target_ran():
    # The note already exists, so has-note passes straight to judge: draft has not
    # run when judge is asked, yet a FAIL would send it back through draft to judge.
    wf = parse_workflow({"name": "w", "start": "has-note", "max_visits": 3, "nodes": [
        {"id": "has-note", "kind": "work", "prompt": "note there?",
         "on_pass": "judge", "on_fail": "draft"},
        {"id": "draft", "kind": "work", "next": "judge"},
        {"id": "judge", "kind": "work", "prompt": "good?", "on_pass": None, "on_fail": "draft"},
    ]})
    eng, calls = _engine({"has-note": "PASS", "judge": "PASS"})
    eng.run(wf, "t")
    by_id = {c.node.id: c for c in calls}
    assert by_id["has-note"].fail_loops_back is False
    assert by_id["judge"].fail_loops_back is True


def test_review_fail_into_a_fixer_that_has_not_run_yet_is_reviewer_feedback():
    """The first time draft runs can be a revision: the note already exists, has-note
    passes to judge, and judge's FAIL sends draft to rework it. draft leads back to
    judge, so it gets the verdict as feedback to address, not as neutral context."""
    seen: list = []
    judged = []

    def runner(req):
        if req.node.id == "draft":
            seen.append(req.upstream_output or "")
            return NodeRunResponse(output="note v2")
        if req.node.id == "has-note":
            return NodeRunResponse(output="PASS\nnote.json present")
        judged.append(1)
        return NodeRunResponse(output="FAIL\ncite the log line" if len(judged) == 1 else "PASS")

    wf = parse_workflow({"name": "w", "start": "has-note", "max_visits": 3, "nodes": [
        {"id": "has-note", "kind": "work", "prompt": "note there?",
         "on_pass": "judge", "on_fail": "draft"},
        {"id": "draft", "kind": "work", "next": "judge"},
        {"id": "judge", "kind": "work", "prompt": "good?", "on_pass": None, "on_fail": "draft"},
    ]})
    res = WorkflowEngine(runner).run(wf, "t")
    assert res.status == "completed"
    (only,) = seen
    assert "Reviewer feedback (address this):\nFAIL\ncite the log line" in only


def test_a_parallel_list_source_is_not_a_path_back_to_the_gate():
    # 'fan' reads lister's output as its item list, but the walk goes from fan to
    # fan's next, never to lister: check's FAIL into fan is a forward route.
    wf = parse_workflow({"name": "w", "start": "lister", "nodes": [
        {"id": "lister", "kind": "work", "next": "check"},
        {"id": "check", "kind": "work", "prompt": "nothing to do?",
         "on_pass": None, "on_fail": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "item", "list_from": "lister", "next": None},
        {"id": "item", "kind": "work"},
    ]})
    eng, calls = _engine({"lister": "[]", "check": "PASS"})
    eng.run(wf, "t")
    by_id = {c.node.id: c for c in calls}
    assert by_id["check"].fail_loops_back is False


def test_a_fail_route_that_returns_only_through_the_pass_step_is_a_forward_route():
    """draft goes on to send, has-note's own pass step, and only send's retry edge
    leads back to has-note. That cycle is send's, not has-note's FAIL: on the first
    run nothing was drafted before, so draft gets the "no" as neutral context."""
    seen: list = []
    calls = []

    def runner(req):
        calls.append(req)
        if req.node.id == "has-note":
            return NodeRunResponse(output="FAIL\nnote.json not present")
        if req.node.id == "draft":
            seen.append(req.upstream_output or "")
            return NodeRunResponse(output="note")
        return NodeRunResponse(output="PASS")

    wf = parse_workflow({"name": "w", "start": "has-note", "max_visits": 3, "nodes": [
        {"id": "has-note", "kind": "work", "prompt": "note there?",
         "on_pass": "send", "on_fail": "draft"},
        {"id": "draft", "kind": "work", "next": "send"},
        {"id": "send", "kind": "work", "prompt": "delivered?",
         "on_pass": None, "on_fail": "has-note"},
    ]})
    res = WorkflowEngine(runner).run(wf, "t")
    assert res.status == "completed"
    assert calls[0].node.id == "has-note" and calls[0].fail_loops_back is False
    (only,) = seen
    assert "Context from 'has-note':\nFAIL\nnote.json not present" in only
    assert "Reviewer feedback" not in only


def test_fail_loops_back_inside_an_outer_loop_over_items():
    # The loop over items brings every node round again. judge's FAIL returns to
    # judge on a path of its own (produce -> judge); has-note's FAIL reaches
    # has-note again only by way of send, its pass step, so it is a forward route.
    picks = iter(["MORE", "DONE"])
    calls = []

    def runner(req):
        calls.append(req)
        if req.node.id == "pick":
            return NodeRunResponse(output=next(picks))
        return NodeRunResponse(output="PASS" if req.node.id in ("judge", "has-note") else "ok")

    wf = parse_workflow({"name": "w", "start": "pick", "max_visits": 3, "nodes": [
        {"id": "pick", "kind": "work", "prompt": "more items?",
         "cases": {"MORE": "produce", "DONE": None}},
        {"id": "produce", "kind": "work", "next": "judge"},
        {"id": "judge", "kind": "work", "prompt": "good?",
         "on_pass": "has-note", "on_fail": "produce"},
        {"id": "has-note", "kind": "work", "prompt": "note there?",
         "on_pass": "send", "on_fail": "draft"},
        {"id": "draft", "kind": "work", "next": "send"},
        {"id": "send", "kind": "work", "next": "pick"},
    ]})
    assert WorkflowEngine(runner).run(wf, "t").status == "completed"
    by_id = {c.node.id: c for c in calls}
    assert by_id["judge"].fail_loops_back is True
    assert by_id["has-note"].fail_loops_back is False


def test_sequential_nodes_share_one_working_dir(tmp_path):
    # Every sequential node — looping or hand-off — reads and writes ONE shared per-run
    # folder, so files accumulate in one place and each stage sees the prior work. (Before,
    # per-node/per-iteration folders scattered a self-loop's files and broke collaboration.)
    seen: dict = {}
    labels = iter(["MORE", "MORE", "DONE"])

    def runner(req):
        seen.setdefault(req.node.id, []).append(req.output_dir)
        out = next(labels) if req.node.id == "loop" else "ok"
        return NodeRunResponse(output=out, session_key="k", messages=[])

    eng = WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))
    wf = parse_workflow({
        "name": "w", "start": "loop", "max_visits": 6,
        "nodes": [
            {"id": "loop", "kind": "work", "tools": "default",
             "cases": {"MORE": "loop", "DONE": "done"}},
            {"id": "done", "kind": "work", "tools": "default", "next": None},
        ],
    })
    eng.run(wf, "go")

    all_dirs = seen["loop"] + seen["done"]
    assert len(seen["loop"]) == 3                    # the loop still looped three times
    assert len(set(all_dirs)) == 1                   # every node + iteration shares one dir
    assert all_dirs[0].endswith("/work")             # the run's shared working folder


def test_needs_input_result_names_the_asking_node():
    wf = parse_workflow({
        "name": "d", "start": "gate",
        "nodes": [{"id": "gate", "kind": "work", "prompt": "clarify?",
                   "cases": {"OK": None, "NEED_INFO": "__needs_input__"}}],
    })
    eng, _ = _engine({"gate": "what env?\nNEED_INFO"})
    result = eng.run(wf, "t")
    assert result.status == "needs_input"
    assert result.needs_input_node == "gate"
    assert result.final_output_node == "gate"
    # The routing label is transport metadata, not part of the question.
    assert result.final_output == "what env?"


def test_needs_input_keeps_output_when_label_is_the_only_line():
    wf = parse_workflow({
        "name": "d", "start": "gate",
        "nodes": [{"id": "gate", "kind": "work", "prompt": "clarify?",
                   "cases": {"OK": None, "NEED_INFO": "__needs_input__"}}],
    })
    eng, _ = _engine({"gate": "NEED_INFO"})
    result = eng.run(wf, "t")
    assert result.status == "needs_input"
    assert result.final_output == "NEED_INFO"


def test_resume_reenters_at_the_asking_node_with_carried_visits(tmp_path):
    from durin.workflow.engine import ResumeState
    wf = parse_workflow({
        "name": "d", "start": "plan",
        "nodes": [
            {"id": "plan", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "clarify?",
             "cases": {"OK": None, "NEED_INFO": "__needs_input__"}},
        ],
    })
    eng, calls = _engine({"plan": "the plan", "gate": "OK"})
    result = eng.run(wf, "answers text", resume=ResumeState(
        run_id="fixed-run", start_at="gate",
        visits={"plan": 1, "gate": 1}, upstream="User answers:\nprod env",
    ))
    assert result.status == "completed"
    assert result.run_id == "fixed-run"
    assert [c.node.id for c in calls] == ["gate"]        # plan NOT re-run
    gate_call = calls[0]
    assert gate_call.iteration == 2                       # continues the count
    assert "prod env" in (gate_call.upstream_output or "")


def test_terminal_binary_gate_contributes_its_output():
    wf = parse_workflow({
        "name": "d", "start": "make",
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": None, "on_fail": "make"},
        ],
    })
    eng, _ = _engine({"make": "the draft", "gate": "PASS\nVerified: tests green, docs updated."})
    result = eng.run(wf, "t")
    assert result.status == "completed"
    assert result.final_output == "Verified: tests green, docs updated."


def test_terminal_gate_with_bare_verdict_keeps_producer_output():
    wf = parse_workflow({
        "name": "d", "start": "make",
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": None, "on_fail": "make"},
        ],
    })
    eng, _ = _engine({"make": "the draft", "gate": "PASS"})
    result = eng.run(wf, "t")
    assert result.final_output == "the draft"      # bare verdict adds nothing
    assert result.final_output_node == "make"      # residue empty — stays the producer's id


def test_a_terminal_gate_decided_by_route_keeps_its_content():
    """The verdict came from the node's `route` call: its review, checklist bullets
    included, is the run's output; only a bare closing verdict that agrees with
    the call is dropped, and a review without one is kept whole."""
    wf = parse_workflow({
        "name": "d", "start": "make",
        "nodes": [
            {"id": "make", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "ok?", "on_pass": "make", "on_fail": None},
        ],
    })
    review = ("## Final review\n\n- PASS: citations resolve\n- FAIL: the expiry date is wrong "
              "(says 30 days, contract says 14)\n\nThe note cannot go out as written.")

    for gate_output in (review, f"{review}\n\nFAIL"):
        def node_runner(req: NodeRunRequest, gate_output=gate_output) -> NodeRunResponse:
            if req.node.id == "make":
                return NodeRunResponse(output="the draft")
            return NodeRunResponse(output=gate_output, route_label="FAIL")

        result = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1").run(wf, "t")
        assert result.final_route_label == "FAIL"
        assert result.final_output == review


def test_terminal_cases_node_contributes_its_output():
    wf = parse_workflow({
        "name": "d", "start": "synth",
        "nodes": [
            {"id": "synth", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "prompt": "grounded?",
             "cases": {"GROUNDED": None, "MISSING": "synth"}},
        ],
    })
    eng, _ = _engine({"synth": "draft answer", "gate": "Final answer: 42.\nGROUNDED"})
    result = eng.run(wf, "t")
    assert result.final_output == "Final answer: 42."


def test_final_output_node_names_the_linear_terminal():
    wf = parse_workflow({"name": "w", "start": "a",
                         "nodes": [{"id": "a", "kind": "work", "next": None}]})
    eng, _ = _engine({"a": "out"})
    assert eng.run(wf, "t").final_output_node == "a"


def test_final_output_node_names_a_contributing_terminal_gate():
    wf = parse_workflow({"name": "w", "start": "make",
                         "nodes": [
                             {"id": "make", "kind": "work", "next": "gate"},
                             {"id": "gate", "kind": "work", "prompt": "ok?",
                              "on_pass": None, "on_fail": "make"}]})
    eng, _ = _engine({"make": "draft", "gate": "PASS\nVerified."})
    r = eng.run(wf, "t")
    assert r.final_output == "Verified." and r.final_output_node == "gate"


def test_cancelled_result_names_the_last_producer_node():
    """When cancel_check returns True between nodes, the run ends with status='cancelled'
    and final_output_node names the last node that ran (whose output became final_output)."""
    wf = parse_workflow({
        "name": "w", "start": "first",
        "nodes": [
            {"id": "first", "kind": "work", "next": "second"},
            {"id": "second", "kind": "work", "next": None},
        ],
    })
    # cancel_check returns True after the first node runs
    cancel_on_second = [False]
    def cancel_check():
        if cancel_on_second[0]:
            return True
        cancel_on_second[0] = True
        return False

    eng = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(
            output=f"output-{req.node.id}", session_key=None, messages=[]
        ),
        run_id_factory=lambda: "r1",
        cancel_check=cancel_check,
    )
    res = eng.run(wf, "t")
    assert res.status == "cancelled"
    assert res.final_output == "output-first"
    assert res.final_output_node == "first"


def test_cancelled_during_terminal_subworkflow_node_reports_cancelled():
    """A cancel that lands entirely inside a subworkflow node's nested run must not
    be misreported as 'completed' when that subworkflow node is the parent's LAST
    node. SubworkflowRunner.__call__ returns a plain string with no status (other
    callers depend on that shape), and — unlike every other node type — there is no
    next loop iteration here for the ordinary top-of-loop cancel_check to run in,
    so without a re-check right after the branch the walk falls through to the
    completed result at the bottom of _walk."""
    wf = parse_workflow({"name": "d", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": None},
    ]})

    def subworkflow_runner(name, task, root_session_key=None, work_dir=None,
                           parent_run_id=None, **_kwargs):
        return "child-partial-output"

    # False on the top-of-loop check before "sub" runs (so it gets a chance to run
    # at all), True on every call after — simulating a cancel that was requested
    # while the nested run was in flight and only discovered once it returns.
    cancel_after_first_check = [False]

    def cancel_check():
        if cancel_after_first_check[0]:
            return True
        cancel_after_first_check[0] = True
        return False

    eng = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(output="unused"),
        run_id_factory=lambda: "r1",
        subworkflow_runner=subworkflow_runner,
        cancel_check=cancel_check,
    )
    res = eng.run(wf, "t")
    assert res.status == "cancelled"
    assert res.final_output == "child-partial-output"
    assert res.final_output_node == "sub"


def test_cancelled_during_non_terminal_subworkflow_node_still_reports_cancelled():
    """Regression guard for the non-terminal case: when the subworkflow node has a
    next node, the ordinary top-of-loop cancel_check already catches the cancel
    before that next node starts — this must keep working exactly as before."""
    wf = parse_workflow({"name": "d", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "after"},
        {"id": "after", "kind": "work", "next": None},
    ]})

    def subworkflow_runner(name, task, root_session_key=None, work_dir=None,
                           parent_run_id=None, **_kwargs):
        return "child-partial-output"

    after_ran = []

    def node_runner(req):
        after_ran.append(req.node.id)
        return NodeRunResponse(output="after-out")

    cancel_after_first_check = [False]

    def cancel_check():
        if cancel_after_first_check[0]:
            return True
        cancel_after_first_check[0] = True
        return False

    eng = WorkflowEngine(
        node_runner=node_runner,
        run_id_factory=lambda: "r1",
        subworkflow_runner=subworkflow_runner,
        cancel_check=cancel_check,
    )
    res = eng.run(wf, "t")
    assert res.status == "cancelled"
    assert after_ran == []                          # "after" never ran
    assert res.final_output == "child-partial-output"
    assert res.final_output_node == "sub"


def test_cases_terminal_stamps_final_route_label():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "g"},
            {"id": "g", "kind": "work", "cases": {"DONE": None, "MORE": "a"}},
        ],
    })
    eng, _ = _engine({"a": "out-a", "g": "verdict\nDONE"})
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert res.final_route_label == "DONE"


def test_binary_terminal_stamps_pass_label():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "g"},
            {"id": "g", "kind": "work", "on_pass": None, "on_fail": "a"},
        ],
    })
    eng, _ = _engine({"a": "out-a", "g": "PASS\nall good"})
    res = eng.run(wf, "t")
    assert res.final_route_label == "PASS"


def test_linear_end_has_no_final_route_label():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    })
    eng, _ = _engine({"a": "out"})
    assert eng.run(wf, "t").final_route_label is None
