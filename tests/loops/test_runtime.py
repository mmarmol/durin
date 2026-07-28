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


def _mk_runtime(tmp_path, results, judge_verdict=None, asks=None,
                counterpart_asks=None, queue_ttl_s=3600, on_outcome=None,
                is_shutting_down=None):
    calls = {"exec": []}

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None):
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
                      check_timeout_s=5, on_operator_ask=on_ask,
                      on_counterpart_ask=on_counterpart_ask,
                      run_id_factory=lambda: next(ids), queue_ttl_s=queue_ttl_s,
                      on_outcome=on_outcome, is_shutting_down=is_shutting_down)
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


def _wr_for(status):
    return WorkflowResult(status=status, final_output="out", run_id="wf1")


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


async def test_stuck_guard_escalation_outcome_reports_escalated(tmp_path):
    """_post_finish rewrites the record to `escalated` before emitting the
    outcome, so the outcome for the run that crosses stuck_after must carry
    that rewritten status rather than the raw `no_goal` the run finished
    with. Guards against `_emit_outcome` ever being moved ahead of the
    escalation rewrite, which would silently ship a stale status."""
    _save(tmp_path, stuck_after=2)
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted"), _wr("exhausted")], on_outcome=on_outcome)
    await rt.fire("l1", source="cron")
    await rt.fire("l1", source="cron")

    assert [o.status for o in outcomes] == ["no_goal", "escalated"]


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

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None):
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

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None):
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


async def test_counterpart_ask_with_channel_origin_becomes_waiting_info(tmp_path):
    """Full channel-shaped origin (as the matcher now builds it, with a
    channel-appropriate thread key and captured reply pieces) registers a
    claim under origin['thread'] and hands the origin through to the
    counterpart callback intact — the runtime never inspects channel/reply,
    only thread."""
    _save(tmp_path)
    counterpart_asks = []
    rt, _ = _mk_runtime(
        tmp_path,
        [_wr("needs_input", out="[TO:counterpart] confirm the invoice?", needs_input_node="gate")],
        counterpart_asks=counterpart_asks,
    )
    origin = {
        "channel": "telegram",
        "sender": "user1",
        "chat_id": "123",
        "thread": "telegram:123:topic:7",
        "subject": None,
        "reply": {"message_thread_id": 7, "message_id": 42},
    }
    m = await rt.fire("l1", source="channel", origin=origin)
    assert m["status"] == "waiting_info"

    claim = claims.lookup(tmp_path, "telegram:123:topic:7")
    assert claim is not None
    assert claim["loop"] == "l1"
    assert claim["run_id"] == m["run_id"]

    assert counterpart_asks == [("l1", m["run_id"], origin, "confirm the invoice?")]


async def test_counterpart_ask_with_custom_correlate_key_embeds_own_loop_name(tmp_path):
    """A custom correlate key (as the matcher derives it: custom:<loop>:<x>)
    always embeds the SAME loop name as the one registering the claim — the
    matcher only ever derives a custom key while iterating that loop's own
    triggers, so the loop firing here and the loop named in the key can never
    diverge. Uses a non-default loop name to prove this isn't hardcoded."""
    _save(tmp_path, name="ticket-loop")
    rt, _ = _mk_runtime(
        tmp_path,
        [_wr("needs_input", out="[TO:counterpart] which ticket?", needs_input_node="gate")],
    )
    origin = {"channel": "email", "thread": "custom:ticket-loop:TCK-9001", "reply": {"thread": "t1"}}
    m = await rt.fire("ticket-loop", source="channel", origin=origin)
    assert m["status"] == "waiting_info"

    claim = claims.lookup(tmp_path, "custom:ticket-loop:TCK-9001")
    assert claim is not None
    # The loop name embedded in the custom key equals the registering loop.
    key_loop_segment = "custom:ticket-loop:TCK-9001".split(":")[1]
    assert claim["loop"] == key_loop_segment == "ticket-loop"


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

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None):
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


async def test_post_finish_drain_re_queues_on_loop_busy(tmp_path, monkeypatch):
    """When the drained fire raises LoopBusy (loop became busy between
    pop_fresh and the fire attempt), the event must be re-enqueued."""
    _save(tmp_path)
    origin = {"channel": "email", "thread": "t1"}
    loop_queue.push(tmp_path, "l1", {"content": "drained task", "origin": origin})
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")])

    # Patch fire to raise LoopBusy on the drained (second) call.
    original_fire = rt.fire
    fire_calls = [0]

    async def fire_with_busy(*args, **kwargs):
        fire_calls[0] += 1
        if fire_calls[0] == 2:  # second call is the drain
            raise LoopBusy("loop is busy")
        return await original_fire(*args, **kwargs)

    monkeypatch.setattr(rt, "fire", fire_with_busy)

    m = await rt.fire("l1", source="manual")
    assert m["status"] == "done"
    await _drain()

    # Verify: the first (manual) fire executed, but the drained fire was re-queued
    assert len(calls["exec"]) == 1  # only the first fire executed
    assert loop_queue.pending(tmp_path, "l1") == 1  # event back in the queue
    # Verify the re-queued event has the same content and origin
    requeued = loop_queue.pop_fresh(tmp_path, "l1", 3600)
    assert requeued["content"] == "drained task"
    assert requeued["origin"] == origin


async def test_fire_records_the_workflow_run_id_before_the_workflow_finishes(tmp_path):
    """The manifest must name the workflow run while it is still in flight —
    a crash sweep reading it later is the only way to tell whether any work
    had started."""
    _save(tmp_path)
    seen = {}

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None):
        # Mid-flight: read back what the loop already persisted.
        seen["mid_flight"] = rl.read_run(tmp_path, "l1", "lr0").get("workflow_run_id")
        seen["passed"] = run_id
        return WorkflowResult(status="completed", final_output="out", run_id=run_id)

    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {}}

    ids = iter([f"lr{i}" for i in range(10)])
    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, run_id_factory=lambda: next(ids))
    await rt.fire("l1", source="chat")

    assert seen["passed"] is not None
    assert seen["mid_flight"] == seen["passed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("wf_status,expected", [
    ("completed", "done"),
    ("exhausted", "no_goal"),
    ("aborted", "error"),
])
async def test_every_terminal_status_emits_exactly_one_outcome(tmp_path, wf_status, expected):
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr_for(wf_status)], on_outcome=on_outcome)
    _save(tmp_path)
    await rt.fire("l1", source="cron")

    assert [o.status for o in outcomes] == [expected]


@pytest.mark.asyncio
async def test_needs_operator_emits_no_outcome(tmp_path):
    """A pause is not a terminal state — the existing ask path owns it."""
    outcomes, asks = [], []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [WorkflowResult(status="needs_input",
                                                  final_output="which one?", run_id="wf1")],
                        asks=asks, on_outcome=on_outcome)
    _save(tmp_path)
    await rt.fire("l1", source="cron")

    assert outcomes == []
    assert len(asks) == 1


@pytest.mark.asyncio
async def test_cancelled_during_shutdown_leaves_the_run_running_for_the_sweep(tmp_path):
    """A graceful shutdown (SIGTERM) cancels the in-flight workflow, which
    surfaces here identically to a deliberate stop: _interpret's aborted/
    cancelled branch. Finalizing it here — even as `interrupted` — would take
    it out of `running`, and find_orphans only ever looks at `running`
    manifests: finalizing here is what would silently disable the one
    mechanism (the next start's orphan sweep) that actually relaunches the
    pending cause. So during a shutdown the manifest must be left exactly as
    start_run wrote it, and nothing must be reported — the run isn't over."""
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr_for("cancelled")], on_outcome=on_outcome,
                        is_shutting_down=lambda: True)
    m = await rt.fire("l1", source="cron")

    assert m["status"] == "running"
    assert m["owner"] is not None
    record = rl.read_run(tmp_path, "l1", m["run_id"])
    assert record["status"] == "running"
    assert record["finished_at"] is None
    assert outcomes == []

    # The owning process is later found dead — what a real restart discovers.
    rl.update_run(tmp_path, "l1", m["run_id"], owner={"pid": 999999, "started": "long ago"})
    orphans = rl.find_orphans(tmp_path)
    assert [o["run_id"] for o in orphans] == [m["run_id"]]


@pytest.mark.asyncio
async def test_cancelled_outside_shutdown_still_finalizes_error(tmp_path):
    """The deliberate-cancel case (a user calling tasks(action='stop') on the
    workflow) must not regress: outside a shutdown, `cancelled` still
    finalizes `error` and still reports it — nobody wants a run someone chose
    to stop relaunched."""
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr_for("cancelled")], on_outcome=on_outcome)
    m = await rt.fire("l1", source="manual")

    assert m["status"] == "error"
    assert m["finished_at"] is not None
    assert [o.status for o in outcomes] == ["error"]


@pytest.mark.asyncio
async def test_aborted_during_shutdown_behaves_like_cancelled(tmp_path):
    """`aborted` (a node failure) reaches the same else branch as `cancelled`
    in _interpret — during a shutdown it gets identical treatment."""
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr_for("aborted")], on_outcome=on_outcome,
                        is_shutting_down=lambda: True)
    m = await rt.fire("l1", source="cron")

    assert m["status"] == "running"
    assert outcomes == []


@pytest.mark.asyncio
async def test_shutdown_left_run_is_interrupted_and_relaunched_by_the_next_sweep(tmp_path):
    """End-to-end: once the run left `running` by a shutdown is found with a
    dead owner (what a restart discovers), the existing sweep_orphans path —
    already verified live for the abrupt-death case — takes over identically:
    finalize `interrupted`, report it, and relaunch it since nothing had
    started under its reserved workflow run id."""
    from durin.workflow import run_log as wf_rl

    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [_wr_for("cancelled"), _wr_for("completed")],
                            on_outcome=on_outcome, is_shutting_down=lambda: True)
    m = await rt.fire("l1", source="cron")
    assert m["status"] == "running"
    wf_run_id = rl.read_run(tmp_path, "l1", m["run_id"])["workflow_run_id"]
    assert wf_rl.read_manifest(tmp_path, "w1", wf_run_id) is None

    # The owning process is later found dead — what a real restart discovers.
    rl.update_run(tmp_path, "l1", m["run_id"], owner={"pid": 999999, "started": "long ago"})

    handled = await rt.sweep_orphans()
    await _drain()

    assert handled == [m["run_id"]]
    assert rl.read_run(tmp_path, "l1", m["run_id"])["status"] == "interrupted"
    assert [o.status for o in outcomes] == ["interrupted", "done"]
    assert len(calls["exec"]) == 2  # the original attempt, then the relaunch


@pytest.mark.asyncio
async def test_orphan_with_no_workflow_manifest_is_relaunched(tmp_path):
    """Nothing ran, so nothing can be duplicated and the cause is unserved."""
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [_wr_for("completed")], on_outcome=on_outcome)
    _save(tmp_path)
    rl.start_run(tmp_path, "l1", "dead", source="chat", task="the original task")
    rl.update_run(tmp_path, "l1", "dead", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})

    handled = await rt.sweep_orphans()
    # The relaunch is now backgrounded via asyncio.create_task (Important-1
    # fix): finalize+notify happen inline, but the replacement run only
    # starts once the sweep pass yields control back to the event loop.
    await _drain()

    assert handled == ["dead"]
    assert rl.read_run(tmp_path, "l1", "dead")["status"] == "interrupted"
    assert [o.status for o in outcomes] == ["interrupted", "done"]
    # Nothing had started, so the summary must not hedge that partial work
    # might exist — that would contradict the "before the workflow started"
    # detail in the very same message.
    assert "may hold partial work" not in outcomes[0].summary
    # Relaunched with the original task, as a NEW run.
    assert calls["exec"][0][1] == "the original task"
    assert rl.read_run(tmp_path, "l1", "dead")["finished_at"] is not None


@pytest.mark.asyncio
async def test_orphan_whose_workflow_manifest_exists_is_not_relaunched(tmp_path):
    """The engine got going and may have posted externally — relaunching
    would duplicate. Existence of the manifest is the test, NOT a count of
    completed nodes: a node is recorded only when it finishes, so a run killed
    inside its first node has an empty node list and the most side effects."""
    import time as _time

    from durin.workflow import run_log as wf_rl

    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [], on_outcome=on_outcome)
    _save(tmp_path)
    rl.start_run(tmp_path, "l1", "dead", source="chat", task="t")
    rl.update_run(tmp_path, "l1", "dead", workflow_run_id="wf1",
                  owner={"pid": 999999, "started": "long ago"})
    # Manifest present, zero completed nodes — the dangerous case.
    wf_rl.start_run(tmp_path, "w1", "wf1", root_session_key=None,
                    started_at=_time.time(), task="t")

    handled = await rt.sweep_orphans()

    assert handled == ["dead"]
    assert rl.read_run(tmp_path, "l1", "dead")["status"] == "interrupted"
    assert [o.status for o in outcomes] == ["interrupted"]
    # Work was in flight — the summary must name the workflow run id so the
    # operator has a handle for retaking it.
    assert "wf1" in outcomes[0].summary
    assert "may hold partial work" in outcomes[0].summary
    assert calls["exec"] == []


@pytest.mark.asyncio
async def test_relaunch_carries_the_original_origin(tmp_path):
    """The origin must survive the restart, or the replacement run's outcome
    has nowhere to go — the exact failure this whole change exists to fix."""
    origin = {"kind": "session", "session_key": "websocket:abc",
              "channel": "websocket", "chat_id": "abc"}
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr_for("completed")], on_outcome=on_outcome)
    _save(tmp_path)
    rl.start_run(tmp_path, "l1", "dead", source="chat", task="t", origin=origin)
    rl.update_run(tmp_path, "l1", "dead", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})

    await rt.sweep_orphans()
    await _drain()

    assert [o.origin for o in outcomes] == [origin, origin]


@pytest.mark.asyncio
async def test_the_interrupted_notice_names_the_run_that_replaces_it(tmp_path):
    """A loop that silently relaunches itself is its own class of problem, so
    the recipient is told which run took over — and the id in the notice must
    be the id that actually runs, not a second one minted later."""
    import re

    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr_for("completed")], on_outcome=on_outcome)
    _save(tmp_path)
    rl.start_run(tmp_path, "l1", "dead", source="cron", task="t")
    rl.update_run(tmp_path, "l1", "dead", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})

    await rt.sweep_orphans()
    await _drain()

    match = re.search(r"relaunched as (\S+)", outcomes[0].summary)
    assert match, f"the interrupted notice must name its replacement: {outcomes[0].summary!r}"
    replacement = match.group(1)
    assert replacement != "dead"
    # The announced id is the run that really happened, not a fiction.
    replacement_run = rl.read_run(tmp_path, "l1", replacement)
    assert replacement_run is not None and replacement_run["status"] == "done"


@pytest.mark.asyncio
async def test_a_paused_loop_is_not_relaunched_and_the_notice_says_why(tmp_path):
    """`fire` deliberately ignores `enabled` (a manual run-now must work on a
    paused loop), so the sweep is what has to respect it — otherwise a loop
    paused while the gateway was down comes back to life on restart. The
    recipient is told no replacement is coming instead of being promised one."""
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [_wr_for("completed")], on_outcome=on_outcome)
    _save(tmp_path, enabled=False)
    rl.start_run(tmp_path, "l1", "dead", source="cron", task="t")
    rl.update_run(tmp_path, "l1", "dead", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})

    await rt.sweep_orphans()
    await _drain()

    assert calls["exec"] == []
    assert [o.status for o in outcomes] == ["interrupted"]
    assert "paused" in outcomes[0].summary
    assert "relaunched as" not in outcomes[0].summary


@pytest.mark.asyncio
async def test_the_sweep_logs_every_orphan_it_finalizes_not_just_relaunches(tmp_path, caplog):
    """The production complaint that started this: a sweep that finalized runs
    left `grep -i reconcil` returning nothing, because only relaunches logged
    — and the dangerous orphan (work in flight) is exactly the one that is
    never relaunched."""
    import logging

    from loguru import logger as loguru_logger

    from durin.workflow import run_log as wf_rl

    rt, _ = _mk_runtime(tmp_path, [])
    _save(tmp_path)
    rl.start_run(tmp_path, "l1", "dead", source="cron", task="t")
    rl.update_run(tmp_path, "l1", "dead", workflow_run_id="wf1",
                  owner={"pid": 999999, "started": "long ago"})
    wf_rl.start_run(tmp_path, "w1", "wf1", root_session_key=None,
                    started_at=time.time(), task="t")

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="INFO")
    try:
        with caplog.at_level(logging.INFO):
            await rt.sweep_orphans()
    finally:
        loguru_logger.remove(handler_id)

    reconciled = [r.getMessage() for r in caplog.records if "reconcil" in r.getMessage().lower()]
    assert len(reconciled) == 1, f"expected one reconcile line, got {[r.getMessage() for r in caplog.records]}"
    assert "dead" in reconciled[0] and "interrupted" in reconciled[0]


@pytest.mark.asyncio
async def test_answering_a_run_re_stamps_its_owner_so_the_sweep_leaves_it_alone(tmp_path):
    """A parked run is answered hours or days later — routinely after a
    restart, which is the whole point of needs_operator/waiting_info. The
    manifest still names the process that first fired it, so unless `answer`
    re-stamps the owner alongside the status, find_orphans reads the resumed
    run as an orphan and sweep_orphans finalizes it `interrupted` WHILE it is
    still executing: a false "interrupted" delivered to the very person who
    just answered, a `single` loop's slot freed under a live run, and a
    manifest the finishing resume then overwrites again."""
    _save(tmp_path)
    seen = {}
    dead_owner = {"pid": 999999, "started": "long ago"}

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None):
        # Mid-resume: this is the window sweep_orphans would run in.
        seen["orphans"] = [r.get("run_id") for r in rl.find_orphans(tmp_path)]
        seen["owner"] = rl.read_run(tmp_path, "l1", "parked").get("owner")
        return _wr_for("completed")

    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {}}

    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, run_id_factory=lambda: "unused")
    rl.start_run(tmp_path, "l1", "parked", source="cron", task="t")
    rl.finalize_run(tmp_path, "l1", "parked", status="needs_operator",
                    workflow_run_id="wf1", ask="which one?")
    rl.update_run(tmp_path, "l1", "parked", owner=dead_owner)

    await rt.answer("l1", "parked", "this one")

    assert seen["orphans"] == []
    assert seen["owner"] != dead_owner


@pytest.mark.asyncio
async def test_sweep_ignores_a_live_run(tmp_path):
    rt, _ = _mk_runtime(tmp_path, [])
    _save(tmp_path)
    rl.start_run(tmp_path, "l1", "alive", source="cron", task="t")
    assert await rt.sweep_orphans() == []


@pytest.mark.asyncio
async def test_a_slow_relaunch_on_one_loop_does_not_delay_another_loops_outcome(tmp_path):
    """The bug the review demonstrated: sweep_orphans used to await each
    relaunch inline inside the per-orphan loop, so a slow replacement
    workflow on one loop blocked the finalize+notify of every LATER orphan
    in the same pass — and, since a `single`-concurrency loop's manifest
    stays "running" until finalized, made that other loop look busy for the
    whole time too. Finalizing and notifying every orphan must happen in one
    pass, and must not wait on either relaunch's own completion."""
    release = asyncio.Event()

    async def slow_workflow_exec(name, task, *, resume_run_id=None, run_id=None):
        await release.wait()
        return _wr_for("completed")

    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {}}

    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    ids = iter([f"lr{i}" for i in range(20)])
    rt = LoopsRuntime(tmp_path, workflow_exec=slow_workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, run_id_factory=lambda: next(ids), on_outcome=on_outcome)
    _save(tmp_path, name="a")
    _save(tmp_path, name="b")
    for loop in ("a", "b"):
        rl.start_run(tmp_path, loop, f"dead-{loop}", source="cron", task=f"task-{loop}")
        rl.update_run(tmp_path, loop, f"dead-{loop}", workflow_run_id=f"wf-{loop}-never-started",
                      owner={"pid": 999999, "started": "long ago"})

    # A regression back to awaiting each relaunch inline would hang here
    # forever (release is only set below, after the assertions) — pytest-timeout
    # isn't installed and no CI workflow sets timeout-minutes, so an inline
    # await would burn the runner's own multi-hour timeout instead of failing
    # legibly. Bound it explicitly so a regression fails fast and self-explains.
    handled = await asyncio.wait_for(rt.sweep_orphans(), 5)

    # `release` is only set below, after these assertions, so neither relaunch
    # can possibly have completed yet — whichever one has merely been given an
    # event-loop turn is a scheduling detail (asyncio.wait_for's own internals
    # differ between Python 3.11 and 3.12+ in how many turns that takes) and
    # is not the guarantee under test. sweep_orphans returning here at all —
    # instead of hanging on the bound below until the 5s timeout — is what
    # proves both orphans' finalize/notify pass did not wait on either relaunch.
    assert set(handled) == {"dead-a", "dead-b"}
    assert rl.read_run(tmp_path, "a", "dead-a")["status"] == "interrupted"
    assert rl.read_run(tmp_path, "b", "dead-b")["status"] == "interrupted"
    assert len(outcomes) == 2
    assert {o.status for o in outcomes} == {"interrupted"}

    # Let both relaunches actually run so nothing is left dangling.
    release.set()
    await _drain()
    await _drain()


@pytest.mark.asyncio
async def test_a_relaunch_that_loses_the_race_is_retracted_to_the_recipient(tmp_path):
    """A single-concurrency loop with another (genuinely live) run active must
    not raise LoopBusy out of the backgrounded relaunch — a second run must
    not be forced. But the interrupted notice already named a replacement, so
    swallowing the miss into an info log leaves the recipient waiting on a run
    that will never exist. The retraction goes to the same recipient, not to
    the log alone."""
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [], on_outcome=on_outcome)
    _save(tmp_path)   # concurrency defaults to "single"
    rl.start_run(tmp_path, "l1", "dead", source="cron", task="t")
    rl.update_run(tmp_path, "l1", "dead", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})
    # A run genuinely owned by this (live) process — active_runs sees it,
    # so the relaunch attempt for "dead" hits LoopBusy.
    rl.start_run(tmp_path, "l1", "live", source="cron", task="other")

    handled = await rt.sweep_orphans()
    await _drain()

    assert handled == ["dead"]
    assert calls["exec"] == []   # relaunch never ran — LoopBusy, not a crash
    assert len(outcomes) == 2
    announced = outcomes[0].summary.split("relaunched as ")[1].strip()
    # The retraction names the very run id the interrupted notice promised.
    assert outcomes[1].run_id == announced
    assert "produced no outcome" in outcomes[1].summary
    assert "active run" in outcomes[1].summary


@pytest.mark.asyncio
async def test_sweep_skips_an_orphan_whose_loop_was_deleted(tmp_path):
    """LoopNotFound (the loop definition no longer exists) is skipped
    silently — there is no spec to relaunch against, no goal/stuck_after to
    judge against, and nobody to notify on behalf of a loop that is gone."""
    rt, _ = _mk_runtime(tmp_path, [])
    rl.start_run(tmp_path, "ghost", "dead", source="cron", task="t")
    rl.update_run(tmp_path, "ghost", "dead", workflow_run_id="wf1",
                  owner={"pid": 999999, "started": "long ago"})

    handled = await rt.sweep_orphans()

    assert handled == []
    assert rl.read_run(tmp_path, "ghost", "dead")["status"] == "running"


@pytest.mark.asyncio
async def test_sweep_skips_a_malformed_orphan_record_missing_a_run_id(tmp_path, monkeypatch):
    """Defensive guard: find_orphans always sets both keys today, but if a
    legacy/corrupt manifest ever produced a record without one, sweep_orphans
    must skip it rather than crash or try to finalize a run with no id."""
    from durin.loops import runtime as runtime_mod

    rt, _ = _mk_runtime(tmp_path, [])
    _save(tmp_path)
    monkeypatch.setattr(runtime_mod.run_log, "find_orphans",
                        lambda ws: [{"loop": "l1", "run_id": ""}, {"loop": "", "run_id": "r1"}])

    assert await rt.sweep_orphans() == []


@pytest.mark.asyncio
async def test_post_finish_tail_failure_does_not_retract_a_delivered_outcome(tmp_path, monkeypatch):
    """A prune_runs failure AFTER the real outcome already went out must not
    escape _post_finish: _relaunch_orphan's catch-all reads any exception out
    of fire() as "the fire failed" and sends a "produced no outcome"
    retraction — which would contradict the "done" outcome the recipient was
    just given for that very run."""
    from durin.loops import runtime as runtime_mod

    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [_wr_for("completed")], on_outcome=on_outcome)
    _save(tmp_path)
    rl.start_run(tmp_path, "l1", "dead", source="chat", task="the original task")
    rl.update_run(tmp_path, "l1", "dead", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})

    # The first _post_finish call (finalizing "dead" as interrupted) must
    # succeed so the relaunch is scheduled at all; only the SECOND call — the
    # replacement run's own _post_finish, reached after its real "done"
    # outcome is already delivered — simulates the filesystem/lock error.
    real_prune_runs = runtime_mod.run_log.prune_runs
    call_count = [0]

    def flaky_prune_runs(ws, loop, keep):
        call_count[0] += 1
        if call_count[0] >= 2:
            raise OSError("simulated filesystem error pruning runs")
        return real_prune_runs(ws, loop, keep)

    monkeypatch.setattr(runtime_mod.run_log, "prune_runs", flaky_prune_runs)

    handled = await rt.sweep_orphans()
    await _drain()

    assert handled == ["dead"]
    assert calls["exec"], "the replacement run must actually have executed"
    # Exactly the interrupted notice and the replacement's real outcome — no
    # third "produced no outcome" retraction contradicting the "done" above.
    assert [o.status for o in outcomes] == ["interrupted", "done"]
