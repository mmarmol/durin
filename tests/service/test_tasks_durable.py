import pytest
from durin.service.principal import Principal
from durin.service.tasks import TasksService, TasksListQuery


class _Sessions:
    def children_of(self, parent):
        return [
            {"key": "subagent:t9", "origin_type": "subagent", "origin_id": "t9",
             "created_at": "2026-06-27T01:00:00", "path": "/x", "title": "subagent: research"},
        ]


@pytest.mark.asyncio
async def test_durable_history_from_children_when_lru_empty(tmp_path):
    # No subagent_manager (simulates post-restart empty LRU); sessions provides lineage.
    svc = TasksService(workspace=tmp_path, subagent_manager=None, sessions=_Sessions())
    res = await svc.list(TasksListQuery(session="websocket:chatA"), Principal.local())
    sub = [t for t in res.tasks if t.kind == "subagent"]
    assert len(sub) == 1
    assert sub[0].id == "t9" and sub[0].status == "done"
    assert "research" in sub[0].label


@pytest.mark.asyncio
async def test_lru_running_takes_precedence_over_durable(tmp_path):
    # If a task is both in the LRU (running) and persisted, the LRU entry wins (no dup).
    class _Status:
        task_id="t9"; label="research"; phase="awaiting_tools"; session_key="subagent:t9"
        started_at=0.0; ended_at=None
    class _Mgr:
        def list_for_session(self, s): return [_Status()]
    svc = TasksService(workspace=tmp_path, subagent_manager=_Mgr(), sessions=_Sessions())
    res = await svc.list(TasksListQuery(session="websocket:chatA"), Principal.local())
    sub = [t for t in res.tasks if t.kind == "subagent"]
    assert len(sub) == 1 and sub[0].status == "running"  # LRU wins
