import pytest
from durin.loops import run_log as rl
from durin.loops.runtime import LoopBusy, LoopsRuntime
from durin.loops.spec import parse_loop
from durin.loops.store import save_loop
from durin.workflow.result import WorkflowResult


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
