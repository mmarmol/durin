import time
import pytest
from durin.service.principal import Principal
from durin.service.tasks import TasksService, TasksListQuery


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


@pytest.mark.asyncio
async def test_merges_subagents_and_workflows_newest_first(tmp_path, monkeypatch):
    mono = time.monotonic()
    sub = _Status("t1", "research", "awaiting_tools", "subagent:t1", started_at=mono)
    svc = TasksService(workspace=tmp_path, subagent_manager=_FakeSubagents([sub]))

    # One workflow run manifest on disk for the same chat session.
    import durin.workflow.run_log as run_log
    monkeypatch.setattr(run_log, "runs_for_session", lambda ws, key: [
        {"run_id": "r9", "workflow": "qa", "status": "running",
         "started_at": time.time() + 5, "finished_at": None,
         "runs": [{"session_key": "workflow:r9:node1:1"}]},
    ])

    res = await svc.list(TasksListQuery(session="websocket:chatA"), Principal.local())
    assert [t.kind for t in res.tasks] == ["workflow", "subagent"]   # workflow started later → first
    sa = next(t for t in res.tasks if t.kind == "subagent")
    assert sa.id == "t1" and sa.status == "running" and sa.session_key == "subagent:t1"
    wf = next(t for t in res.tasks if t.kind == "workflow")
    assert wf.status == "running" and wf.session_key == "workflow:r9:node1:1"


@pytest.mark.asyncio
async def test_status_mapping_and_no_manager(tmp_path):
    svc = TasksService(workspace=tmp_path, subagent_manager=None)
    res = await svc.list(TasksListQuery(session="websocket:chatA"), Principal.local())
    assert res.tasks == []   # no manager, no manifests → empty, no crash
