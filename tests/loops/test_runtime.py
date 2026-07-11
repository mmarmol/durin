import asyncio
import time

import pytest
from durin.loops import claims
from durin.loops import queue as loop_queue
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


def _mk_runtime(tmp_path, results, judge_verdict=None, asks=None, counterpart_asks=None, queue_ttl_s=3600):
    calls = {"exec": []}

    async def workflow_exec(name, task, *, resume_run_id=None):
        calls["exec"].append((name, task, resume_run_id))
        return results.pop(0)

    async def judge(intent, assertions, evidence):
        return judge_verdict or {"intent_met": True, "assertions": {a: True for a in assertions}}

    async def on_ask(loop, run_id, kind, text):
        (asks if asks is not None else []).append((loop, run_id, kind, text))

    async def on_counterpart_ask(loop, run_id, origin, text):
        (counterpart_asks if counterpart_asks is not None else []).append((loop, run_id, origin, text))

    ids = iter([f"lr{i}" for i in range(100)])
    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, on_operator_ask=on_ask, on_counterpart_ask=on_counterpart_ask,
                      run_id_factory=lambda: next(ids), queue_ttl_s=queue_ttl_s)
    return rt, calls


async def _drain():
    """Let a `_post_finish`-scheduled asyncio.create_task drain fire run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


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


async def test_counterpart_ask_with_thread_origin_becomes_waiting_info(tmp_path):
    _save(tmp_path)
    counterpart_asks = []
    rt, _ = _mk_runtime(
        tmp_path,
        [_wr("needs_input", out="[TO:counterpart] confirm the invoice?", needs_input_node="gate")],
        counterpart_asks=counterpart_asks,
    )
    origin = {"thread": "thread-123"}
    m = await rt.fire("l1", source="channel", origin=origin)
    assert m["status"] == "waiting_info"
    assert m["ask"] == "confirm the invoice?"

    claim = claims.lookup(tmp_path, "thread-123")
    assert claim is not None
    assert claim["loop"] == "l1"
    assert claim["run_id"] == m["run_id"]

    assert counterpart_asks == [("l1", m["run_id"], origin, "confirm the invoice?")]


async def test_counterpart_ask_without_origin_degrades_to_needs_operator(tmp_path):
    _save(tmp_path)
    asks = []
    rt, _ = _mk_runtime(
        tmp_path,
        [_wr("needs_input", out="[TO:counterpart] confirm the invoice?", needs_input_node="gate")],
        asks=asks,
    )
    m = await rt.fire("l1", source="cron")
    assert m["status"] == "needs_operator"
    assert m["ask"] == "confirm the invoice? (counterpart channel unavailable — answer here)"
    assert asks == [("l1", m["run_id"], "ask", f"[l1 · {m['run_id']}] {m['ask']}")]


async def test_untagged_ask_with_origin_is_still_needs_operator(tmp_path):
    """No [TO:counterpart] tag => operator-bound even when origin has a thread
    (V1 behavior must not change just because a thread happens to be present)."""
    _save(tmp_path)
    asks = []
    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="approve?", needs_input_node="gate")], asks=asks)
    origin = {"thread": "thread-xyz"}
    m = await rt.fire("l1", source="channel", origin=origin)
    assert m["status"] == "needs_operator" and m["ask"] == "approve?"
    assert claims.lookup(tmp_path, "thread-xyz") is None


async def test_answer_on_waiting_info_resumes_and_releases_claim(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [
        _wr("needs_input", out="[TO:counterpart] confirm?", needs_input_node="gate"),
        _wr("completed"),
    ])
    origin = {"thread": "thread-abc"}
    m = await rt.fire("l1", source="channel", origin=origin)
    assert m["status"] == "waiting_info"
    assert claims.lookup(tmp_path, "thread-abc") is not None

    m2 = await rt.answer("l1", m["run_id"], "yes, confirmed")
    assert m2["status"] == "done"
    assert calls["exec"][1] == ("w1", "yes, confirmed", "wf1")
    assert claims.lookup(tmp_path, "thread-abc") is None


async def test_reask_after_answer_keeps_fresh_claim(tmp_path):
    """A tagged re-ask on the same thread after an answer must register a
    fresh claim that survives — the pre-resume release (finding 1 fix) must
    not wipe it via a trailing unconditional finally."""
    _save(tmp_path)
    counterpart_asks = []
    rt, calls = _mk_runtime(
        tmp_path,
        [
            _wr("needs_input", out="[TO:counterpart] confirm the invoice?", needs_input_node="gate"),
            _wr("needs_input", out="[TO:counterpart] what's the PO number?", needs_input_node="gate2", run_id="wf1"),
        ],
        counterpart_asks=counterpart_asks,
    )
    origin = {"thread": "thread-reask"}
    m = await rt.fire("l1", source="channel", origin=origin)
    assert m["status"] == "waiting_info"
    assert claims.lookup(tmp_path, "thread-reask")["run_id"] == m["run_id"]

    m2 = await rt.answer("l1", m["run_id"], "confirmed")
    assert m2["status"] == "waiting_info"
    assert calls["exec"][1] == ("w1", "confirmed", "wf1")

    claim = claims.lookup(tmp_path, "thread-reask")
    assert claim is not None
    assert claim["loop"] == "l1"
    assert claim["run_id"] == m2["run_id"] == m["run_id"]


async def test_answer_releases_claim_even_when_resumed_exec_raises(tmp_path):
    _save(tmp_path)
    results = [_wr("needs_input", out="[TO:counterpart] confirm?", needs_input_node="gate")]
    calls = {"exec": []}

    async def workflow_exec(name, task, *, resume_run_id=None):
        calls["exec"].append((name, task, resume_run_id))
        if results:
            return results.pop(0)
        raise RuntimeError("boom")

    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {}}

    ids = iter([f"lr{i}" for i in range(100)])
    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, run_id_factory=lambda: next(ids))
    origin = {"thread": "thread-boom"}
    m = await rt.fire("l1", source="channel", origin=origin)
    assert m["status"] == "waiting_info"

    m2 = await rt.answer("l1", m["run_id"], "go")
    assert m2["status"] == "error"
    assert m2["detail"] == "boom"
    assert claims.lookup(tmp_path, "thread-boom") is None


async def test_post_finish_drains_next_fresh_queued_event(tmp_path):
    """single-concurrency loop: a run finishing (freeing the slot) must fire
    the oldest fresh queued event next, via a scheduled asyncio task."""
    _save(tmp_path)
    origin = {"channel": "email", "thread": "t1"}
    loop_queue.push(tmp_path, "l1", {"content": "queued task", "origin": origin})
    rt, calls = _mk_runtime(tmp_path, [_wr("completed"), _wr("completed")])

    m = await rt.fire("l1", source="manual")
    assert m["status"] == "done"
    await _drain()

    assert len(calls["exec"]) == 2
    assert calls["exec"][1] == ("w1", "queued task", None)
    assert loop_queue.pending(tmp_path, "l1") == 0


async def test_post_finish_skips_drain_when_queue_only_has_expired_events(tmp_path):
    _save(tmp_path)
    origin = {"channel": "email"}
    loop_queue.push(tmp_path, "l1", {"content": "stale task", "origin": origin,
                                     "queued_at": time.time() - 3600})
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")], queue_ttl_s=60)

    m = await rt.fire("l1", source="manual")
    assert m["status"] == "done"
    await _drain()

    assert len(calls["exec"]) == 1  # no second (drained) fire
    assert loop_queue.pending(tmp_path, "l1") == 0  # expired entry still dropped


async def test_post_finish_skips_drain_when_loop_disabled(tmp_path):
    _save(tmp_path, enabled=False)
    origin = {"channel": "email"}
    loop_queue.push(tmp_path, "l1", {"content": "queued task", "origin": origin})
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")])

    # fire() itself doesn't gate on `enabled` (only try_fire does); the drain
    # hook inside _post_finish is what must respect it.
    m = await rt.fire("l1", source="manual")
    assert m["status"] == "done"
    await _drain()

    assert len(calls["exec"]) == 1
    assert loop_queue.pending(tmp_path, "l1") == 1  # event left untouched


async def test_post_finish_skips_drain_for_parallel_concurrency(tmp_path):
    _save(tmp_path, concurrency="parallel")
    origin = {"channel": "email"}
    loop_queue.push(tmp_path, "l1", {"content": "queued task", "origin": origin})
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")])

    m = await rt.fire("l1", source="manual")
    assert m["status"] == "done"
    await _drain()

    assert len(calls["exec"]) == 1
    assert loop_queue.pending(tmp_path, "l1") == 1  # event left untouched
