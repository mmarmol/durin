from pathlib import Path
from durin.workflow.engine import WorkflowEngine, NodeRunResponse
from durin.workflow.spec import parse_workflow


def _wf(nodes, start):
    return parse_workflow({"name": "w", "start": start, "nodes": nodes})


def _parallel_wf(reconcile):
    return parse_workflow({
        "name": "w", "start": "seed",
        "nodes": [
            {"id": "seed", "kind": "work", "tools": "default", "next": "par"},
            {"id": "par", "kind": "parallel", "branches": ["b1"],
             "reconcile": reconcile, "next": None,
             **({"criteria": "best"} if reconcile == "choose" else {})},
            {"id": "b1", "kind": "work", "tools": "default"},
        ],
    })


def test_writing_branch_sees_and_extends_the_shared_work_folder(tmp_path):
    seen = {}
    def runner(req):
        if req.node.id == "seed":
            Path(req.output_dir, "draft.md").write_text("v1")
        if req.node.id == "b1":
            seen["saw"] = Path(req.output_dir, "draft.md").read_text()
            Path(req.output_dir, "draft.md").write_text("v2")
        return NodeRunResponse(output=f"out-{req.node.id}")
    eng = WorkflowEngine(runner, workspace=str(tmp_path),
                         pick_runner=lambda c, outs, m: 0)
    result = eng.run(_parallel_wf("choose"), "t")
    assert result.status == "completed"
    assert seen["saw"] == "v1"                                # fork was seeded
    work = Path(tmp_path) / ".workflow" / result.run_id / "work"
    assert (work / "draft.md").read_text() == "v2"            # write reconciled back


def test_read_branch_and_dynamic_worker_get_the_shared_folder(tmp_path):
    seen = {}
    def runner(req):
        seen[(req.node.id, req.worker_index)] = req.output_dir
        if req.node.id == "lister":
            return NodeRunResponse(output='["s1", "s2"]')
        return NodeRunResponse(output="x")
    wf = parse_workflow({
        "name": "w", "start": "lister",
        "nodes": [
            {"id": "lister", "kind": "work", "tools": "default", "next": "read_par"},
            {"id": "read_par", "kind": "parallel", "branches": ["rb"],
             "reconcile": "read", "next": "fan"},
            {"id": "rb", "kind": "work", "tools": "default"},
            {"id": "fan", "kind": "parallel", "worker": "wk", "list_from": "lister", "next": None},
            {"id": "wk", "kind": "work", "tools": "default"},
        ],
    })
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    shared = seen[("lister", None)]
    assert seen[("rb", None)] == shared                       # read branch told the folder
    assert seen[("wk", 0)] == shared                          # dynamic worker told the folder
    assert seen[("wk", 1)] == shared


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


def test_preflight_rejection_writes_no_manifest_and_prunes_nothing(tmp_path):
    """Pre-flight rejection must not create the manifest record or artifact folders.
    When a preflight check (declared file input without files) rejects the run,
    no manifest should be written and the artifact tree should not be created."""
    wf = parse_workflow({
        "name": "w", "start": "a",
        "input": {"file": True},
        "nodes": [{"id": "a", "kind": "work", "tools": "default", "next": None}],
    })
    result = WorkflowEngine(lambda req: NodeRunResponse(output="x"),
                            workspace=str(tmp_path)).run(wf, "t")
    assert result.status == "needs_input"
    # Pre-flight rejection must not create the manifest folder
    assert not (Path(tmp_path) / "workflows-runs").exists()


def test_completed_result_lists_output_files(tmp_path):
    def runner(req):
        Path(req.output_dir, "report.md").write_text("done")
        Path(req.output_dir, "sub").mkdir(exist_ok=True)
        Path(req.output_dir, "sub", "data.csv").write_text("a,b")
        return NodeRunResponse(output="x")
    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    result = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    assert sorted(result.output_files) == ["report.md", "sub/data.csv"]


def test_engine_prune_keep_is_wired(tmp_path):
    import durin.workflow.engine as engine_mod
    seen = {}
    def fake_prune(base, keep=20):
        seen["keep"] = keep
    orig = engine_mod.prune_runs
    engine_mod.prune_runs = fake_prune
    try:
        wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
        WorkflowEngine(lambda req: NodeRunResponse(output="x"),
                       workspace=str(tmp_path), prune_keep=5).run(wf, "t")
    finally:
        engine_mod.prune_runs = orig
    assert seen["keep"] == 5


# work_dir_override: nested/subworkflow runs must not prune or crash the parent's folder

def test_override_run_never_prunes(tmp_path):
    import durin.workflow.engine as engine_mod
    called = {}
    orig = engine_mod.prune_runs
    engine_mod.prune_runs = lambda base, keep=20: called.setdefault("hit", True)
    try:
        override = tmp_path / "pw"
        override.mkdir()
        wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
        WorkflowEngine(lambda req: NodeRunResponse(output="x"),
                       workspace=str(tmp_path)).run(wf, "t", work_dir_override=str(override))
    finally:
        engine_mod.prune_runs = orig
    assert "hit" not in called


def test_active_run_folder_mtime_refreshes_per_node(tmp_path):
    import os
    import time
    seen = {}
    def runner(req):
        seen.setdefault("dir", req.output_dir)
        run_root = Path(req.output_dir).parent
        old = run_root.stat().st_mtime - 3600
        os.utime(run_root, (old, old))          # simulate a stale folder mid-run
        return NodeRunResponse(output="x")
    wf = _wf([
        {"id": "a", "kind": "work", "tools": "default", "next": "b"},
        {"id": "b", "kind": "work", "tools": "default", "next": None},
    ], "a")
    WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "t")
    run_root = Path(seen["dir"]).parent
    assert time.time() - run_root.stat().st_mtime < 60   # engine re-touched it after the node


# work_dir_override + input_files: the override dir may not exist yet

def test_input_files_seeded_into_a_not_yet_existing_override_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "report.txt").write_text("hello")
    override = tmp_path / "not-yet"                # deliberately not created

    seen = {}
    def runner(req):
        seen[req.node.id] = req.output_dir
        return NodeRunResponse(output="x")

    wf = _wf([{"id": "a", "kind": "work", "tools": "default", "next": None}], "a")
    result = WorkflowEngine(runner, workspace=str(tmp_path)).run(
        wf, "t", input_files=[str(src / "report.txt")], work_dir_override=str(override))

    assert result.status == "completed"
    assert (override / "report.txt").read_text() == "hello"


# ---------------------------------------------------------------------------
# Declared artifacts (output.artifacts): post-run verification + node framing
# ---------------------------------------------------------------------------

def _artifact_wf(produce_cmd):
    from durin.workflow.spec import parse_workflow
    return parse_workflow({
        "name": "contract", "start": "s",
        "output": {"file": True, "artifacts": [
            {"path": "context.json", "description": "the context"},
            {"path": "evidence.json"},
        ]},
        "nodes": [{"id": "s", "kind": "script", "command": produce_cmd, "next": None}],
    })


def _script_engine(tmp_path):
    from durin.workflow.engine import WorkflowEngine
    from durin.workflow.script_runner import ScriptNodeRunner
    return WorkflowEngine(
        node_runner=lambda req: (_ for _ in ()).throw(RuntimeError("no agent nodes")),
        script_runner=ScriptNodeRunner(str(tmp_path)),
        workspace=str(tmp_path),
    )


def test_missing_declared_artifacts_reported_as_warning(tmp_path):
    from durin.workflow import run_log
    result = _script_engine(tmp_path).run(_artifact_wf("echo '{}' > context.json"), "go")
    assert result.status == "completed"          # warning, never a failure
    assert result.missing_artifacts == ["evidence.json"]
    rec = run_log.read_manifest(tmp_path, "contract", result.run_id)
    assert rec["missing_artifacts"] == ["evidence.json"]


def test_all_declared_artifacts_present_reports_none_missing(tmp_path):
    result = _script_engine(tmp_path).run(
        _artifact_wf("echo '{}' > context.json; echo '[]' > evidence.json"), "go")
    assert result.status == "completed"
    assert result.missing_artifacts == []


def test_framing_carries_declared_artifacts(tmp_path):
    from durin.workflow.engine import WorkflowEngine
    wf = _artifact_wf("true")
    framed = WorkflowEngine._frame_task(wf, "the task")
    assert "context.json" in framed and "the context" in framed
    assert "evidence.json" in framed
