"""Script nodes as parallel branches.

The original restriction (branches must be work nodes) conflated "a script can
iterate internally" with "a script may not run BESIDE an LLM branch". Live
evidence (mxHero box, 2026-07-22): stage1 wants download/convert/classify (a
deterministic script chain collapsible into one script) overlapped with the
60-90s analyze-ticket LLM node — inexpressible without mixed-kind branches.

A script branch behaves like the linear script contract, per branch: stdin is
the parallel node's upstream text, stdout is the branch output in the fan-in,
cwd is the run's shared working folder (a private fork of it under
choose/union), a non-zero exit marks THAT branch failed while survivors
complete, and the exit code lands in the branch's trace record.
"""

from pathlib import Path

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import parse_workflow


def _write_script(workspace: Path, name: str, body: str) -> None:
    d = workspace / "workflows" / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)


def _mixed_wf(reconcile="read", **extra):
    return parse_workflow({"name": "d", "start": "fan", "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["think", "fetch"],
         "reconcile": reconcile, "next": "join", **extra},
        {"id": "think", "kind": "work"},
        {"id": "fetch", "kind": "script", "script": "fetch.py"},
        {"id": "join", "kind": "work", "next": None},
    ]})


def _engine(tmp_path, node_runner):
    return WorkflowEngine(
        node_runner=node_runner,
        script_runner=ScriptNodeRunner(tmp_path),
        run_id_factory=lambda: "r1",
        workspace=str(tmp_path),
    )


def test_spec_accepts_a_script_branch():
    wf = _mixed_wf()
    assert wf.nodes["fan"].branches == ("think", "fetch")


def test_script_branch_runs_beside_the_llm_branch(tmp_path):
    _write_script(tmp_path, "fetch.py",
                  "import sys\nprint('fetched: ' + sys.stdin.read().strip())\n")

    def node_runner(req):
        return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])

    res = _engine(tmp_path, node_runner).run(_mixed_wf(), "the task")
    assert res.status == "completed"
    fan = next(r for r in res.runs if r.node_id == "fan")
    assert "[think]\nthink-out" in fan.output
    assert "[fetch]\nfetched: the task" in fan.output   # stdin = the parallel's input
    fetch = next(r for r in res.runs if r.node_id == "fetch")
    assert fetch.exit_code == 0
    assert fetch.branch_id == "fetch"


def test_script_branch_works_in_the_shared_folder(tmp_path):
    _write_script(tmp_path, "fetch.py",
                  "from pathlib import Path\nPath('fetched.json').write_text('{}')\nprint('ok')\n")

    def node_runner(req):
        return NodeRunResponse(output="x", session_key=None, messages=[])

    res = _engine(tmp_path, node_runner).run(_mixed_wf(), "t")
    assert res.status == "completed"
    assert (tmp_path / ".workflow" / "r1" / "work" / "fetched.json").exists()


def test_failed_script_branch_is_isolated_and_recorded(tmp_path):
    _write_script(tmp_path, "fetch.py", "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")

    def node_runner(req):
        return NodeRunResponse(output=f"{req.node.id}-out", session_key=None, messages=[])

    res = _engine(tmp_path, node_runner).run(_mixed_wf(), "t")
    assert res.status == "completed"                      # the surviving branch carried on
    fan = next(r for r in res.runs if r.node_id == "fan")
    assert "[think]\nthink-out" in fan.output
    assert "[fetch] FAILED:" in fan.output
    fetch = next(r for r in res.runs if r.node_id == "fetch")
    assert fetch.status == "node_failed"
    assert fetch.exit_code == 3


def test_script_branch_forks_under_union(tmp_path):
    _write_script(tmp_path, "fetch.py",
                  "from pathlib import Path\nPath('s.txt').write_text('from-script')\nprint('ok')\n")

    def node_runner(req):
        if req.node.id == "think":                       # the forked branch writes...
            Path(req.workspace_override, "w.txt").write_text("from-work")
        return NodeRunResponse(output="w", session_key=None, messages=[])   # ...join just merges

    res = _engine(tmp_path, node_runner).run(_mixed_wf("union"), "t")
    assert res.status == "completed"
    work = tmp_path / ".workflow" / "r1" / "work"
    assert (work / "s.txt").read_text() == "from-script"
    assert (work / "w.txt").read_text() == "from-work"


def test_branches_from_may_resolve_a_script_branch(tmp_path):
    _write_script(tmp_path, "fetch.py", "print('fetched')\n")
    wf = parse_workflow({"name": "d", "start": "route", "nodes": [
        {"id": "route", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches_from": "route", "next": None},
        {"id": "fetch", "kind": "script", "script": "fetch.py"},
        {"id": "think", "kind": "work"},
    ]})

    def node_runner(req):
        out = '["fetch"]' if req.node.id == "route" else f"{req.node.id}-out"
        return NodeRunResponse(output=out, session_key=None, messages=[])

    res = _engine(tmp_path, node_runner).run(wf, "t")
    assert res.status == "completed"
    fan = next(r for r in res.runs if r.node_id == "fan")
    assert "[fetch]\nfetched" in fan.output


def test_missing_branch_script_file_aborts_preflight(tmp_path):
    def node_runner(req):
        raise AssertionError("no node may run when pre-flight fails")

    res = _engine(tmp_path, node_runner).run(_mixed_wf(), "t")
    assert res.status == "aborted"
    assert "fetch.py" in (res.final_output or "")
    assert res.runs == []
