import pytest
from durin.loops import run_log as rl
from durin.loops.runtime import LoopBusy, LoopsRuntime
from durin.loops.spec import parse_loop
from durin.loops.store import save_loop
from durin.workflow.result import WorkflowResult


@pytest.fixture(autouse=True)
def _isolate_telemetry_dir(tmp_path, monkeypatch):
    """LoopsRuntime binds a session telemetry logger around fire/try_fire/answer
    (durin/loops/runtime.py) since those entrypoints run outside an agent turn's
    bound ContextVar. Without this, every test below would write real JSONL
    files to the developer's ~/.cache/durin/telemetry."""
    import durin.telemetry.logger as telemetry_logger

    telemetry_dir = tmp_path / "_telemetry"
    monkeypatch.setattr(telemetry_logger, "_DEFAULT_DIR", telemetry_dir)
    return telemetry_dir


def _mk_runtime(tmp_path, results, judge_verdict=None, asks=None):
    calls = {"exec": []}

    async def workflow_exec(name, task, *, resume_run_id=None):
        calls["exec"].append((name, task, resume_run_id))
        return results.pop(0)

    async def judge(intent, assertions, evidence):
        return judge_verdict or {"intent_met": True, "assertions": {a: True for a in assertions}}

    async def on_ask(loop, run_id, kind, text):
        (asks if asks is not None else []).append((loop, run_id, kind, text))

    ids = iter([f"lr{i}" for i in range(100)])
    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, on_operator_ask=on_ask, run_id_factory=lambda: next(ids))
    return rt, calls


def _save(tmp_path, **over):
    data = {"name": "l1", "workflow": "w1", "goal": {"intent": "it is done"}} | over
    save_loop(tmp_path, parse_loop(data))


def _wr(status, **kw):
    return WorkflowResult(status=status, final_output=kw.pop("out", "output"), run_id=kw.pop("run_id", "wf1"), **kw)


async def test_completed_and_goal_reached_is_done(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")])
    m = await rt.fire("l1", source="manual")
    assert m["status"] == "done" and m["goal_reached"] is True
    assert calls["exec"][0][0] == "w1"


async def test_completed_but_goal_not_reached_is_no_goal(tmp_path):
    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("completed")], judge_verdict={"intent_met": False, "assertions": {}})
    m = await rt.fire("l1", source="manual")
    assert m["status"] == "no_goal" and m["goal_reached"] is False


async def test_needs_input_becomes_needs_operator_and_notifies(tmp_path):
    _save(tmp_path)
    asks = []
    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="approve?", needs_input_node="gate")], asks=asks)
    m = await rt.fire("l1", source="cron")
    assert m["status"] == "needs_operator" and m["ask"] == "approve?"
    assert asks == [("l1", m["run_id"], "ask", f"[l1 · {m['run_id']}] approve?")]


async def test_answer_resumes_and_finishes(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [_wr("needs_input", out="approve?", needs_input_node="gate"), _wr("completed")])
    m = await rt.fire("l1", source="cron")
    m2 = await rt.answer("l1", m["run_id"], "yes, approved")
    assert m2["status"] == "done"
    assert calls["exec"][1] == ("w1", "yes, approved", "wf1")  # resumed same workflow run


async def test_single_concurrency_blocks_manual_and_skips_cron(tmp_path):
    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="q", needs_input_node="g")])
    await rt.fire("l1", source="manual")  # leaves an active needs_operator run
    with pytest.raises(LoopBusy):
        await rt.fire("l1", source="manual")
    assert await rt.try_fire("l1", source="cron") is None


async def test_parallel_concurrency_allows_second_run(tmp_path):
    _save(tmp_path, concurrency="parallel")
    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="q", needs_input_node="g"), _wr("completed")])
    await rt.fire("l1", source="manual")
    m2 = await rt.fire("l1", source="manual")
    assert m2["status"] == "done"


async def test_stuck_guard_escalates(tmp_path):
    _save(tmp_path, stuck_after=2)
    asks = []
    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted"), _wr("exhausted")], asks=asks)
    await rt.fire("l1", source="cron")
    m2 = await rt.fire("l1", source="cron")
    assert m2["status"] == "escalated"
    assert any(k == "escalation" for _, _, k, _ in asks)


async def test_disabled_loop_skips_cron_fire(tmp_path):
    _save(tmp_path, enabled=False)
    rt, calls = _mk_runtime(tmp_path, [])
    assert await rt.try_fire("l1", source="cron") is None
    assert calls["exec"] == []


class _RecordingTelemetry:
    """Minimal telemetry-sink double: records (event_type, data) pairs."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


async def test_fire_emits_telemetry_when_unbound(tmp_path, _isolate_telemetry_dir):
    """fire() runs outside an agent turn, so no logger is bound on entry.
    LoopsRuntime must bind its own session logger for the call so
    loops.fired / loops.run_finished actually land on disk."""
    import json

    from durin.telemetry.logger import current_telemetry

    telemetry_dir = _isolate_telemetry_dir
    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("completed")])

    assert current_telemetry() is None
    await rt.fire("l1", source="manual")
    assert current_telemetry() is None  # unbound again after the call returns

    files = list(telemetry_dir.glob("*.jsonl"))
    assert len(files) == 1
    event_types = [
        json.loads(line)["type"]
        for line in files[0].read_text(encoding="utf-8").strip().splitlines()
    ]
    assert "loops.fired" in event_types
    assert "loops.run_finished" in event_types


async def test_fire_reuses_already_bound_telemetry(tmp_path, _isolate_telemetry_dir):
    """When a logger is already bound (e.g. fire() invoked via the loops agent
    tool from inside a live agent turn), LoopsRuntime must not override it —
    events go to the caller's logger, and no separate loop-scoped file appears."""
    from durin.telemetry.logger import bind_telemetry, reset_telemetry

    telemetry_dir = _isolate_telemetry_dir
    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("completed")])

    fake = _RecordingTelemetry()
    token = bind_telemetry(fake)
    try:
        await rt.fire("l1", source="manual")
    finally:
        reset_telemetry(token)

    event_types = [event_type for event_type, _ in fake.events]
    assert "loops.fired" in event_types
    assert "loops.run_finished" in event_types
    assert not telemetry_dir.exists() or list(telemetry_dir.glob("*.jsonl")) == []


async def test_workflow_exec_exception_sets_detail_not_ask(tmp_path):
    _save(tmp_path)

    async def workflow_exec(name, task, *, resume_run_id=None):
        raise RuntimeError("boom")

    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {}}

    ids = iter([f"lr{i}" for i in range(100)])
    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, run_id_factory=lambda: next(ids))
    m = await rt.fire("l1", source="manual")
    assert m["status"] == "error"
    assert m.get("detail") == "boom"
    assert m.get("ask") is None


async def test_judge_exception_finalizes_as_error_not_stuck_running(tmp_path):
    """Amendment 1: a judge that raises must not strand the run as 'running'.

    It is finalized via the same failure path as a workflow_exec exception
    (status='error', goal_reached=False), and the stuck guard still counts
    it toward consecutive_no_goal — so active_runs is empty right after.
    """
    _save(tmp_path)
    calls = {"exec": []}

    async def workflow_exec(name, task, *, resume_run_id=None):
        calls["exec"].append((name, task, resume_run_id))
        return _wr("completed")

    async def judge(intent, assertions, evidence):
        raise RuntimeError("judge blew up")

    ids = iter([f"lr{i}" for i in range(100)])
    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, run_id_factory=lambda: next(ids))
    m = await rt.fire("l1", source="manual")
    assert m["status"] in ("error", "escalated")
    assert rl.active_runs(tmp_path, "l1") == []
