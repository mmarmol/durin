"""The shared background-task merge (consumed by the HTTP service AND the tasks tool)."""

import json
import time

from durin.agent.background_tasks import collect_tasks


class _Status:
    def __init__(self, task_id, label, phase, session_key, started_at, ended_at=None):
        self.task_id = task_id
        self.label = label
        self.phase = phase
        self.session_key = session_key
        self.started_at = started_at
        self.ended_at = ended_at


class _FakeSubagents:
    def __init__(self, statuses):
        self._statuses = statuses

    def list_for_session(self, session_key):
        return list(self._statuses)


def test_collect_returns_dicts_with_stable_shape(tmp_path, monkeypatch):
    sub = _Status("t1", "research", "awaiting_tools", "subagent:t1", started_at=time.monotonic())

    import durin.workflow.run_log as run_log
    monkeypatch.setattr(run_log, "runs_for_session", lambda ws, key: [
        {"run_id": "r9", "workflow": "qa", "status": "cancelled",
         "started_at": time.time() + 5, "finished_at": time.time() + 6,
         "task": "do the qa", "runs": [{"session_key": "workflow:r9:n1:1", "node_id": "n1", "status": "ok"}]},
    ])

    rows = collect_tasks(str(tmp_path), subagent_manager=_FakeSubagents([sub]),
                         sessions=None, session_key="websocket:chatA")

    assert [r["kind"] for r in rows] == ["workflow", "subagent"]  # workflow started later → first
    wf = next(r for r in rows if r["kind"] == "workflow")
    assert wf["id"] == "r9"
    assert wf["status"] == "cancelled"          # the new terminal status surfaces
    assert wf["task"] == "do the qa"
    assert wf["nodes"] and wf["nodes"][0]["id"] == "n1"
    sa = next(r for r in rows if r["kind"] == "subagent")
    assert sa["id"] == "t1" and sa["status"] == "running" and sa["nodes"] is None


def test_collect_empty_without_sources(tmp_path):
    assert collect_tasks(str(tmp_path), subagent_manager=None, sessions=None,
                         session_key="websocket:chatA") == []


def test_needs_input_run_carries_the_questions(tmp_path, monkeypatch):
    import durin.workflow.run_log as run_log
    monkeypatch.setattr(run_log, "runs_for_session", lambda ws, key: [
        {"run_id": "r1", "workflow": "w", "status": "needs_input",
         "started_at": time.time(), "finished_at": time.time() + 1,
         "task": "do it", "final_output": "Which env — staging or prod?", "runs": []},
    ])

    rows = collect_tasks(str(tmp_path), subagent_manager=None, sessions=None,
                         session_key="websocket:chatA")

    wf = next(r for r in rows if r["id"] == "r1")
    assert wf["needs_input_detail"] == "Which env — staging or prod?"


def test_non_needs_input_run_has_no_questions(tmp_path, monkeypatch):
    import durin.workflow.run_log as run_log
    monkeypatch.setattr(run_log, "runs_for_session", lambda ws, key: [
        {"run_id": "r2", "workflow": "w", "status": "completed",
         "started_at": time.time(), "finished_at": time.time() + 1,
         "task": "do it", "final_output": "the answer", "runs": []},
    ])

    rows = collect_tasks(str(tmp_path), subagent_manager=None, sessions=None,
                         session_key="websocket:chatA")

    wf = next(r for r in rows if r["id"] == "r2")
    assert wf["needs_input_detail"] is None


def test_needs_input_questions_capped_at_500_chars(tmp_path, monkeypatch):
    import durin.workflow.run_log as run_log
    long_questions = "q" * 600
    monkeypatch.setattr(run_log, "runs_for_session", lambda ws, key: [
        {"run_id": "r3", "workflow": "w", "status": "needs_input",
         "started_at": time.time(), "finished_at": time.time() + 1,
         "task": "do it", "final_output": long_questions, "runs": []},
    ])

    rows = collect_tasks(str(tmp_path), subagent_manager=None, sessions=None,
                         session_key="websocket:chatA")

    wf = next(r for r in rows if r["id"] == "r3")
    assert wf["needs_input_detail"] == "q" * 500


def test_a_running_node_appears_in_the_node_tree(tmp_path):
    """Reloading mid-node must still show which node is in flight — the live WS
    frames are gone after a reload, so this polled path is the only source."""
    from durin.agent.background_tasks import collect_tasks
    from durin.workflow import run_log

    run_log.start_run(tmp_path, "wf", "r1", root_session_key="websocket:c", started_at=100.0)
    run_log.mark_node_started(tmp_path, "wf", "r1", node_id="consolidate",
                              label="Consolidate", started_at=140.0)

    row = next(t for t in collect_tasks(tmp_path, session_key="websocket:c") if t["kind"] == "workflow")
    running = [n for n in row["nodes"] if n["status"] == "running"]
    assert [n["id"] for n in running] == ["consolidate"]
    assert running[0]["started_at"] == 140.0
    assert running[0]["label"] == "Consolidate"


def test_finished_nodes_carry_duration_artifacts_and_typical(tmp_path):
    from durin.agent.background_tasks import collect_tasks
    from durin.workflow import run_log

    run_log.start_run(tmp_path, "wf", "r2", root_session_key="websocket:c",
                      started_at=100.0, typical_s={"consolidate": 360.0})
    path = run_log._record_path(tmp_path, "wf", "r2")
    rec = run_log.read_manifest(tmp_path, "wf", "r2")
    rec["runs"] = [{"node_id": "consolidate", "iteration": 1, "status": "ok",
                    "duration_s": 361.5, "artifacts": ["context.json"]}]
    path.write_text(json.dumps(rec), encoding="utf-8")

    row = next(t for t in collect_tasks(tmp_path, session_key="websocket:c")
               if t["kind"] == "workflow")
    node = next(n for n in row["nodes"] if n["id"] == "consolidate")
    assert node["duration_s"] == 361.5
    assert node["artifacts"] == ["context.json"]
    assert node["typical_s"] == 360.0
    assert row["typical_total_s"] == 360.0


def test_typical_total_is_absent_without_history(tmp_path):
    """A first-ever run must show no estimate rather than an estimate of zero."""
    from durin.agent.background_tasks import collect_tasks
    from durin.workflow import run_log

    run_log.start_run(tmp_path, "wf", "r3", root_session_key="websocket:c", started_at=100.0)
    row = next(t for t in collect_tasks(tmp_path, session_key="websocket:c")
               if t["kind"] == "workflow")
    assert row["typical_total_s"] is None


def test_running_revisit_of_a_completed_node_collapses_to_one_row(tmp_path):
    """A looping workflow can revisit a node that already completed at least once.
    The manifest's active_node then names an id already present in the completed
    ``runs`` rows — the merged tree must still show exactly ONE row for that id,
    now reporting it as running, never two rows for the same node."""
    from durin.agent.background_tasks import collect_tasks
    from durin.workflow import run_log

    run_log.start_run(tmp_path, "wf", "r4", root_session_key="websocket:c", started_at=100.0)
    path = run_log._record_path(tmp_path, "wf", "r4")
    rec = run_log.read_manifest(tmp_path, "wf", "r4")
    rec["runs"] = [{"node_id": "search", "iteration": 1, "status": "ok",
                    "duration_s": 12.0, "artifacts": ["hits.json"]}]
    path.write_text(json.dumps(rec), encoding="utf-8")
    run_log.mark_node_started(tmp_path, "wf", "r4", node_id="search",
                              label="Search", started_at=250.0)

    row = next(t for t in collect_tasks(tmp_path, session_key="websocket:c")
               if t["kind"] == "workflow")
    matches = [n for n in row["nodes"] if n["id"] == "search"]
    assert len(matches) == 1
    assert matches[0]["status"] == "running"
    assert matches[0]["started_at"] == 250.0


def test_a_crashed_run_reports_no_running_node(tmp_path):
    """Crash reconciliation (reconcile_one / reconcile_running) flips a dead run's
    status to "crashed" but does not touch active_node — only a normal completion
    (update_run) clears that marker. Without a status gate at the reader, a run
    that died mid-node would report that node as "running" forever."""
    from durin.agent.background_tasks import collect_tasks
    from durin.workflow import run_log

    run_log.start_run(tmp_path, "wf", "r5", root_session_key="websocket:c", started_at=100.0)
    run_log.mark_node_started(tmp_path, "wf", "r5", node_id="search",
                              label="Search", started_at=140.0)
    # Simulate the owning process having died mid-node, then run the real
    # crash-reconciliation path against it — the same one the gateway runs at
    # boot and the one the tasks tool runs when a user pokes a stale run.
    path = run_log._record_path(tmp_path, "wf", "r5")
    rec = run_log.read_manifest(tmp_path, "wf", "r5")
    rec["owner"] = {"pid": 2**22 + 54321, "started": "never"}
    path.write_text(json.dumps(rec), encoding="utf-8")
    assert run_log.reconcile_one(tmp_path, "wf", "r5") is True

    manifest = run_log.read_manifest(tmp_path, "wf", "r5")
    assert manifest["status"] == "crashed"
    assert manifest["active_node"]["node_id"] == "search"   # reconcile leaves it set

    row = next(t for t in collect_tasks(tmp_path, session_key="websocket:c")
               if t["kind"] == "workflow")
    assert row["status"] == "failed"
    assert not any(n["status"] == "running" for n in row["nodes"])
