import os
import time

import pytest

from durin.workflow.engine import NodeExecutionError, NodeRunRequest
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import ScriptNode


def _req(node, upstream=None, tmp_path=None, iteration=1):
    return NodeRunRequest(
        node=node, task="the task", upstream_output=upstream, shared_context=[],
        run_id="r1", iteration=iteration, root_session_key=None,
        output_dir=str(tmp_path) if tmp_path else None,
    )


def runner(tmp_path, **kw):
    return ScriptNodeRunner(str(tmp_path), **kw)


def test_stdin_carries_upstream_and_stdout_is_output(tmp_path):
    node = ScriptNode(id="s", command="tr a-z A-Z")
    resp = runner(tmp_path)(_req(node, upstream="hello", tmp_path=tmp_path))
    assert resp.output.strip() == "HELLO"
    assert resp.exit_code == 0
    assert resp.session_key is None and not resp.persist_failed and resp.messages == []


def test_env_vars_and_cwd(tmp_path):
    work = tmp_path / "work"
    node = ScriptNode(id="mynode", command='echo "$DURIN_NODE_ID $DURIN_RUN_ID $DURIN_ITERATION"; pwd; echo "$DURIN_TASK"')
    resp = runner(tmp_path)(_req(node, tmp_path=work, iteration=2))
    lines = resp.output.strip().splitlines()
    assert lines[0] == "mynode r1 2"
    assert lines[1] == str(work)          # cwd = output_dir (created by the runner)
    assert lines[2] == "the task"


def test_binary_gate_exit_zero_is_pass(tmp_path):
    node = ScriptNode(id="g", command="echo checked; exit 0", on_pass=None, on_fail="g")
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.route_label == "PASS" and resp.exit_code == 0
    assert "checked" in resp.output


def test_binary_gate_nonzero_is_fail_with_stderr_feedback(tmp_path):
    node = ScriptNode(id="g", command="echo partial; echo boom >&2; exit 3", on_pass=None, on_fail="g")
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.route_label == "FAIL" and resp.exit_code == 3
    assert "partial" in resp.output and "boom" in resp.output and "exit code 3" in resp.output


def test_cases_label_from_last_stdout_line(tmp_path):
    node = ScriptNode(id="c", command="echo details; echo READY", cases={"READY": None, "MISSING": "c"})
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.route_label == "READY"


def test_cases_nonzero_exit_is_node_failure(tmp_path):
    node = ScriptNode(id="c", command="echo READY; exit 1", cases={"READY": None})
    with pytest.raises(NodeExecutionError) as exc:
        runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert exc.value.node_id == "c" and exc.value.session_key is None


def test_linear_nonzero_exit_is_node_failure(tmp_path):
    node = ScriptNode(id="t", command="exit 7", next=None)
    with pytest.raises(NodeExecutionError):
        runner(tmp_path)(_req(node, tmp_path=tmp_path))


def test_timeout_kills_process_group(tmp_path):
    node = ScriptNode(id="slow", command="sleep 30", timeout=1, next=None)
    t0 = time.time()
    with pytest.raises(NodeExecutionError, match="timed out"):
        runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert time.time() - t0 < 10


def test_stdout_capped_with_notice(tmp_path):
    node = ScriptNode(id="big", command="yes x | head -c 50000")
    resp = runner(tmp_path, max_output_chars=1000)(_req(node, tmp_path=tmp_path))
    assert len(resp.output) < 1200 and "truncated" in resp.output


def test_script_file_python(tmp_path):
    scripts = tmp_path / "workflows" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "up.py").write_text("import sys; print(sys.stdin.read().upper(), end='')\n")
    node = ScriptNode(id="s", script="up.py")
    resp = runner(tmp_path)(_req(node, upstream="abc", tmp_path=tmp_path))
    assert resp.output == "ABC"


def test_script_file_shell(tmp_path):
    scripts = tmp_path / "workflows" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "hi.sh").write_text("read line; echo \"got:$line\"\n")
    node = ScriptNode(id="s", script="hi.sh")
    resp = runner(tmp_path)(_req(node, upstream="x", tmp_path=tmp_path))
    assert resp.output.strip() == "got:x"


def test_missing_command_binary_is_node_failure(tmp_path):
    node = ScriptNode(id="s", command="definitely-not-a-binary-xyz")
    # bash -c returns 127 for a missing binary — a plain failure, not a crash.
    with pytest.raises(NodeExecutionError):
        runner(tmp_path)(_req(node, tmp_path=tmp_path))


def test_non_utf8_output_degrades_instead_of_raising(tmp_path):
    node = ScriptNode(id="s", command="printf '\\xff\\xfe ok'")
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.exit_code == 0 and "ok" in resp.output


def test_timeout_closes_pipes(tmp_path, monkeypatch):
    import subprocess as subprocess_mod

    # Capture the Popen instance the runner creates so we can inspect its
    # pipes after the timeout path runs — proves communicate() drained and
    # closed them instead of leaving them for the garbage collector.
    procs = []
    real_popen = subprocess_mod.Popen

    def spying_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(subprocess_mod, "Popen", spying_popen)

    node = ScriptNode(id="slow", command="sleep 30", timeout=1, next=None)
    with pytest.raises(NodeExecutionError, match="timed out"):
        runner(tmp_path)(_req(node, tmp_path=tmp_path))

    assert len(procs) == 1
    assert procs[0].stdout.closed
    assert procs[0].stderr.closed
