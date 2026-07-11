from dataclasses import dataclass, field

import pytest

from durin.loops import claims
from durin.loops import run_log as rl
from durin.loops.matcher import TriggerMatcher
from durin.loops.runtime import LoopBusy
from durin.loops.spec import parse_loop
from durin.loops.store import save_loop


@pytest.fixture(autouse=True)
def _isolate_telemetry_dir(tmp_path, monkeypatch):
    """TriggerMatcher._emit binds a session telemetry logger around the
    synchronous loops.event_matched emit (mirrors LoopsRuntime's own
    binding in durin/loops/runtime.py) since there is no bound logger
    outside an agent turn. Without this, tests would write real JSONL
    files to the developer's ~/.cache/durin/telemetry."""
    import durin.telemetry.logger as telemetry_logger

    telemetry_dir = tmp_path / "_telemetry"
    monkeypatch.setattr(telemetry_logger, "_DEFAULT_DIR", telemetry_dir)
    return telemetry_dir


@dataclass
class FakeMsg:
    channel: str = "email"
    sender_id: str = "alice@example.com"
    chat_id: str = "alice@example.com"
    content: str = "hello there"
    metadata: dict = field(default_factory=dict)


def _email_msg(*, sender="alice@example.com", subject="Re: quarterly report",
               thread="digest-1", content="hello there", channel="email"):
    return FakeMsg(
        channel=channel, sender_id=sender, chat_id=sender, content=content,
        metadata={"sender_email": sender, "subject": subject, "email": {"thread": thread}},
    )


class FakeRuntime:
    """Records fire/answer calls; fire() raises LoopBusy for names in `busy`."""

    def __init__(self, busy: set[str] | None = None):
        self.fire_calls: list[tuple] = []
        self.answer_calls: list[tuple] = []
        self._busy = busy or set()

    async def fire(self, name, *, source, task=None, origin=None):
        self.fire_calls.append((name, source, task, origin))
        if name in self._busy:
            raise LoopBusy(name)
        return {"status": "done"}

    async def answer(self, name, run_id, answer):
        self.answer_calls.append((name, run_id, answer))
        return {"status": "done"}


def _save(ws, name="l1", **over):
    data = {
        "name": name, "workflow": "w1", "goal": {"intent": "it is done"},
        "triggers": [{"source": "channel", "channel": "email", "filters": {}}],
    } | over
    save_loop(ws, parse_loop(data))


async def _drain():
    """Let scheduled asyncio.create_task callbacks run."""
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_non_email_message_is_passthrough(tmp_path):
    _save(tmp_path)
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _email_msg(channel="slack")

    consumed = await matcher.handle_inbound(msg)

    assert consumed is False
    assert rt.fire_calls == []


async def test_no_matching_loop_is_passthrough(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email",
                                "filters": {"from_contains": "nobody@nowhere.com"}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())

    assert consumed is False
    assert rt.fire_calls == []


async def test_structural_match_fires_with_origin(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email",
                                "filters": {"from_contains": "alice", "subject_contains": "quarterly"}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _email_msg()

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    assert len(rt.fire_calls) == 1
    name, source, task, origin = rt.fire_calls[0]
    assert name == "l1" and source == "channel" and task == "hello there"
    assert origin == {
        "channel": "email", "sender": "alice@example.com", "chat_id": "alice@example.com",
        "thread": "digest-1", "subject": "Re: quarterly report",
    }


async def test_filters_reject_when_sender_does_not_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email",
                                "filters": {"from_contains": "bob@"}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())

    assert consumed is False
    assert rt.fire_calls == []


async def test_filters_reject_when_subject_does_not_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email",
                                "filters": {"subject_contains": "invoice"}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())

    assert consumed is False
    assert rt.fire_calls == []


async def test_semantic_judge_consulted_only_after_structural_pass(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email",
                                "filters": {"from_contains": "bob@"}, "semantic": "is urgent"}])
    rt = FakeRuntime()
    judge_calls = []

    async def judge(condition, summary):
        judge_calls.append((condition, summary))
        return True

    matcher = TriggerMatcher(tmp_path, runtime=rt, semantic_judge=judge)

    consumed = await matcher.handle_inbound(_email_msg())  # sender is alice, filter wants bob

    assert consumed is False
    assert judge_calls == []  # structural filter already failed; judge never consulted


async def test_semantic_judge_true_fires(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email",
                                "filters": {"from_contains": "alice"}, "semantic": "is urgent"}])
    rt = FakeRuntime()
    judge_calls = []

    async def judge(condition, summary):
        judge_calls.append((condition, summary))
        return True

    matcher = TriggerMatcher(tmp_path, runtime=rt, semantic_judge=judge)
    msg = _email_msg()

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    assert len(judge_calls) == 1
    condition, summary = judge_calls[0]
    assert condition == "is urgent"
    assert "alice@example.com" in summary
    assert "Re: quarterly report" in summary
    assert "hello there" in summary
    assert len(rt.fire_calls) == 1


async def test_semantic_judge_error_is_no_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email",
                                "filters": {"from_contains": "alice"}, "semantic": "is urgent"}])
    rt = FakeRuntime()

    async def judge(condition, summary):
        raise RuntimeError("boom")

    matcher = TriggerMatcher(tmp_path, runtime=rt, semantic_judge=judge)

    consumed = await matcher.handle_inbound(_email_msg())

    assert consumed is False
    assert rt.fire_calls == []


async def test_semantic_condition_without_judge_configured_is_no_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email",
                                "filters": {"from_contains": "alice"}, "semantic": "is urgent"}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)  # no semantic_judge

    consumed = await matcher.handle_inbound(_email_msg())

    assert consumed is False
    assert rt.fire_calls == []


async def test_parallel_busy_still_fires(tmp_path):
    _save(tmp_path, concurrency="parallel",
          triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rl.start_run(tmp_path, "l1", "existing-run", source="cron", task="t")
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())
    await _drain()

    assert consumed is True
    assert len(rt.fire_calls) == 1
    assert rt.fire_calls[0][0] == "l1"


async def test_single_busy_queues_when_enqueue_wired(tmp_path):
    _save(tmp_path, concurrency="single",
          triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rl.start_run(tmp_path, "l1", "existing-run", source="cron", task="t")
    rt = FakeRuntime()
    queued = []
    matcher = TriggerMatcher(tmp_path, runtime=rt, enqueue=lambda loop, event: queued.append((loop, event)))

    consumed = await matcher.handle_inbound(_email_msg())

    assert consumed is True
    assert rt.fire_calls == []
    assert len(queued) == 1
    loop_name, event = queued[0]
    assert loop_name == "l1"
    assert event["content"] == "hello there"
    assert event["origin"]["thread"] == "digest-1"


async def test_single_busy_without_enqueue_passes_through(tmp_path):
    _save(tmp_path, concurrency="single",
          triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rl.start_run(tmp_path, "l1", "existing-run", source="cron", task="t")
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)  # no enqueue wired

    consumed = await matcher.handle_inbound(_email_msg())

    assert consumed is False
    assert rt.fire_calls == []


async def test_single_no_active_run_fires(tmp_path):
    _save(tmp_path, concurrency="single",
          triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())
    await _drain()

    assert consumed is True
    assert len(rt.fire_calls) == 1


async def test_claim_wake_answers_waiting_run(tmp_path):
    _save(tmp_path)
    rl.start_run(tmp_path, "l1", "run1", source="channel", task="t")
    rl.finalize_run(tmp_path, "l1", "run1", status="waiting_info", ask="what's the PO number?")
    claims.register(tmp_path, key="digest-1", loop="l1", run_id="run1")
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _email_msg(content="PO-4521")

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    assert rt.answer_calls == [("l1", "run1", "PO-4521")]
    assert rt.fire_calls == []


async def test_stale_claim_is_released_and_falls_through_to_trigger_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rl.start_run(tmp_path, "l1", "run1", source="channel", task="t")
    rl.finalize_run(tmp_path, "l1", "run1", status="done")  # no longer waiting
    claims.register(tmp_path, key="digest-1", loop="l1", run_id="run1")
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())
    await _drain()

    assert consumed is True
    assert rt.answer_calls == []
    assert len(rt.fire_calls) == 1  # fell through to trigger match instead
    assert claims.lookup(tmp_path, "digest-1") is None  # stale claim released


async def test_message_without_thread_can_still_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _email_msg(thread=None)

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    assert rt.fire_calls[0][3]["thread"] is None


async def test_disabled_loop_is_skipped(tmp_path):
    _save(tmp_path, enabled=False,
          triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())

    assert consumed is False
    assert rt.fire_calls == []


async def test_first_match_wins_in_alphabetical_order(tmp_path):
    _save(tmp_path, name="a-loop", triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    _save(tmp_path, name="z-loop", triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())
    await _drain()

    assert consumed is True
    assert rt.fire_calls[0][0] == "a-loop"
