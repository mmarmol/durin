"""A script node must leave a record of what it ran and printed.

Its stdout only ever travelled the edge to the next node and its stderr was
discarded, so a successful script was unreadable after the fact and a failing
one left only a stderr tail. The manifest now carries the command and both
streams, capped — and, critically, on the failure paths too, since the script a
user most wants to read is the one that broke.
"""

import json

from durin.workflow import run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.script_runner import ScriptNodeRunner
from durin.workflow.spec import parse_workflow


def _run(tmp_path, command, *, node_extra=None, log_max_chars=4000):
    workflow = parse_workflow({
        "name": "scripted", "start": "s",
        "nodes": [{"id": "s", "kind": "script", "command": command, "next": None,
                   **(node_extra or {})}],
    })
    engine = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(output=""),
        script_runner=ScriptNodeRunner(str(tmp_path), log_max_chars=log_max_chars),
        workspace=str(tmp_path), run_id_factory=lambda: "r1",
    )
    result = engine.run(workflow, "task")
    manifest = run_log.read_manifest(tmp_path, "scripted", "r1") or {}
    return result, manifest["runs"][0]


def test_successful_script_records_command_and_stdout(tmp_path):
    _, row = _run(tmp_path, "echo hello")
    assert row["command"] == "echo hello"
    assert "hello" in row["stdout"]
    assert row["exit_code"] == 0


def test_stderr_is_recorded_even_when_the_script_succeeds(tmp_path):
    _, row = _run(tmp_path, "echo out; echo warn >&2")
    assert "out" in row["stdout"]
    assert "warn" in row["stderr"]


def test_failing_script_records_both_streams(tmp_path):
    """The aborting path builds its NodeRun from the error, not from a response —
    the streams must ride along or the interesting case stays blind."""
    result, row = _run(tmp_path, "echo partial; echo boom >&2; exit 3")
    assert result.status == "aborted"
    assert row["status"] == "node_failed"
    assert row["exit_code"] == 3
    assert "partial" in row["stdout"]
    assert "boom" in row["stderr"]


def test_failing_gate_records_both_streams(tmp_path):
    """A binary gate's non-zero exit is a verdict, not a failure — it returns a
    response rather than raising, and must capture just the same."""
    workflow = parse_workflow({
        "name": "scripted", "start": "s",
        "nodes": [
            {"id": "s", "kind": "script", "command": "echo checked; echo why >&2; exit 1",
             "on_pass": None, "on_fail": "fix"},
            {"id": "fix", "kind": "work", "next": None},
        ],
    })
    engine = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(output="fixed"),
        script_runner=ScriptNodeRunner(str(tmp_path)),
        workspace=str(tmp_path), run_id_factory=lambda: "r1",
    )
    engine.run(workflow, "task")
    row = (run_log.read_manifest(tmp_path, "scripted", "r1") or {})["runs"][0]
    assert "checked" in row["stdout"]
    assert "why" in row["stderr"]
    assert row["exit_code"] == 1


def test_timed_out_script_keeps_what_it_printed_before_the_kill(tmp_path):
    _, row = _run(tmp_path, "echo before-hang; sleep 30", node_extra={"timeout": 1})
    assert row["status"] == "node_failed"
    assert "before-hang" in (row["stdout"] or "")


def test_streams_are_capped_with_a_notice(tmp_path):
    _, row = _run(tmp_path, "printf 'x%.0s' $(seq 1 5000)", log_max_chars=200)
    assert len(row["stdout"]) < 400
    assert "truncated" in row["stdout"]


def test_a_stored_secret_never_reaches_the_manifest(tmp_path, monkeypatch):
    """The manifest is a durable readable file: a credential landing there is a
    leak with a long half-life, so the capture must read the redacted streams."""
    from durin.workflow import script_runner as sr

    monkeypatch.setattr(sr, "redact_secrets", lambda text: text.replace("hunter2", "«redacted»"))
    _, row = _run(tmp_path, "echo token=hunter2; echo token=hunter2 >&2")
    raw = json.dumps(row)
    assert "hunter2" not in raw
    assert "«redacted»" in row["stdout"]
    assert "«redacted»" in row["stderr"]
    # The command line persists too, and is every bit as capable of carrying a
    # credential as the output is.
    assert "«redacted»" in row["command"]


def test_an_agent_node_records_no_script_fields(tmp_path):
    workflow = parse_workflow({
        "name": "scripted", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "next": None}],
    })
    engine = WorkflowEngine(node_runner=lambda req: NodeRunResponse(output="text"),
                            workspace=str(tmp_path), run_id_factory=lambda: "r1")
    engine.run(workflow, "task")
    row = (run_log.read_manifest(tmp_path, "scripted", "r1") or {})["runs"][0]
    assert row["command"] is None
    assert row["stdout"] is None
    assert row["stderr"] is None
