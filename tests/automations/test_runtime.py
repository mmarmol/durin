import asyncio
import logging
import time

import pytest
from loguru import logger as loguru_logger

from durin.automations import claims
from durin.automations import queue as automation_queue
from durin.automations import run_log as rl
from durin.automations.chains import CHAIN_HOP_CAP
from durin.automations.runtime import AutomationBusy, AutomationsRuntime
from durin.automations.spec import AutomationSpec, AutomationTrigger, Delivery, Help, Life
from durin.automations.store import load_automation, save_automation
from durin.bus.events import SendReceipt
from durin.workflow.result import WorkflowResult


@pytest.fixture(autouse=True)
def _isolate_telemetry_dir(tmp_path, monkeypatch):
    """AutomationsRuntime binds a session telemetry logger around
    fire/try_fire/answer (durin/automations/runtime.py) since those
    entrypoints run outside an agent turn's bound ContextVar. The suite-wide
    conftest fixture already keeps every test off the real
    ~/.cache/durin/telemetry, but it is session-scoped (one shared directory
    for the whole run) — a test asserting on JSONL content here needs its OWN
    fresh, single-file directory instead."""
    import durin.telemetry.logger as telemetry_logger

    telemetry_dir = tmp_path / "_telemetry"
    monkeypatch.setattr(telemetry_logger, "_DEFAULT_DIR", telemetry_dir)
    return telemetry_dir


class _RecordingTelemetry:
    """Minimal telemetry-sink double: records (event_type, data) pairs."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))


def _spec(name="a1", workflow="w1", **over) -> AutomationSpec:
    base = dict(name=name, workflow=workflow, enabled=True, triggers=(),
                delivery=Delivery(), help=Help(), life=None, concurrency="single")
    base.update(over)
    return AutomationSpec(**base)


def _save(tmp_path, **over):
    save_automation(tmp_path, _spec(**over))


def _wr(status, **kw):
    return WorkflowResult(status=status, final_output=kw.pop("out", "output"),
                          run_id=kw.pop("run_id", "wf1"), **kw)


def _mk_runtime(tmp_path, results, *, on_help_ask=None, on_counterpart_ask=None,
                on_outcome=None, is_shutting_down=None, queue_ttl_s=3600):
    calls = {"exec": []}

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None,
                            work_key=None, root_session_key=None):
        calls["exec"].append({"name": name, "task": task, "resume_run_id": resume_run_id,
                              "run_id": run_id, "work_key": work_key,
                              "root_session_key": root_session_key})
        return results.pop(0)

    ids = iter([f"r{i}" for i in range(200)])
    rt = AutomationsRuntime(tmp_path, workflow_exec=workflow_exec, keep_runs=20,
                            on_help_ask=on_help_ask, on_counterpart_ask=on_counterpart_ask,
                            on_outcome=on_outcome, run_id_factory=lambda: next(ids),
                            queue_ttl_s=queue_ttl_s, is_shutting_down=is_shutting_down)
    return rt, calls


async def _drain():
    """Let a `_post_finish`-scheduled asyncio.create_task (chain/drain) fire."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Basic fire / classify round-trips
# ---------------------------------------------------------------------------

async def test_completed_but_not_achieved_stays_completed(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")])
    m = await rt.fire("a1", source="manual")
    assert m["status"] == "completed"
    assert calls["exec"][0]["name"] == "w1"
    assert calls["exec"][0]["root_session_key"] == "automation:a1"


async def test_workflow_run_id_persisted_before_launch(tmp_path):
    """A later sweep needs the workflow run id on disk BEFORE the workflow
    itself starts — the only handle it has for asking whether work began."""
    _save(tmp_path)
    seen = {}

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None,
                            work_key=None, root_session_key=None):
        seen["mid_flight"] = rl.read_run(tmp_path, "a1", "run0").get("workflow_run_id")
        seen["run_id"] = run_id
        seen["root_session_key"] = root_session_key
        return WorkflowResult(status="completed", final_output="out", run_id=run_id)

    ids = iter(["run0", "wf0"])
    rt = AutomationsRuntime(tmp_path, workflow_exec=workflow_exec, keep_runs=20,
                            run_id_factory=lambda: next(ids))
    await rt.fire("a1", source="manual")
    assert seen["root_session_key"] == "automation:a1"
    assert seen["mid_flight"] == seen["run_id"] == "wf0"


async def test_work_key_derived_only_from_custom_prefixed_thread(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [_wr("completed"), _wr("completed"), _wr("completed")])

    await rt.fire("a1", source="channel", origin={"thread": "custom:a1:TCK-1"})
    await rt.fire("a1", source="channel", origin={"thread": "slack:C1:123"})
    await rt.fire("a1", source="cron")

    assert calls["exec"][0]["work_key"] == "custom:a1:TCK-1"
    assert calls["exec"][1]["work_key"] is None
    assert calls["exec"][2]["work_key"] is None


async def test_try_fire_skips_disabled_automation(tmp_path):
    _save(tmp_path, enabled=False)
    rt, calls = _mk_runtime(tmp_path, [])
    assert await rt.try_fire("a1", source="cron") is None
    assert calls["exec"] == []


async def test_single_concurrency_busy_raises_and_try_fire_skips(tmp_path):
    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="q", ask_kind="question")])
    await rt.fire("a1", source="manual")  # leaves an active paused run
    with pytest.raises(AutomationBusy):
        await rt.fire("a1", source="manual")
    assert await rt.try_fire("a1", source="cron") is None


async def test_workflow_exec_exception_finalizes_failed_with_detail(tmp_path):
    _save(tmp_path)

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None,
                            work_key=None, root_session_key=None):
        raise RuntimeError("boom")

    ids = iter([f"r{i}" for i in range(10)])
    rt = AutomationsRuntime(tmp_path, workflow_exec=workflow_exec, keep_runs=20,
                            run_id_factory=lambda: next(ids))
    m = await rt.fire("a1", source="manual")
    assert m["status"] == "failed"
    assert m["detail"] == "boom"


# ---------------------------------------------------------------------------
# Life condition: achieved / stuck streaks
# ---------------------------------------------------------------------------

async def test_achieved_auto_disables_always_delivers_and_chains_still_fire(tmp_path):
    _save(tmp_path, life=Life(intent="get to done", achieved_when="any_completed"),
         delivery=Delivery(channel="email", to="ops@x.com", notify="never"))
    _save(tmp_path, name="downstream", workflow="w2",
         triggers=(AutomationTrigger(source="chain", chain_automation="a1", chain_when="achieved"),))
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [_wr("completed", out="great success")], on_outcome=on_outcome)
    m = await rt.fire("a1", source="manual")
    assert m["status"] == "achieved"

    # notify="never" would normally silence everything — achieved bypasses it.
    assert [o.status for o in outcomes] == ["achieved"]
    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["delivery"]["result"] == "delivered"
    assert record["delivery"]["channel"] == "email"

    spec_after = load_automation(tmp_path, "a1")
    assert spec_after.enabled is False

    await _drain()
    downstream_calls = [c for c in calls["exec"] if c["name"] == "w2"]
    assert len(downstream_calls) == 1
    assert downstream_calls[0]["task"] == "great success"


async def test_failures_streak_reaching_max_attempts_escalate_pause_disables(tmp_path):
    _save(tmp_path, life=Life(intent="x", max_attempts=2, on_stuck="escalate_pause"))
    asks = []

    async def on_help_ask(spec, run_id, kind, text, proposal):
        asks.append((spec.name, run_id, kind, text, proposal))
        return None

    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted"), _wr("exhausted")], on_help_ask=on_help_ask)

    m1 = await rt.fire("a1", source="cron")
    assert m1["status"] == "failed"
    assert load_automation(tmp_path, "a1").enabled is True  # streak=1, not yet at max

    m2 = await rt.fire("a1", source="cron")
    assert m2["status"] == "failed"
    assert load_automation(tmp_path, "a1").enabled is False  # streak=2 == max_attempts

    escalations = [a for a in asks if a[2] == "escalation"]
    assert len(escalations) == 1
    assert escalations[0][0] == "a1"


async def test_on_stuck_notify_mode_notifies_but_does_not_disable(tmp_path):
    _save(tmp_path, life=Life(intent="x", max_attempts=1, on_stuck="notify"))
    asks = []

    async def on_help_ask(spec, run_id, kind, text, proposal):
        asks.append(kind)
        return None

    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted")], on_help_ask=on_help_ask)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "failed"
    assert "escalation" in asks
    assert load_automation(tmp_path, "a1").enabled is True


async def test_on_stuck_keep_mode_is_silent(tmp_path):
    _save(tmp_path, life=Life(intent="x", max_attempts=1, on_stuck="keep"))
    asks = []

    async def on_help_ask(spec, run_id, kind, text, proposal):
        asks.append(kind)
        return None

    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted")], on_help_ask=on_help_ask)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "failed"
    assert asks == []
    assert load_automation(tmp_path, "a1").enabled is True


async def test_rejected_run_does_not_re_trigger_an_already_reached_stuck_escalation(tmp_path):
    """The stuck check only runs when THIS run's own status counts toward the
    streak (failed/completed). A rejected run is streak-transparent — the
    escalation must not re-fire just because an OLDER streak already crossed
    max_attempts; consecutive_unachieved would still report the same count
    either way, so re-checking on a transparent run only spams the help
    destination and redundantly re-saves the automation with nothing new
    having failed."""
    _save(tmp_path, life=Life(intent="x", max_attempts=1, on_stuck="notify"))
    asks = []

    async def on_help_ask(spec, run_id, kind, text, proposal):
        asks.append(kind)
        return None

    rt, _ = _mk_runtime(tmp_path, [
        _wr("exhausted"),
        _wr("needs_input", out="ok to ship?", ask_kind="approval"),
        WorkflowResult(status="cancelled", final_output="ok to ship?", run_id="wf1", rejected=True),
    ], on_help_ask=on_help_ask)

    m1 = await rt.fire("a1", source="cron")
    assert m1["status"] == "failed"
    assert "escalation" in asks
    asks.clear()

    m2 = await rt.fire("a1", source="cron")
    assert m2["status"] == "paused"  # legitimately calls on_help_ask(kind="approval") — expected
    asks.clear()
    m3 = await rt.answer("a1", m2["run_id"], "n/a", action="reject")
    assert m3["status"] == "rejected"
    assert asks == []  # no re-fired escalation for the streak-transparent rejection


async def test_automation_without_life_never_escalates(tmp_path):
    _save(tmp_path)  # life=None
    asks = []

    async def on_help_ask(spec, run_id, kind, text, proposal):
        asks.append(kind)
        return None

    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted"), _wr("exhausted"), _wr("exhausted")],
                        on_help_ask=on_help_ask)
    for _ in range(3):
        await rt.fire("a1", source="cron")
    assert asks == []
    assert load_automation(tmp_path, "a1").enabled is True


# ---------------------------------------------------------------------------
# Delivery policy
# ---------------------------------------------------------------------------

async def test_when_notable_with_silent_label_is_silenced(tmp_path):
    _save(tmp_path, delivery=Delivery(channel="email", to="ops@x.com", notify="when_notable",
                                      silent_labels=("NOTHING_TO_REPORT",)))
    rt, _ = _mk_runtime(tmp_path, [_wr("completed", final_route_label="NOTHING_TO_REPORT")])
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "completed"
    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["delivery"]["result"] == "silenced"


async def test_when_notable_with_non_silent_label_is_delivered(tmp_path):
    _save(tmp_path, delivery=Delivery(channel="email", to="ops@x.com", notify="when_notable"))
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr("completed", final_route_label="INVOICED")], on_outcome=on_outcome)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "completed"
    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["delivery"]["result"] == "delivered"
    assert len(outcomes) == 1


async def test_failed_reaches_help_backstop_when_notify_never(tmp_path):
    """notify=never would otherwise drop a failure into total silence — the
    help channel is the backstop so it is never silently lost."""
    _save(tmp_path, delivery=Delivery(channel=None, notify="never"),
         help=Help(channel="slack", to="ops-room"))
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted")], on_outcome=on_outcome)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "failed"
    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["delivery"]["result"] == "delivered"
    assert record["delivery"]["channel"] == "slack"
    assert len(outcomes) == 1


# ---------------------------------------------------------------------------
# Help asks: approval, question, counterpart
# ---------------------------------------------------------------------------

async def test_approval_pause_calls_on_help_ask_with_proposal_and_registers_receipt_claim(tmp_path):
    _save(tmp_path)
    asks = []

    async def on_help_ask(spec, run_id, kind, text, proposal):
        asks.append((spec.name, run_id, kind, text, proposal))
        return SendReceipt(thread_key="slack:C1:123.45")

    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="approve this?", ask_kind="approval")],
                        on_help_ask=on_help_ask)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "paused"
    assert m["ask_kind"] == "approval"
    assert m["proposal"] == "approve this?"

    assert len(asks) == 1
    name, run_id, kind, text, proposal = asks[0]
    assert kind == "approval"
    assert proposal == "approve this?"
    assert text == f"[a1 · {m['run_id']}] approve this?"

    claim = claims.lookup(tmp_path, "slack:C1:123.45")
    assert claim is not None
    assert claim["automation"] == "a1" and claim["run_id"] == m["run_id"]


async def test_on_help_ask_returning_none_registers_no_claim(tmp_path):
    asks = []

    async def on_help_ask(spec, run_id, kind, text, proposal):
        asks.append((kind, text, proposal))
        return None

    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="which one?", ask_kind="question")],
                        on_help_ask=on_help_ask)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "paused"
    assert asks == [("question", f"[a1 · {m['run_id']}] which one?", None)]
    # No claim was ever registered — the claims file doesn't even exist.
    assert claims.claims_path(tmp_path).exists() is False


async def test_counterpart_question_registers_origin_thread_claim(tmp_path):
    _save(tmp_path)
    counterpart_asks = []

    async def on_counterpart_ask(name, run_id, origin, text):
        counterpart_asks.append((name, run_id, origin, text))

    rt, _ = _mk_runtime(
        tmp_path,
        [_wr("needs_input", out="[TO:counterpart] confirm the invoice?", ask_kind="question")],
        on_counterpart_ask=on_counterpart_ask,
    )
    origin = {"thread": "thread-123"}
    m = await rt.fire("a1", source="channel", origin=origin)
    assert m["status"] == "paused"
    assert m["ask"] == "confirm the invoice?"

    claim = claims.lookup(tmp_path, "thread-123")
    assert claim is not None
    assert claim["automation"] == "a1" and claim["run_id"] == m["run_id"]
    assert counterpart_asks == [("a1", m["run_id"], origin, "confirm the invoice?")]


async def test_counterpart_ask_without_origin_degrades_to_help(tmp_path):
    _save(tmp_path)
    asks = []

    async def on_help_ask(spec, run_id, kind, text, proposal):
        asks.append(text)
        return None

    rt, _ = _mk_runtime(
        tmp_path,
        [_wr("needs_input", out="[TO:counterpart] confirm the invoice?", ask_kind="question")],
        on_help_ask=on_help_ask,
    )
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "paused"
    assert m["ask"] == "confirm the invoice? (counterpart channel unavailable — answer here)"
    assert asks == [f"[a1 · {m['run_id']}] {m['ask']}"]


# ---------------------------------------------------------------------------
# answer()
# ---------------------------------------------------------------------------

async def test_answer_action_approve_resumes_with_approve_text_and_records_approval(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [_wr("needs_input", out="ok to ship?", ask_kind="approval"),
                                       _wr("completed")])
    m = await rt.fire("a1", source="cron")
    wf_run_id = rl.read_run(tmp_path, "a1", m["run_id"])["workflow_run_id"]

    m2 = await rt.answer("a1", m["run_id"], "ignored free text", action="approve", by="alice")
    assert m2["status"] == "completed"
    assert calls["exec"][1]["task"] == "approve"
    assert calls["exec"][1]["resume_run_id"] == wf_run_id

    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["approval"] == {"action": "approve", "by": "alice", "at_ms": record["approval"]["at_ms"]}


async def test_answer_action_reject_resumes_with_reject_text_and_records_approval(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [
        _wr("needs_input", out="ok to ship?", ask_kind="approval"),
        WorkflowResult(status="cancelled", final_output="ok to ship?", run_id="wf1", rejected=True),
    ])
    m = await rt.fire("a1", source="cron")

    m2 = await rt.answer("a1", m["run_id"], "doesn't matter", action="reject", by="bob")
    assert m2["status"] == "rejected"
    assert calls["exec"][1]["task"] == "reject"

    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["approval"]["action"] == "reject" and record["approval"]["by"] == "bob"


async def test_answer_free_text_revise_resumes_with_raw_text_and_records_revise(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [
        _wr("needs_input", out="proposal v1", ask_kind="approval"),
        _wr("needs_input", out="proposal v2", ask_kind="approval"),
    ])
    m = await rt.fire("a1", source="cron")

    m2 = await rt.answer("a1", m["run_id"], "please tweak the tone")
    assert m2["status"] == "paused"
    assert m2["proposal"] == "proposal v2"
    assert calls["exec"][1]["task"] == "please tweak the tone"

    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["approval"]["action"] == "revise"


async def test_answer_on_question_ask_ignores_action_and_resumes_text_as_is(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [_wr("needs_input", out="which one?", ask_kind="question"),
                                       _wr("completed")])
    m = await rt.fire("a1", source="cron")

    m2 = await rt.answer("a1", m["run_id"], "the second one", action="approve")
    assert m2["status"] == "completed"
    assert calls["exec"][1]["task"] == "the second one"  # action ignored for questions

    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record.get("approval") is None  # only approval asks get recorded


async def test_answer_re_stamps_owner_and_releases_claim(tmp_path):
    _save(tmp_path)
    rt, calls = _mk_runtime(tmp_path, [
        _wr("needs_input", out="[TO:counterpart] confirm?", ask_kind="question"),
        _wr("completed"),
    ])
    origin = {"thread": "thread-abc"}
    m = await rt.fire("a1", source="channel", origin=origin)
    assert claims.lookup(tmp_path, "thread-abc") is not None

    m2 = await rt.answer("a1", m["run_id"], "yes, confirmed")
    assert m2["status"] == "completed"
    assert calls["exec"][1]["task"] == "yes, confirmed"
    assert claims.lookup(tmp_path, "thread-abc") is None


async def test_answer_raises_when_run_is_not_paused(tmp_path):
    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("completed")])
    m = await rt.fire("a1", source="manual")
    assert m["status"] == "completed"
    with pytest.raises(ValueError):
        await rt.answer("a1", m["run_id"], "too late")


# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------

async def test_chain_dedup_fires_target_once_despite_two_matching_triggers(tmp_path):
    _save(tmp_path)
    _save(tmp_path, name="downstream", workflow="w2",
         triggers=(
             AutomationTrigger(source="chain", chain_automation="a1", chain_when="completed"),
             AutomationTrigger(source="chain", chain_automation="a1", chain_when="any"),
         ))
    rt, calls = _mk_runtime(tmp_path, [_wr("completed", out="upstream output")])
    m = await rt.fire("a1", source="manual")
    assert m["status"] == "completed"
    await _drain()

    downstream_calls = [c for c in calls["exec"] if c["name"] == "w2"]
    assert len(downstream_calls) == 1
    assert downstream_calls[0]["task"] == "upstream output"


async def test_chain_uses_failure_summary_when_there_is_no_final_output(tmp_path):
    _save(tmp_path)
    _save(tmp_path, name="downstream", workflow="w2",
         triggers=(AutomationTrigger(source="chain", chain_automation="a1", chain_when="failed"),))
    calls = []

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None,
                            work_key=None, root_session_key=None):
        calls.append((name, task))
        if name == "w1":
            raise RuntimeError("boom")
        return WorkflowResult(status="completed", final_output="ok", run_id=run_id)

    ids = iter([f"r{i}" for i in range(20)])
    rt = AutomationsRuntime(tmp_path, workflow_exec=workflow_exec, keep_runs=20,
                            run_id_factory=lambda: next(ids))
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "failed"
    await _drain()

    downstream_calls = [c for c in calls if c[0] == "w2"]
    assert len(downstream_calls) == 1
    assert "boom" in downstream_calls[0][1] and "failed" in downstream_calls[0][1]


async def test_chain_depth_cap_refuses_further_dispatch(tmp_path, caplog):
    _save(tmp_path)
    _save(tmp_path, name="downstream", workflow="w2",
         triggers=(AutomationTrigger(source="chain", chain_automation="a1", chain_when="any"),))
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")])

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="WARNING")
    try:
        with caplog.at_level(logging.WARNING):
            m = await rt.fire("a1", source="chain", chain_depth=CHAIN_HOP_CAP)
            await _drain()
    finally:
        loguru_logger.remove(handler_id)

    assert m["status"] == "completed"
    assert all(c["name"] != "w2" for c in calls["exec"])
    assert any("refused" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Shutdown handling
# ---------------------------------------------------------------------------

async def test_shutdown_predicate_leaves_manifest_running(tmp_path):
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("cancelled")], on_outcome=on_outcome, is_shutting_down=lambda: True)
    m = await rt.fire("a1", source="cron")

    assert m["status"] == "running"
    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["status"] == "running"
    assert record["finished_at"] is None
    assert outcomes == []


async def test_cancelled_outside_shutdown_still_finalizes_failed(tmp_path):
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("cancelled")], on_outcome=on_outcome)
    m = await rt.fire("a1", source="manual")

    assert m["status"] == "failed"
    assert m["finished_at"] is not None


async def test_shutdown_does_not_swallow_a_genuine_approval_rejection(tmp_path):
    """rejected=True on a cancelled result is a deliberate human 'no', never a
    shutdown artifact — it must finalize 'rejected' even mid-shutdown."""
    _save(tmp_path)
    rt, _ = _mk_runtime(
        tmp_path,
        [WorkflowResult(status="cancelled", final_output="nope", run_id="wf1", rejected=True)],
        is_shutting_down=lambda: True,
    )
    m = await rt.fire("a1", source="manual")
    assert m["status"] == "rejected"


# ---------------------------------------------------------------------------
# Orphan sweep
# ---------------------------------------------------------------------------

async def test_orphan_sweep_relaunches_only_never_started(tmp_path):
    from durin.workflow import run_log as wf_rl

    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [_wr("completed")], on_outcome=on_outcome)
    _save(tmp_path, name="a1", workflow="w1", delivery=Delivery(channel="email", to="ops@x.com"))
    _save(tmp_path, name="a2", workflow="w2", delivery=Delivery(channel="email", to="ops@x.com"))

    # a1/dead1: never started -> must relaunch.
    rl.start_run(tmp_path, "a1", "dead1", cause={"kind": "cron", "excerpt": "t", "trigger_index": None})
    rl.update_run(tmp_path, "a1", "dead1", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})

    # a2/dead2: workflow manifest already exists -> must NOT relaunch.
    rl.start_run(tmp_path, "a2", "dead2", cause={"kind": "cron", "excerpt": "t", "trigger_index": None})
    rl.update_run(tmp_path, "a2", "dead2", workflow_run_id="wf1",
                  owner={"pid": 999999, "started": "long ago"})
    wf_rl.start_run(tmp_path, "w2", "wf1", root_session_key=None, started_at=time.time(), task="t")

    handled = await rt.sweep_orphans()
    await _drain()

    assert set(handled) == {"dead1", "dead2"}
    assert rl.read_run(tmp_path, "a1", "dead1")["status"] == "interrupted"
    assert rl.read_run(tmp_path, "a2", "dead2")["status"] == "interrupted"
    assert len(calls["exec"]) == 1  # only the relaunch for a1 — none for a2
    assert calls["exec"][0]["name"] == "w1"
    assert [o.status for o in outcomes] == ["interrupted", "interrupted", "completed"]


async def test_a_paused_automation_is_not_relaunched(tmp_path):
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [], on_outcome=on_outcome)
    _save(tmp_path, enabled=False, delivery=Delivery(channel="email", to="ops@x.com"))
    rl.start_run(tmp_path, "a1", "dead", cause={"kind": "cron", "excerpt": "t", "trigger_index": None})
    rl.update_run(tmp_path, "a1", "dead", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})

    await rt.sweep_orphans()
    await _drain()

    assert calls["exec"] == []
    assert "paused" in outcomes[0].summary


async def test_sweep_ignores_a_live_run(tmp_path):
    rt, _ = _mk_runtime(tmp_path, [])
    _save(tmp_path)
    rl.start_run(tmp_path, "a1", "alive", cause={"kind": "cron", "excerpt": "t", "trigger_index": None})
    assert await rt.sweep_orphans() == []


async def test_sweep_skips_an_orphan_whose_automation_was_deleted(tmp_path):
    rt, _ = _mk_runtime(tmp_path, [])
    rl.start_run(tmp_path, "ghost", "dead", cause={"kind": "cron", "excerpt": "t", "trigger_index": None})
    rl.update_run(tmp_path, "ghost", "dead", workflow_run_id="wf1",
                  owner={"pid": 999999, "started": "long ago"})

    handled = await rt.sweep_orphans()

    assert handled == []
    assert rl.read_run(tmp_path, "ghost", "dead")["status"] == "running"


async def test_relaunch_that_loses_the_fire_race_is_retracted(tmp_path):
    outcomes = []

    async def on_outcome(outcome):
        outcomes.append(outcome)

    rt, calls = _mk_runtime(tmp_path, [], on_outcome=on_outcome)
    _save(tmp_path, delivery=Delivery(channel="email", to="ops@x.com"))  # concurrency defaults to "single"
    rl.start_run(tmp_path, "a1", "dead", cause={"kind": "cron", "excerpt": "t", "trigger_index": None})
    rl.update_run(tmp_path, "a1", "dead", workflow_run_id="wf-never-started",
                  owner={"pid": 999999, "started": "long ago"})
    # A run genuinely owned by this (live) process — active_runs sees it, so
    # the relaunch attempt for "dead" hits AutomationBusy.
    rl.start_run(tmp_path, "a1", "live", cause={"kind": "cron", "excerpt": "other", "trigger_index": None})

    handled = await rt.sweep_orphans()
    await _drain()

    assert handled == ["dead"]
    assert calls["exec"] == []
    assert len(outcomes) == 2
    assert "produced no outcome" in outcomes[1].summary


# ---------------------------------------------------------------------------
# Single-concurrency queue drain
# ---------------------------------------------------------------------------

async def test_post_finish_drains_next_fresh_queued_event(tmp_path):
    _save(tmp_path)
    origin = {"channel": "email", "thread": "t1"}
    automation_queue.push(tmp_path, "a1", {"content": "queued task", "origin": origin})
    rt, calls = _mk_runtime(tmp_path, [_wr("completed"), _wr("completed")])

    m = await rt.fire("a1", source="manual")
    assert m["status"] == "completed"
    await _drain()

    assert len(calls["exec"]) == 2
    assert calls["exec"][1]["task"] == "queued task"
    assert automation_queue.pending(tmp_path, "a1") == 0


async def test_post_finish_drain_emits_event_matched_drained(tmp_path, _isolate_telemetry_dir):
    """Mirrors durin.loops.runtime's own placement: the "drained" action is
    emitted right where the queue drain finds a fresh event, before the
    re-fire is scheduled — not from the matcher, which never sees a drained
    event at all."""
    import json

    _save(tmp_path)
    origin = {"channel": "email", "thread": "t1"}
    automation_queue.push(tmp_path, "a1", {"content": "queued task", "origin": origin})
    rt, _ = _mk_runtime(tmp_path, [_wr("completed"), _wr("completed")])

    await rt.fire("a1", source="manual")
    await _drain()

    files = list(_isolate_telemetry_dir.glob("*.jsonl"))
    events = [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").strip().splitlines()]
    matched = [e for e in events if e["type"] == "automations.event_matched"]
    assert len(matched) == 1
    assert matched[0]["data"]["automation"] == "a1"
    assert matched[0]["data"]["source_channel"] == "email"
    assert matched[0]["data"]["action"] == "drained"


async def test_post_finish_skips_drain_for_parallel_concurrency(tmp_path):
    _save(tmp_path, concurrency="parallel")
    automation_queue.push(tmp_path, "a1", {"content": "queued task", "origin": {"channel": "email"}})
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")])

    m = await rt.fire("a1", source="manual")
    assert m["status"] == "completed"
    await _drain()

    assert len(calls["exec"]) == 1
    assert automation_queue.pending(tmp_path, "a1") == 1  # left untouched


async def test_post_finish_skips_drain_when_this_run_just_disabled_the_automation(tmp_path):
    """achieved auto-disables the automation for FUTURE fires — the queue
    must not be drained into a just-paused automation."""
    _save(tmp_path, life=Life(intent="x", achieved_when="any_completed"))
    automation_queue.push(tmp_path, "a1", {"content": "queued task", "origin": {"channel": "email"}})
    rt, calls = _mk_runtime(tmp_path, [_wr("completed")])

    m = await rt.fire("a1", source="manual")
    assert m["status"] == "achieved"
    await _drain()

    assert len(calls["exec"]) == 1  # no drained second fire
    assert automation_queue.pending(tmp_path, "a1") == 1  # left untouched


# ---------------------------------------------------------------------------
# Fix round 1, finding 1: chain dispatch is a safely-backgrounded task — a
# busy single-concurrency target is queued (not lost), and an unhandled
# exception is logged rather than becoming an untraceable dropped task.
# ---------------------------------------------------------------------------

async def test_chain_into_busy_single_target_queues_event_and_drains_after_free(tmp_path):
    _save(tmp_path)  # a1
    _save(tmp_path, name="downstream", workflow="w2",
         triggers=(AutomationTrigger(source="chain", chain_automation="a1", chain_when="any"),))

    rt, calls = _mk_runtime(tmp_path, [
        _wr("needs_input", out="q", ask_kind="question"),  # 1: downstream's own first fire -> pauses
        _wr("completed", out="upstream done"),              # 2: a1 finishes -> chains into busy downstream
        _wr("completed"),                                   # 3: downstream's answer() resume -> completes
        _wr("completed"),                                   # 4: downstream's drained chain re-fire -> completes
    ])

    d = await rt.fire("downstream", source="manual")
    assert d["status"] == "paused"

    m = await rt.fire("a1", source="manual")
    assert m["status"] == "completed"
    await _drain()

    # The chain fire lost the race (downstream busy) — queued, not dropped.
    assert automation_queue.pending(tmp_path, "downstream") == 1

    d2 = await rt.answer("downstream", d["run_id"], "answering")
    assert d2["status"] == "completed"
    await _drain()

    # _post_finish's own queue drain picks up the chained event once
    # downstream frees its concurrency slot.
    assert automation_queue.pending(tmp_path, "downstream") == 0
    downstream_tasks = [c["task"] for c in calls["exec"] if c["name"] == "w2"]
    assert "upstream done" in downstream_tasks


async def test_chain_fire_exception_is_logged_not_lost(tmp_path, caplog):
    """A chain target that no longer exists (AutomationNotFound, uncaught by
    fire() itself) must be logged by _chain_fire, not turned into an
    asyncio "Task exception was never retrieved" warning nobody ever reads."""
    rt, _ = _mk_runtime(tmp_path, [])

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="ERROR")
    try:
        with caplog.at_level(logging.ERROR):
            await rt._chain_fire("does-not-exist", "task text", 1)
    finally:
        loguru_logger.remove(handler_id)

    assert any("chained fire" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Fix round 1, finding 2: telemetry producers land now, mirroring the exact
# bind-around-the-call mechanism durin.loops.runtime uses.
# ---------------------------------------------------------------------------

async def test_fire_emits_telemetry_when_unbound(tmp_path, _isolate_telemetry_dir):
    import json

    from durin.telemetry.logger import current_telemetry

    telemetry_dir = _isolate_telemetry_dir
    _save(tmp_path, delivery=Delivery(channel="email", to="ops@x.com"))
    rt, _ = _mk_runtime(tmp_path, [_wr("completed")])

    assert current_telemetry() is None
    await rt.fire("a1", source="manual")
    assert current_telemetry() is None  # unbound again after the call returns

    files = list(telemetry_dir.glob("*.jsonl"))
    assert len(files) == 1
    events = [json.loads(line) for line in files[0].read_text(encoding="utf-8").strip().splitlines()]
    event_types = [e["type"] for e in events]
    assert "automations.fired" in event_types
    assert "automations.run_finished" in event_types
    assert "automations.delivered" in event_types

    fired = next(e for e in events if e["type"] == "automations.fired")
    # emit_tool_event auto-injects session_key/iteration when absent — check
    # the fields this call site actually sets, not the full payload shape.
    assert fired["data"]["automation"] == "a1"
    assert fired["data"]["source"] == "manual"
    assert fired["data"]["skipped"] is False


async def test_fire_reuses_already_bound_telemetry(tmp_path, _isolate_telemetry_dir):
    from durin.telemetry.logger import bind_telemetry, reset_telemetry

    telemetry_dir = _isolate_telemetry_dir
    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("completed")])

    fake = _RecordingTelemetry()
    token = bind_telemetry(fake)
    try:
        await rt.fire("a1", source="manual")
    finally:
        reset_telemetry(token)

    event_types = [event_type for event_type, _ in fake.events]
    assert "automations.fired" in event_types
    assert "automations.run_finished" in event_types
    assert not telemetry_dir.exists() or list(telemetry_dir.glob("*.jsonl")) == []


async def test_try_fire_busy_skip_emits_fired_with_skipped_true(tmp_path, _isolate_telemetry_dir):
    import json

    _save(tmp_path)
    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="q", ask_kind="question")])
    await rt.fire("a1", source="manual")  # leaves an active paused run
    assert await rt.try_fire("a1", source="cron") is None

    files = list(_isolate_telemetry_dir.glob("*.jsonl"))
    events = [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").strip().splitlines()]
    skipped = [e for e in events if e["type"] == "automations.fired" and e["data"].get("skipped") is True]
    assert len(skipped) == 1
    assert skipped[0]["data"]["automation"] == "a1"
    assert skipped[0]["data"]["source"] == "cron"


async def test_try_fire_disabled_skip_emits_no_telemetry(tmp_path, _isolate_telemetry_dir):
    """Mirrors durin.loops.runtime's own try_fire: a disabled skip is silent
    — only the busy skip carries a signal, since disabled is a deliberate,
    already-visible configuration state rather than a race worth flagging."""
    import json

    _save(tmp_path, enabled=False)
    rt, _ = _mk_runtime(tmp_path, [])
    assert await rt.try_fire("a1", source="cron") is None

    files = list(_isolate_telemetry_dir.glob("*.jsonl"))
    events = [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").strip().splitlines()]
    assert events == []


async def test_stuck_escalation_emits_automations_escalated(tmp_path, _isolate_telemetry_dir):
    import json

    _save(tmp_path, life=Life(intent="x", max_attempts=1, on_stuck="notify"))
    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted")])
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "failed"

    files = list(_isolate_telemetry_dir.glob("*.jsonl"))
    events = [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").strip().splitlines()]
    escalated = [e for e in events if e["type"] == "automations.escalated"]
    assert len(escalated) == 1
    assert escalated[0]["data"]["automation"] == "a1"
    assert escalated[0]["data"]["run_id"] == m["run_id"]
    assert escalated[0]["data"]["consecutive_unachieved"] == 1


# ---------------------------------------------------------------------------
# Fix round 1, finding 3: on_outcome receives the routed destination.
# ---------------------------------------------------------------------------

async def test_on_outcome_receives_the_delivery_destination(tmp_path):
    _save(tmp_path, delivery=Delivery(channel="email", to="ops@x.com", notify="always"))
    received = []

    async def on_outcome(outcome):
        received.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr("completed")], on_outcome=on_outcome)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "completed"

    assert len(received) == 1
    assert received[0].kind == "delivery"
    assert received[0].channel == "email"
    assert received[0].to == "ops@x.com"


async def test_on_outcome_receives_the_session_destination(tmp_path):
    _save(tmp_path)
    received = []

    async def on_outcome(outcome):
        received.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr("completed")], on_outcome=on_outcome)
    origin = {"kind": "session", "session_key": "websocket:abc", "channel": "websocket", "chat_id": "c1"}
    m = await rt.fire("a1", source="chat", origin=origin)
    assert m["status"] == "completed"

    assert received[0].kind == "session"
    # A session destination carries no channel/to of its own — the wiring
    # layer routes it from outcome.origin["session_key"] instead, same as
    # dest.origin, so channel/to stay None rather than duplicating it.
    assert received[0].channel is None
    assert received[0].to is None
    assert received[0].origin["session_key"] == "websocket:abc"


async def test_on_outcome_receives_the_help_backstop_destination(tmp_path):
    _save(tmp_path, delivery=Delivery(channel=None, notify="never"), help=Help(channel="slack", to="ops-room"))
    received = []

    async def on_outcome(outcome):
        received.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr("exhausted")], on_outcome=on_outcome)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "failed"

    assert received[0].kind == "help"
    assert received[0].channel == "slack"
    assert received[0].to == "ops-room"


# ---------------------------------------------------------------------------
# Fix round 1, finding 5: achieved on a help-only automation must be heard.
# ---------------------------------------------------------------------------

async def test_achieved_on_help_only_automation_routes_to_help(tmp_path):
    _save(tmp_path, life=Life(intent="x", achieved_when="any_completed"),
         delivery=Delivery(channel=None), help=Help(channel="slack", to="ops-room"))
    received = []

    async def on_outcome(outcome):
        received.append(outcome)

    rt, _ = _mk_runtime(tmp_path, [_wr("completed")], on_outcome=on_outcome)
    m = await rt.fire("a1", source="cron")
    assert m["status"] == "achieved"

    assert received[0].kind == "help"
    assert received[0].channel == "slack" and received[0].to == "ops-room"
    record = rl.read_run(tmp_path, "a1", m["run_id"])
    assert record["delivery"]["result"] == "delivered"


# ---------------------------------------------------------------------------
# Fix round 2, finding 1: chain_depth (and source) must survive the
# busy -> queue -> drain path, or CHAIN_HOP_CAP is silently defeated.
# ---------------------------------------------------------------------------

async def test_chain_depth_survives_the_busy_queue_drain_path_so_the_cap_still_bites(tmp_path, monkeypatch, caplog):
    """a1 -> downstream (busy at the moment) -> tail, with CHAIN_HOP_CAP
    patched to 1. The chain hop into downstream is depth 1 (still under the
    cap, so it's attempted); downstream is busy, so it's queued. Once
    downstream frees up and drains, it must resume at depth 1 (not reset to
    0) so ITS OWN attempt to chain into "tail" is refused by the cap.

    downstream's OWN busy-making run is resolved as a REJECTED approval
    (not completed) specifically so that resolution's own _post_finish does
    not independently chain into "tail" at depth 0 (rejected is not a
    chainable outcome) — isolating the assertion to the drained fire alone,
    which is the one this test is actually about.
    """
    import durin.automations.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "CHAIN_HOP_CAP", 1)

    _save(tmp_path)  # a1
    _save(tmp_path, name="downstream", workflow="w2",
         triggers=(AutomationTrigger(source="chain", chain_automation="a1", chain_when="any"),))
    _save(tmp_path, name="tail", workflow="w3",
         triggers=(AutomationTrigger(source="chain", chain_automation="downstream", chain_when="any"),))

    rt, calls = _mk_runtime(tmp_path, [
        _wr("needs_input", out="approve?", ask_kind="approval"),  # 1: downstream's own first fire -> pauses (busy)
        _wr("completed", out="upstream done"),                    # 2: a1 finishes -> chains into busy downstream (depth 1)
        WorkflowResult(status="cancelled", final_output="approve?", run_id="wf1", rejected=True),  # 3: reject -> no chain
        _wr("completed"),                                         # 4: downstream's drained chain re-fire -> completes
    ])

    d = await rt.fire("downstream", source="manual")
    assert d["status"] == "paused"

    m = await rt.fire("a1", source="manual")
    assert m["status"] == "completed"
    await _drain()
    assert automation_queue.pending(tmp_path, "downstream") == 1

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="WARNING")
    try:
        with caplog.at_level(logging.WARNING):
            d2 = await rt.answer("downstream", d["run_id"], "n/a", action="reject")
            assert d2["status"] == "rejected"  # not chainable — dispatches nothing on its own
            await _drain()
    finally:
        loguru_logger.remove(handler_id)

    # "tail" must never fire — the drained re-fire (the only completion left
    # that could chain into it) carried chain_depth=1 through, so
    # downstream's own _dispatch_chains hit the (patched) cap.
    assert all(c["name"] != "w3" for c in calls["exec"])
    assert any("refused" in r.getMessage() for r in caplog.records)


async def test_drained_chain_fire_records_source_chain_in_cause_and_telemetry(tmp_path, _isolate_telemetry_dir):
    import json

    _save(tmp_path)
    _save(tmp_path, name="downstream", workflow="w2",
         triggers=(AutomationTrigger(source="chain", chain_automation="a1", chain_when="any"),))

    rt, _ = _mk_runtime(tmp_path, [
        _wr("needs_input", out="q", ask_kind="question"),
        _wr("completed", out="upstream done"),
        _wr("completed"),  # downstream's answer() resume
        _wr("completed"),  # downstream's drained chain re-fire
    ])

    d = await rt.fire("downstream", source="manual")
    m = await rt.fire("a1", source="manual")
    assert m["status"] == "completed"
    await _drain()

    d2 = await rt.answer("downstream", d["run_id"], "answering")
    assert d2["status"] == "completed"
    await _drain()

    runs = rl.list_runs(tmp_path, "downstream", limit=None)
    drained_run = next(r for r in runs if r["run_id"] not in (d["run_id"], d2["run_id"]))
    assert drained_run["cause"]["kind"] == "chain"

    files = list(_isolate_telemetry_dir.glob("*.jsonl"))
    events = [json.loads(line) for f in files for line in f.read_text(encoding="utf-8").strip().splitlines()]
    downstream_fired = [e for e in events if e["type"] == "automations.fired"
                        and e["data"].get("automation") == "downstream"]
    assert any(e["data"].get("source") == "chain" for e in downstream_fired)


async def test_chain_fire_busy_handler_logs_when_queue_push_itself_raises(tmp_path, monkeypatch, caplog):
    """The residual of finding 1: queue.push inside the AutomationBusy handler
    must be guarded — if it raises (lock/IO), the exception must be logged,
    not left to escape _chain_fire and recreate the exact "Task exception
    was never retrieved" mode finding 1 eliminated."""
    import durin.automations.runtime as runtime_mod

    _save(tmp_path)
    _save(tmp_path, name="downstream", workflow="w2")  # concurrency defaults to "single"
    rt, _ = _mk_runtime(tmp_path, [_wr("needs_input", out="q", ask_kind="question")])
    await rt.fire("downstream", source="manual")  # leaves downstream busy

    def _raise_push(*args, **kwargs):
        raise OSError("simulated queue push failure")

    monkeypatch.setattr(runtime_mod.queue, "push", _raise_push)

    handler_id = loguru_logger.add(caplog.handler, format="{message}", level="ERROR")
    try:
        with caplog.at_level(logging.ERROR):
            await rt._chain_fire("downstream", "task text", 1)  # hits AutomationBusy, then push raises
    finally:
        loguru_logger.remove(handler_id)

    assert any("queue" in r.getMessage().lower() for r in caplog.records)
