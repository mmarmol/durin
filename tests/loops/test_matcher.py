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
    is_dm: bool = False


def _email_msg(*, sender="alice@example.com", subject="Re: quarterly report",
               thread="digest-1", content="hello there", channel="email"):
    return FakeMsg(
        channel=channel, sender_id=sender, chat_id=sender, content=content,
        metadata={"sender_email": sender, "subject": subject, "email": {"thread": thread}},
    )


def _telegram_msg(*, sender="alice", chat_id="grp1", content="hello there",
                   message_thread_id=None, message_id=None, is_forum=False, is_dm=False):
    metadata = {}
    if is_forum:
        metadata["is_forum"] = True
    if message_thread_id is not None:
        metadata["message_thread_id"] = message_thread_id
    if message_id is not None:
        metadata["message_id"] = message_id
    return FakeMsg(
        channel="telegram", sender_id=sender, chat_id=chat_id, content=content,
        metadata=metadata, is_dm=is_dm,
    )


def _slack_msg(*, sender="alice", chat_id="C1", content="hello there", thread_ts=None):
    metadata = {}
    if thread_ts is not None:
        metadata["slack"] = {"thread_ts": thread_ts}
    return FakeMsg(channel="slack", sender_id=sender, chat_id=chat_id, content=content, metadata=metadata)


def _whatsapp_msg(*, sender="alice", chat_id="12345", content="hello there", is_group=False):
    return FakeMsg(
        channel="whatsapp", sender_id=sender, chat_id=chat_id, content=content,
        metadata={"is_group": is_group}, is_dm=not is_group,
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
        "thread": "digest-1", "subject": "Re: quarterly report", "reply": {"thread": "digest-1"},
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


async def test_claim_wake_skipped_for_always_new_loop(tmp_path):
    """A loop whose channel trigger declares match: "always_new" does not
    want thread replies resuming its parked runs — each matching message
    should open its own new run instead. The claim stays in place (the run
    really is still waiting) and the message falls through to normal
    trigger matching."""
    _save(tmp_path, concurrency="parallel",
          triggers=[{"source": "channel", "channel": "email",
                     "filters": {}, "match": "always_new"}])
    rl.start_run(tmp_path, "l1", "run1", source="channel", task="t")
    rl.finalize_run(tmp_path, "l1", "run1", status="waiting_info", ask="what's the PO number?")
    claims.register(tmp_path, key="digest-1", loop="l1", run_id="run1")
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _email_msg(content="PO-4521")

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True  # fell through and fired a fresh run
    assert rt.answer_calls == []
    claim = claims.lookup(tmp_path, "digest-1")
    assert claim is not None and claim["loop"] == "l1" and claim["run_id"] == "run1"
    assert len(rt.fire_calls) == 1
    assert rt.fire_calls[0][0] == "l1"
    assert rt.fire_calls[0][2] == "PO-4521"


async def test_claim_wake_default_when_spec_missing(tmp_path):
    """A claim can outlive its loop's spec file (the loop was deleted while
    a run it started was still parked waiting on a reply). With no spec to
    check a match policy against, the matcher defaults to honoring the
    claim and attempts the wake — mirroring current behavior rather than
    silently dropping a message a human might be counting on. If the
    wake itself then fails (runtime.answer needs the same spec and raises
    LoopNotFound), the matcher must not leave the claim stuck: it releases
    it so a later message on that thread isn't captured by a dead claim
    forever."""
    rl.start_run(tmp_path, "ghost", "run1", source="channel", task="t")
    rl.finalize_run(tmp_path, "ghost", "run1", status="waiting_info", ask="what's the PO number?")
    claims.register(tmp_path, key="digest-1", loop="ghost", run_id="run1")

    class MissingSpecRuntime(FakeRuntime):
        async def answer(self, name, run_id, answer):
            self.answer_calls.append((name, run_id, answer))
            from durin.loops.spec import LoopNotFound
            raise LoopNotFound(f"loop '{name}' not found")

    rt = MissingSpecRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _email_msg(content="PO-4521")

    consumed = await matcher.handle_inbound(msg)
    assert consumed is True  # wake attempted synchronously (no spec = default wake)
    await _drain()

    assert rt.answer_calls == [("ghost", "run1", "PO-4521")]
    assert claims.lookup(tmp_path, "digest-1") is None  # released after the failed wake


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


async def test_sequential_messages_single_loop_second_queues(tmp_path):
    """Two sequential inbound messages for the same single-concurrency loop,
    with no active run yet: the pending-fires guard must make the second
    message see the loop as busy synchronously (before the first message's
    scheduled `_fire` task has even run), so it queues instead of racing
    into runtime.fire() and hitting LoopBusy."""
    _save(tmp_path, concurrency="single",
          triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    queued = []
    matcher = TriggerMatcher(tmp_path, runtime=rt, enqueue=lambda loop, event: queued.append((loop, event)))

    consumed1 = await matcher.handle_inbound(_email_msg(content="first"))
    consumed2 = await matcher.handle_inbound(_email_msg(content="second"))
    await _drain()

    assert consumed1 is True
    assert consumed2 is True
    assert len(rt.fire_calls) == 1
    assert rt.fire_calls[0][2] == "first"
    assert len(queued) == 1
    loop_name, event = queued[0]
    assert loop_name == "l1"
    assert event["content"] == "second"


async def test_sequential_messages_no_enqueue_passthrough(tmp_path):
    """Same race, but with no queue wired: the second message must be
    rejected (passed through as a normal turn) at decision time via the
    pending-fires guard — never by losing a LoopBusy race inside the
    scheduled fire task."""
    _save(tmp_path, concurrency="single",
          triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)  # no enqueue wired

    consumed1 = await matcher.handle_inbound(_email_msg(content="first"))
    consumed2 = await matcher.handle_inbound(_email_msg(content="second"))
    await _drain()

    assert consumed1 is True
    assert consumed2 is False
    assert len(rt.fire_calls) == 1
    assert rt.fire_calls[0][2] == "first"


async def test_loopbusy_fallback_enqueues(tmp_path):
    """Belt-and-braces: if the pending-fires guard somehow misses (e.g. the
    fire task already started/finished its own bookkeeping) and
    runtime.fire() itself raises LoopBusy, the fallback in `_fire` must
    still enqueue the event when a queue is wired, with matching
    telemetry."""
    _save(tmp_path, concurrency="single",
          triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime(busy={"l1"})
    queued = []
    matcher = TriggerMatcher(tmp_path, runtime=rt, enqueue=lambda loop, event: queued.append((loop, event)))
    origin = {"channel": "email", "sender": "alice@example.com", "chat_id": "alice@example.com",
              "thread": "digest-1", "subject": "Re: quarterly report"}

    # Call _fire directly to simulate the guard having already been cleared
    # (empty pending set) while runtime.fire() still raises LoopBusy.
    await matcher._fire("l1", "email", "hello there", origin)

    assert len(rt.fire_calls) == 1
    assert len(queued) == 1
    loop_name, event = queued[0]
    assert loop_name == "l1"
    assert event["content"] == "hello there"


async def test_fired_telemetry_emitted_after_successful_fire(tmp_path):
    """`_dispatch_match` must not claim "fired" before runtime.fire()
    actually returns; the fired event is emitted from inside `_fire` only
    on success."""
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    events = []
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    orig_emit = matcher._emit

    def spy_emit(loop_name, channel, action):
        events.append(action)
        orig_emit(loop_name, channel, action)

    matcher._emit = spy_emit

    consumed = await matcher.handle_inbound(_email_msg())
    assert events == []  # not emitted yet — fire task hasn't run
    await _drain()

    assert consumed is True
    assert events == ["fired"]


async def test_fire_task_is_tracked_then_discarded_on_completion(tmp_path):
    """The matcher must hold a strong ref to the scheduled fire task (an
    unreferenced asyncio.create_task can be GC'd mid-run) and release it once
    the task finishes."""
    _save(tmp_path, triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())
    assert consumed is True
    assert len(matcher._tasks) == 1  # tracked synchronously before the task ran

    await _drain()

    assert matcher._tasks == set()  # discarded once the task completed


async def test_first_match_wins_in_alphabetical_order(tmp_path):
    _save(tmp_path, name="a-loop", triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    _save(tmp_path, name="z-loop", triggers=[{"source": "channel", "channel": "email", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)

    consumed = await matcher.handle_inbound(_email_msg())
    await _drain()

    assert consumed is True
    assert rt.fire_calls[0][0] == "a-loop"


# --- V4: channel-agnostic matching + custom correlation -------------------


async def test_telegram_topic_message_fires_with_prefixed_thread_and_reply(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "telegram", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _telegram_msg(chat_id="grp1", message_thread_id=42, message_id="m1", is_forum=True)

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    name, source, task, origin = rt.fire_calls[0]
    assert name == "l1" and source == "channel"
    assert origin["thread"] == "telegram:grp1:topic:42"
    assert origin["reply"] == {"message_thread_id": 42, "message_id": "m1"}


async def test_slack_thread_reply_wakes_claimed_run(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "slack", "filters": {}}])
    rl.start_run(tmp_path, "l1", "run1", source="channel", task="t")
    rl.finalize_run(tmp_path, "l1", "run1", status="waiting_info", ask="which channel?")
    claims.register(tmp_path, key="slack:C1:1690000000.000100", loop="l1", run_id="run1")
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _slack_msg(chat_id="C1", thread_ts="1690000000.000100", content="the #general channel")

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    assert rt.answer_calls == [("l1", "run1", "the #general channel")]
    assert rt.fire_calls == []


async def test_whatsapp_group_message_has_no_thread(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "whatsapp", "filters": {}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _whatsapp_msg(chat_id="120363", is_group=True)

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    origin = rt.fire_calls[0][3]
    assert origin["thread"] is None


async def test_text_contains_and_sender_contains_filters_both_pass(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "telegram",
                                "filters": {"sender_contains": "ali", "text_contains": "invoice"}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _telegram_msg(sender="alice", content="please send the invoice today")

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    assert len(rt.fire_calls) == 1


async def test_text_contains_filter_rejects_when_text_does_not_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "channel", "channel": "telegram",
                                "filters": {"sender_contains": "ali", "text_contains": "invoice"}}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _telegram_msg(sender="alice", content="just saying hi")

    consumed = await matcher.handle_inbound(msg)

    assert consumed is False
    assert rt.fire_calls == []


async def test_correlate_fire_sets_origin_thread_but_registers_no_claim(tmp_path):
    """Matching a correlate pattern on a fresh (non-wake) message must not
    itself register a claim — that is the runtime's job once the run
    actually parks waiting for a reply (Task 4)."""
    _save(tmp_path, triggers=[{"source": "channel", "channel": "telegram",
                                "filters": {}, "correlate": r"TICKET-(\d+)"}])
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _telegram_msg(chat_id="grp1", content="please help with TICKET-123 today")

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    origin = rt.fire_calls[0][3]
    assert origin["thread"] == "custom:l1:123"
    assert claims.lookup(tmp_path, "custom:l1:123") is None


async def test_correlate_wakes_claim_from_a_different_thread(tmp_path):
    """A run parked under a custom correlate key must be woken by any later
    message whose correlate match captures the same id, even if that
    message arrives on a completely different thread/chat."""
    _save(tmp_path, triggers=[{"source": "channel", "channel": "telegram",
                                "filters": {}, "correlate": r"TICKET-(\d+)"}])
    rl.start_run(tmp_path, "l1", "run1", source="channel", task="t")
    rl.finalize_run(tmp_path, "l1", "run1", status="waiting_info", ask="more details?")
    claims.register(tmp_path, key="custom:l1:123", loop="l1", run_id="run1")
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    # A different chat/thread entirely, no forum/topic — only the captured
    # ticket id ties it back to the parked run.
    msg = _telegram_msg(chat_id="unrelated-chat", content="update on TICKET-123: shipped")

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    assert rt.answer_calls == [("l1", "run1", "update on TICKET-123: shipped")]
    assert rt.fire_calls == []


async def test_correlate_key_takes_precedence_over_thread_key(tmp_path):
    """When both a custom correlate key and the message's plain thread_key
    have claims registered, the custom key wins."""
    _save(tmp_path, triggers=[{"source": "channel", "channel": "telegram",
                                "filters": {}, "correlate": r"TICKET-(\d+)"}])
    rl.start_run(tmp_path, "l1", "run-custom", source="channel", task="t")
    rl.finalize_run(tmp_path, "l1", "run-custom", status="waiting_info", ask="a?")
    rl.start_run(tmp_path, "l1", "run-thread", source="channel", task="t")
    rl.finalize_run(tmp_path, "l1", "run-thread", status="waiting_info", ask="b?")
    claims.register(tmp_path, key="custom:l1:123", loop="l1", run_id="run-custom")
    claims.register(tmp_path, key="telegram:dm:bob", loop="l1", run_id="run-thread")
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = _telegram_msg(chat_id="bob", is_dm=True, content="re TICKET-123: any update?")

    consumed = await matcher.handle_inbound(msg)
    await _drain()

    assert consumed is True
    assert rt.answer_calls == [("l1", "run-custom", "re TICKET-123: any update?")]


async def test_unsupported_channel_is_passthrough(tmp_path):
    _save(tmp_path)
    rt = FakeRuntime()
    matcher = TriggerMatcher(tmp_path, runtime=rt)
    msg = FakeMsg(channel="websocket", sender_id="alice", chat_id="alice", content="hi")

    consumed = await matcher.handle_inbound(msg)

    assert consumed is False
    assert rt.fire_calls == []
