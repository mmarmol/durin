import os
import time

import pytest

from durin.workflow.engine import NodeExecutionError, NodeRunRequest, ScriptCancelled
from durin.workflow.script_runner import _MAX_TASK_ENV_CHARS, ScriptNodeRunner
from durin.workflow.spec import ScriptNode


def _req(node, upstream=None, tmp_path=None, iteration=1, cancel_check=None):
    return NodeRunRequest(
        node=node, task="the task", upstream_output=upstream, shared_context=[],
        run_id="r1", iteration=iteration, root_session_key=None,
        output_dir=str(tmp_path) if tmp_path else None,
        cancel_check=cancel_check,
    )


def runner(tmp_path, **kw):
    return ScriptNodeRunner(str(tmp_path), **kw)


def test_stdin_carries_upstream_and_stdout_is_output(tmp_path):
    node = ScriptNode(id="s", command="tr a-z A-Z")
    resp = runner(tmp_path)(_req(node, upstream="hello", tmp_path=tmp_path))
    assert resp.output.strip() == "HELLO"
    assert resp.exit_code == 0
    assert resp.session_key is None and not resp.persist_failed and resp.messages == []


def test_stdin_falls_back_to_task_at_chain_start(tmp_path):
    node = ScriptNode(id="s", command="tr a-z A-Z")
    resp = runner(tmp_path)(_req(node, upstream=None, tmp_path=tmp_path))
    assert resp.output.strip() == "THE TASK"


def test_empty_upstream_output_stays_empty_on_stdin(tmp_path):
    node = ScriptNode(id="s", command="cat")
    resp = runner(tmp_path)(_req(node, upstream="", tmp_path=tmp_path))
    assert resp.output == ""


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


def test_cancel_check_kills_running_process_before_timeout(tmp_path):
    # timeout is generous (30s) so only the cancel flag — not the deadline — can
    # end this run; the flag flips true after ~0.5-1s, well inside the timeout.
    node = ScriptNode(id="slow", command="sleep 30", timeout=30, next=None)
    t0 = time.monotonic()

    def cancel_check():
        return time.monotonic() - t0 > 0.5

    with pytest.raises(NodeExecutionError) as exc:
        runner(tmp_path)(_req(node, tmp_path=tmp_path, cancel_check=cancel_check))
    assert isinstance(exc.value.cause, ScriptCancelled)
    assert time.monotonic() - t0 < 10


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


def test_task_env_var_capped_at_max_chars(tmp_path):
    node = ScriptNode(id="s", command='echo -n "${#DURIN_TASK}"')
    req = NodeRunRequest(
        node=node, task="x" * (_MAX_TASK_ENV_CHARS + 500), upstream_output=None,
        shared_context=[], run_id="r1", iteration=1, root_session_key=None,
        output_dir=str(tmp_path),
    )
    resp = runner(tmp_path)(req)
    assert resp.output.strip() == str(_MAX_TASK_ENV_CHARS)


def test_work_dir_env_var_equals_output_dir(tmp_path):
    work = tmp_path / "work"
    node = ScriptNode(id="s", command='echo -n "$DURIN_WORK_DIR"')
    resp = runner(tmp_path)(_req(node, tmp_path=work))
    assert resp.output == str(work)


def test_clean_env_hides_ambient_var_but_keeps_path_and_durin_vars(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_TEST_SENTINEL", "leaked")
    node = ScriptNode(
        id="mynode", command='echo "sentinel=${DURIN_TEST_SENTINEL:-unset}"; '
                              'echo "path=${PATH:+set}"; echo "node=$DURIN_NODE_ID"',
        env="clean",
    )
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    lines = resp.output.strip().splitlines()
    assert lines[0] == "sentinel=unset"
    assert lines[1] == "path=set"
    assert lines[2] == "node=mynode"


def test_inherit_env_exposes_ambient_var(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_TEST_SENTINEL", "visible")
    node = ScriptNode(id="s", command='echo -n "$DURIN_TEST_SENTINEL"', env="inherit")
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.output == "visible"


def test_timeout_kill_reaps_grandchild(tmp_path):
    # The child backgrounds a grandchild that would otherwise outlive the timeout kill
    # unless the process GROUP (not just the direct child) is signalled. It writes the
    # grandchild's pid to a file in cwd first so the test can check on it after the kill.
    node = ScriptNode(id="slow", command="sleep 60 & echo $! > child.pid; wait", timeout=1, next=None)
    with pytest.raises(NodeExecutionError, match="timed out"):
        runner(tmp_path)(_req(node, tmp_path=tmp_path))

    pid_file = tmp_path / "child.pid"
    assert pid_file.exists()
    grandchild_pid = int(pid_file.read_text().strip())

    deadline = time.time() + 5
    dead = False
    while time.time() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            dead = True
            break
        time.sleep(0.1)
    assert dead, "grandchild process is still alive after the timeout kill"


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


# ---------------------------------------------------------------------------
# Declared secrets (node.secrets): injection, failure modes, redaction
# ---------------------------------------------------------------------------

class _FakeEntry:
    def __init__(self, value, scope):
        self.value, self.scope = value, scope


def _store_with(monkeypatch, entries):
    """Point the runner (and the redactor) at a fake store: {name: (value, scope)}."""
    class _Store:
        def all(self):
            return {n: _FakeEntry(v, sc) for n, (v, sc) in entries.items()}

    monkeypatch.setattr("durin.security.secrets.get_secret_store", lambda **kw: _Store())
    monkeypatch.setattr("durin.workflow.script_runner.get_secret_store", lambda **kw: _Store())


def test_declared_secret_injected(monkeypatch, tmp_path):
    _store_with(monkeypatch, {"MY_TOKEN": ("tok-value-12345", ["exec"])})
    node = ScriptNode(id="s", command='printf "%s" "${MY_TOKEN:+set}"', secrets=("MY_TOKEN",))
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.output == "set"


def test_undeclared_secret_not_injected(monkeypatch, tmp_path):
    _store_with(monkeypatch, {"MY_TOKEN": ("tok-value-12345", ["exec"])})
    node = ScriptNode(id="s", command='printf "%s" "${MY_TOKEN:-absent}"')
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.output == "absent"


def test_missing_secret_is_node_failure(monkeypatch, tmp_path):
    _store_with(monkeypatch, {})
    node = ScriptNode(id="s", command="true", secrets=("MY_TOKEN",))
    with pytest.raises(NodeExecutionError, match="MY_TOKEN"):
        runner(tmp_path)(_req(node, tmp_path=tmp_path))


def test_scope_denied_secret_is_node_failure(monkeypatch, tmp_path):
    _store_with(monkeypatch, {"MY_TOKEN": ("tok-value-12345", ["skill:deploy"])})
    node = ScriptNode(id="s", command="true", secrets=("MY_TOKEN",))
    with pytest.raises(NodeExecutionError, match="scope"):
        runner(tmp_path)(_req(node, tmp_path=tmp_path))


def test_stdout_redacts_secret_values(monkeypatch, tmp_path):
    _store_with(monkeypatch, {"MY_TOKEN": ("tok-value-12345", ["exec"])})
    node = ScriptNode(id="s", command='printf "%s" "$MY_TOKEN"', secrets=("MY_TOKEN",))
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert "tok-value-12345" not in resp.output
    assert "MY_TOKEN" in resp.output          # the «redacted:NAME» marker names it


def test_stderr_feedback_redacts_secret_values(monkeypatch, tmp_path):
    _store_with(monkeypatch, {"MY_TOKEN": ("tok-value-12345", ["exec"])})
    node = ScriptNode(id="g", command='echo "leak $MY_TOKEN" >&2; exit 1',
                      secrets=("MY_TOKEN",), on_pass=None, on_fail="g")
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.route_label == "FAIL"
    assert "tok-value-12345" not in resp.output


def test_secret_cannot_shadow_durin_metadata(monkeypatch, tmp_path):
    _store_with(monkeypatch, {"DURIN_NODE_ID": ("evil", ["exec"])})
    node = ScriptNode(id="s", command='printf "%s" "$DURIN_NODE_ID"', secrets=("DURIN_NODE_ID",))
    resp = runner(tmp_path)(_req(node, tmp_path=tmp_path))
    assert resp.output == "s"                 # DURIN_* metadata wins over a declared secret
