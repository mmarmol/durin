"""The shared background-task merge (consumed by the HTTP service AND the tasks tool)."""

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


def test_running_run_with_pending_cancel_shows_stopping(tmp_path, monkeypatch):
    """A running workflow whose cancel was requested surfaces as "stopping" so
    the UI acknowledges the stop while the engine winds down; without the
    pending cancel the same row stays "running"."""
    import durin.workflow.run_log as run_log
    from durin.workflow import cancellation

    monkeypatch.setattr(run_log, "runs_for_session", lambda ws, key: [
        {"run_id": "rstop", "workflow": "w", "status": "running",
         "started_at": 1.0, "finished_at": None, "runs": []},
    ])

    rows = collect_tasks(str(tmp_path), session_key="websocket:x")
    assert rows[0]["status"] == "running"

    cancellation.request_cancel("rstop")
    try:
        rows = collect_tasks(str(tmp_path), session_key="websocket:x")
        assert rows[0]["status"] == "stopping"
    finally:
        cancellation.clear("rstop")
