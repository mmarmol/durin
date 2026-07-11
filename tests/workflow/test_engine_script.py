import json
import os
import time
from importlib.resources import files as pkg_files

import pytest

from durin.workflow import run_log
from durin.workflow.artifacts import artifact_dir
from durin.workflow.engine import NodeRunRequest, WorkflowConfigError, WorkflowEngine, build_resume_state
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import parse_workflow


def fake_agent_runner(outputs):
    """Sequential canned agent outputs, mimicking AgentNodeRunner's response shape."""
    from durin.workflow.engine import NodeRunResponse
    calls = []

    def run(req):
        calls.append(req)
        return NodeRunResponse(output=outputs.pop(0), session_key=f"sess:{req.node.id}:{req.iteration}")
    run.calls = calls
    return run


def engine_for(tmp_path, agent_outputs):
    return WorkflowEngine(
        node_runner=fake_agent_runner(agent_outputs),
        script_runner=ScriptNodeRunner(str(tmp_path)),
        workspace=str(tmp_path),
    )


def test_mixed_chain_agent_script_agent(tmp_path):
    wf = parse_workflow({"name": "t", "start": "a", "nodes": [
        {"id": "a", "prompt": "produce", "next": "s"},
        {"id": "s", "kind": "script", "command": "tr a-z A-Z", "next": "b"},
        {"id": "b", "prompt": "consume", "next": None},
    ]})
    eng = engine_for(tmp_path, ["hello world", "final answer"])
    result = eng.run(wf, "task")
    assert result.status == "completed"
    # the script saw the agent's output on stdin, the next agent saw the script's stdout
    b_req = eng._node_runner.calls[1]
    assert "HELLO WORLD" in (b_req.upstream_output or "")
    trace = {r.node_id: r for r in result.runs}
    assert trace["s"].exit_code == 0 and trace["s"].session_key is None
    assert trace["a"].exit_code is None


def test_script_gate_fail_loops_back_with_feedback(tmp_path):
    marker = tmp_path / "fixed"
    # gate: fails until the marker file exists (the "producer" creates it on retry)
    gate_cmd = f'test -f "{marker}" || {{ echo "missing marker" >&2; exit 1; }}'
    wf = parse_workflow({"name": "t", "start": "p", "max_visits": 3, "nodes": [
        {"id": "p", "prompt": "produce", "next": "g"},
        {"id": "g", "kind": "script", "command": gate_cmd, "on_pass": None, "on_fail": "p"},
    ]})

    from durin.workflow.engine import NodeRunResponse
    passes = []

    def producer(req):
        passes.append(req.upstream_output)
        if len(passes) == 2:
            marker.write_text("ok")
        return NodeRunResponse(output=f"attempt {len(passes)}", session_key="s")

    eng = WorkflowEngine(node_runner=producer,
                         script_runner=ScriptNodeRunner(str(tmp_path)),
                         workspace=str(tmp_path))
    result = eng.run(wf, "task")
    assert result.status == "completed"
    # second producer pass received the gate's stderr as reviewer feedback
    assert "missing marker" in (passes[1] or "") and "exit code 1" in (passes[1] or "")


def test_script_cases_routing_and_needs_input(tmp_path):
    wf = parse_workflow({"name": "t", "start": "c", "nodes": [
        {"id": "c", "kind": "script",
         "command": "echo what is the target env?; echo MISSING",
         "cases": {"READY": None, "MISSING": "__needs_input__"}},
    ]})
    result = engine_for(tmp_path, []).run(wf, "task")
    assert result.status == "needs_input" and result.needs_input_node == "c"
    assert "target env" in (result.final_output or "")


def test_script_as_list_from_source(tmp_path):
    wf = parse_workflow({"name": "t", "start": "lst", "nodes": [
        {"id": "lst", "kind": "script", "command": "echo '[\"x\", \"y\"]'", "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "w", "list_from": "lst", "next": None},
        {"id": "w", "prompt": "work one item"},
    ]})
    eng = engine_for(tmp_path, ["did x", "did y"])
    result = eng.run(wf, "task")
    assert result.status == "completed"
    worker_runs = [r for r in result.runs if r.node_id == "w"]
    assert len(worker_runs) == 2


def test_linear_script_failure_aborts_named(tmp_path):
    wf = parse_workflow({"name": "t", "start": "s", "nodes": [
        {"id": "s", "kind": "script", "command": "exit 9", "next": None},
    ]})
    result = engine_for(tmp_path, []).run(wf, "task")
    assert result.status == "aborted" and result.failed_node == "s"
    assert "exited with code 9" in (result.final_output or "")


def test_preflight_missing_script_file_aborts_before_any_node(tmp_path):
    wf = parse_workflow({"name": "t", "start": "a", "nodes": [
        {"id": "a", "prompt": "never runs", "next": "s"},
        {"id": "s", "kind": "script", "script": "ghost.py", "next": None},
    ]})
    eng = engine_for(tmp_path, ["should not run"])
    result = eng.run(wf, "task")
    assert result.status == "aborted" and "ghost.py" in (result.final_output or "")
    assert result.runs == [] and eng._node_runner.calls == []


def test_script_node_without_script_runner_is_config_error(tmp_path):
    wf = parse_workflow({"name": "t", "start": "s", "nodes": [
        {"id": "s", "kind": "script", "command": "true", "next": None},
    ]})
    eng = WorkflowEngine(node_runner=fake_agent_runner([]), workspace=str(tmp_path))
    with pytest.raises(WorkflowConfigError):
        eng.run(wf, "task")


def test_linear_script_failure_records_exit_code_in_trace(tmp_path):
    wf = parse_workflow({"name": "t", "start": "s", "nodes": [
        {"id": "s", "kind": "script", "command": "exit 9", "next": None},
    ]})
    result = engine_for(tmp_path, []).run(wf, "task")
    trace = {r.node_id: r for r in result.runs}
    assert trace["s"].status == "node_failed"
    assert trace["s"].exit_code == 9


def test_timeout_failure_records_exit_code_none_in_trace(tmp_path):
    wf = parse_workflow({"name": "t", "start": "s", "nodes": [
        {"id": "s", "kind": "script", "command": "sleep 5", "timeout": 1, "next": None},
    ]})
    result = engine_for(tmp_path, []).run(wf, "task")
    trace = {r.node_id: r for r in result.runs}
    assert trace["s"].status == "node_failed"
    assert trace["s"].exit_code is None


def test_resume_at_script_node_feeds_answers_via_stdin(tmp_path):
    # A multi-way script node that is itself the workflow's start: it re-runs on resume
    # (not a downstream node), so the same node must see the composed answers on stdin,
    # not its own prior stdout. `tee -a` records every pass's stdin into a file, and the
    # branch it takes depends only on whether "answers" has shown up in that stdin yet —
    # deterministic across the first pass (no needs_input/no answers) and the resumed
    # pass (the framed answers context build_resume_state composes).
    wf = parse_workflow({"name": "t", "start": "ask", "nodes": [
        {"id": "ask", "kind": "script",
         "command": 'tee -a seen.txt | grep -q answers && { echo done; echo OK; } '
                    '|| { echo need more; echo MISSING; }',
         "cases": {"MISSING": "__needs_input__", "OK": None}},
    ]})
    eng = engine_for(tmp_path, [])
    first = eng.run(wf, "build the report")
    assert first.status == "needs_input" and first.needs_input_node == "ask"

    manifest = run_log.read_manifest(tmp_path, "t", first.run_id)
    resume = build_resume_state(manifest, "the answers")
    resumed = eng.run(wf, "build the report", resume=resume)
    assert resumed.status == "completed"

    work_dir = artifact_dir(tmp_path, first.run_id, "work", None)
    seen = (work_dir / "seen.txt").read_text()
    assert "build the report" in seen      # first pass: task text arrived on stdin
    assert "the answers" in seen           # resumed pass: the framed answers arrived on stdin


def test_shared_buffer_passes_through_script(tmp_path):
    wf = parse_workflow({"name": "t", "start": "a", "nodes": [
        {"id": "a", "prompt": "first", "context": "shared", "next": "s"},
        {"id": "s", "kind": "script", "command": "cat", "next": "b"},
        {"id": "b", "prompt": "second", "context": "shared", "next": None},
    ]})
    from durin.workflow.engine import NodeRunResponse
    reqs = []

    def agent(req):
        reqs.append(req)
        return NodeRunResponse(output=f"out-{req.node.id}", session_key="s",
                               messages=[{"role": "user", "content": f"turn-{req.node.id}"},
                                         {"role": "assistant", "content": f"out-{req.node.id}"}])

    eng = WorkflowEngine(node_runner=agent, script_runner=ScriptNodeRunner(str(tmp_path)),
                         workspace=str(tmp_path))
    result = eng.run(wf, "task")
    assert result.status == "completed"
    # b (shared) still received a's turns — the script did not consume or reset the buffer
    assert any("turn-a" in str(m) for m in reqs[1].shared_context)


def test_cancel_kills_a_running_script_node(tmp_path):
    # The script writes its own pid to a file in its working folder (the engine's
    # output_dir for a script node) before sleeping — so the test can confirm the
    # subprocess actually died, not just that run() returned early.
    wf = parse_workflow({"name": "t", "start": "s", "nodes": [
        {"id": "s", "kind": "script", "command": "echo $$ > pid.txt; sleep 30",
         "timeout": 30, "next": None},
    ]})
    t0 = time.monotonic()

    def cancel_check():
        return time.monotonic() - t0 > 1.0

    eng = WorkflowEngine(node_runner=fake_agent_runner([]),
                         script_runner=ScriptNodeRunner(str(tmp_path)),
                         workspace=str(tmp_path),
                         cancel_check=cancel_check)
    result = eng.run(wf, "task")
    assert time.monotonic() - t0 < 10
    assert result.status == "cancelled"

    work_dir = artifact_dir(tmp_path, result.run_id, "work", None)
    pid = int((work_dir / "pid.txt").read_text().strip())
    deadline = time.time() + 5
    dead = False
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            dead = True
            break
        time.sleep(0.1)
    assert dead, "script subprocess is still alive after the cancel"


def _build_specs_gate_command() -> str:
    # Load the actual seed file (not a re-typed copy) so this test pins the real
    # gate command's behavior, not a paraphrase of it.
    path = pkg_files("durin") / "templates" / "workflows" / "build-specs.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    wf = parse_workflow(data)
    gate = wf.nodes["gate"]
    assert gate.kind == "script" and gate.on_fail == "assemble"
    return gate.command


def test_build_specs_seed_gate_passes_when_every_component_is_present(tmp_path):
    (tmp_path / "components.json").write_text(
        json.dumps(["auth-module", "rate-limiter"]), encoding="utf-8")
    node = parse_workflow({"name": "t", "start": "gate", "nodes": [
        {"id": "gate", "kind": "script", "command": _build_specs_gate_command(),
         "on_pass": None, "on_fail": "assemble"},
        {"id": "assemble", "prompt": "reassemble", "next": None},
    ]}).nodes["gate"]
    spec_text = "## Auth-Module\nHandles login.\n\n## Rate-Limiter\nThrottles requests."
    req = NodeRunRequest(node=node, task="task", upstream_output=spec_text, shared_context=[],
                         run_id="r1", iteration=1, root_session_key=None, output_dir=str(tmp_path))
    resp = ScriptNodeRunner(str(tmp_path))(req)
    assert resp.route_label == "PASS" and resp.exit_code == 0


def test_build_specs_seed_gate_fails_and_names_the_missing_component(tmp_path):
    (tmp_path / "components.json").write_text(
        json.dumps(["auth-module", "rate-limiter"]), encoding="utf-8")
    node = parse_workflow({"name": "t", "start": "gate", "nodes": [
        {"id": "gate", "kind": "script", "command": _build_specs_gate_command(),
         "on_pass": None, "on_fail": "assemble"},
        {"id": "assemble", "prompt": "reassemble", "next": None},
    ]}).nodes["gate"]
    spec_text = "## Auth-Module\nHandles login."   # rate-limiter section missing
    req = NodeRunRequest(node=node, task="task", upstream_output=spec_text, shared_context=[],
                         run_id="r1", iteration=1, root_session_key=None, output_dir=str(tmp_path))
    resp = ScriptNodeRunner(str(tmp_path))(req)
    assert resp.route_label == "FAIL" and resp.exit_code == 1
    assert "rate-limiter" in resp.output
