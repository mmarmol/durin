"""HookDispatcher — webhook trigger ingress matching (fired/queued/woken/
no_match/semantic-reject paths). Runs on top of a live TriggerMatcher, so it
shares that matcher's FakeRuntime double and telemetry-isolation fixture."""

import pytest

from durin.automations import claims
from durin.automations import run_log as rl
from durin.automations.hooks import HookDispatcher
from durin.automations.matcher import TriggerMatcher
from durin.automations.runtime import AutomationBusy
from durin.automations.spec import parse_automation
from durin.automations.store import save_automation


@pytest.fixture(autouse=True)
def _per_test_telemetry_dir(tmp_path, monkeypatch):
    """Mirrors tests/automations/test_matcher.py's fixture of the same name:
    the matcher's `_emit` binds a session telemetry logger around every
    automations.event_matched call, which otherwise writes real JSONL files
    to the developer's ~/.cache/durin/telemetry. Deliberately not named
    `_isolate_telemetry_dir`: that collides with the session-scoped guard
    of the same name in tests/conftest.py, and a fixture defined in a test
    module shadows a same-named one from a parent conftest for every test
    in that module — reusing the name would silently disable the
    suite-wide guard for this entire file."""
    import durin.telemetry.logger as telemetry_logger

    telemetry_dir = tmp_path / "_telemetry"
    monkeypatch.setattr(telemetry_logger, "_DEFAULT_DIR", telemetry_dir)
    return telemetry_dir


class FakeRuntime:
    """Records fire/answer calls; fire() raises AutomationBusy for names in `busy`."""

    def __init__(self, busy: set[str] | None = None):
        self.fire_calls: list[tuple] = []
        self.answer_calls: list[tuple] = []
        self._busy = busy or set()

    async def fire(self, name, *, source, task=None, origin=None):
        self.fire_calls.append((name, source, task, origin))
        if name in self._busy:
            raise AutomationBusy(name)
        return {"status": "done"}

    async def answer(self, name, run_id, answer):
        self.answer_calls.append((name, run_id, answer))
        return {"status": "done"}


def _save(ws, name="l1", **over):
    data = {
        "name": name, "workflow": "w1",
        "triggers": [{"source": "webhook", "hook": "orders"}],
    } | over
    save_automation(ws, parse_automation(data))


def _cause(**over):
    return {"kind": "channel", "excerpt": "t", "trigger_index": 0} | over


def _park(ws, name, run_id, *, ask="confirm?"):
    """Start a run and park it awaiting a reply — the shape
    AutomationsRuntime._park actually leaves behind (status="paused"), which
    is what TriggerMatcher._try_wake's claim-wake check looks for."""
    rl.start_run(ws, name, run_id, cause=_cause())
    rl.update_run(ws, name, run_id, status="paused", ask=ask, ask_kind="question")


def _dispatcher(tmp_path, rt, *, enqueue=None, semantic_judge=None):
    matcher = TriggerMatcher(tmp_path, runtime=rt, enqueue=enqueue, semantic_judge=semantic_judge)
    return HookDispatcher(matcher)


async def _drain():
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_no_matching_automation_is_no_match(tmp_path):
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("orders", {"text": "hi"})

    assert result == {"result": "no_match"}
    assert rt.fire_calls == []


async def test_disabled_automation_is_no_match(tmp_path):
    _save(tmp_path, enabled=False)
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("orders", {"text": "hi"})

    assert result == {"result": "no_match"}


async def test_wrong_hook_name_is_no_match(tmp_path):
    _save(tmp_path)
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("shipments", {"text": "hi"})

    assert result == {"result": "no_match"}


async def test_matching_hook_fires(tmp_path):
    _save(tmp_path)
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("orders", {"text": "new order #42"})
    await _drain()

    assert result == {"result": "fired", "automation": "l1"}
    assert len(rt.fire_calls) == 1
    name, source, task, origin = rt.fire_calls[0]
    # source ("webhook", not "channel"): matcher.py's _fire distinguishes a
    # webhook-origin fire (this dispatcher's synthetic channel="webhook") from
    # an ordinary inbound-channel one, so a run's cause.kind can tell the two
    # apart — see matcher.py's own _fire docstring comment.
    assert name == "l1" and source == "webhook" and task == "new order #42"
    assert origin == {
        "channel": "webhook", "sender": "orders", "chat_id": "orders",
        "thread": None, "subject": "orders", "reply": {},
    }


async def test_payload_without_text_is_compact_json(tmp_path):
    _save(tmp_path)
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("orders", {"order_id": 42, "status": "paid"})
    await _drain()

    assert result == {"result": "fired", "automation": "l1"}
    task = rt.fire_calls[0][2]
    assert task == '{"order_id":42,"status":"paid"}'


async def test_text_and_json_are_capped_at_4000_chars(tmp_path):
    _save(tmp_path)
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("orders", {"text": "x" * 5000})
    await _drain()

    assert result == {"result": "fired", "automation": "l1"}
    assert len(rt.fire_calls[0][2]) == 4000


async def test_busy_single_concurrency_queues(tmp_path):
    _save(tmp_path, concurrency="single")
    rl.start_run(tmp_path, "l1", "run0", cause=_cause(kind="cron"))
    rt = FakeRuntime()
    queued = []
    dispatcher = _dispatcher(tmp_path, rt, enqueue=lambda name, ev: queued.append((name, ev)))

    result = await dispatcher.dispatch("orders", {"text": "new order #42"})

    assert result == {"result": "queued", "automation": "l1"}
    assert rt.fire_calls == []
    assert len(queued) == 1
    automation_name, event = queued[0]
    assert automation_name == "l1"
    assert event["content"] == "new order #42"
    assert event["source"] == "webhook"


async def test_busy_no_queue_wired_is_no_match(tmp_path):
    """Belt-and-braces: production always wires the matcher's queue (see
    durin/cli/commands.py), but a matcher without one must not silently
    report "fired"/"queued" for a busy automation — no_match is the only
    value in the documented result contract that fits "not consumed"."""
    _save(tmp_path, concurrency="single")
    rl.start_run(tmp_path, "l1", "run0", cause=_cause(kind="cron"))
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("orders", {"text": "new order #42"})

    assert result == {"result": "no_match"}
    assert rt.fire_calls == []


async def test_correlate_wakes_a_waiting_run(tmp_path):
    _save(tmp_path, triggers=[{"source": "webhook", "hook": "orders",
                                "correlate": r"ORDER-(\d+)"}])
    _park(tmp_path, "l1", "run1")
    claims.register(tmp_path, key="custom:l1:42", automation="l1", run_id="run1")
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("orders", {"text": "update for ORDER-42: shipped"})
    await _drain()

    assert result == {"result": "woken", "automation": "l1", "run_id": "run1"}
    assert rt.answer_calls == [("l1", "run1", "update for ORDER-42: shipped")]
    assert rt.fire_calls == []


async def test_correlate_no_match_falls_through_to_fire(tmp_path):
    _save(tmp_path, concurrency="parallel",
          triggers=[{"source": "webhook", "hook": "orders",
                     "correlate": r"ORDER-(\d+)"}])
    _park(tmp_path, "l1", "run1")
    claims.register(tmp_path, key="custom:l1:42", automation="l1", run_id="run1")
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    # No "ORDER-<digits>" in the text, so no custom key is derived — the
    # claim on custom:l1:42 is irrelevant here, a fresh run fires instead.
    result = await dispatcher.dispatch("orders", {"text": "unrelated payload"})
    await _drain()

    assert result == {"result": "fired", "automation": "l1"}
    assert rt.answer_calls == []


async def test_semantic_true_fires(tmp_path):
    _save(tmp_path, triggers=[{"source": "webhook", "hook": "orders", "semantic": "is urgent"}])
    rt = FakeRuntime()
    judge_calls = []

    async def judge(condition, summary):
        judge_calls.append((condition, summary))
        return True

    dispatcher = _dispatcher(tmp_path, rt, semantic_judge=judge)

    result = await dispatcher.dispatch("orders", {"text": "urgent: server down"})
    await _drain()

    assert result == {"result": "fired", "automation": "l1"}
    assert len(judge_calls) == 1
    condition, summary = judge_calls[0]
    assert condition == "is urgent"
    assert "urgent: server down" in summary
    # title=None for the match pass: no "Subject:" line, no hook-name prefix
    # that would shift a correlate/semantic match against the raw payload.
    assert "Subject:" not in summary


async def test_semantic_false_is_no_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "webhook", "hook": "orders", "semantic": "is urgent"}])
    rt = FakeRuntime()

    async def judge(condition, summary):
        return False

    dispatcher = _dispatcher(tmp_path, rt, semantic_judge=judge)

    result = await dispatcher.dispatch("orders", {"text": "routine update"})

    assert result == {"result": "no_match"}
    assert rt.fire_calls == []


async def test_semantic_condition_without_judge_configured_is_no_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "webhook", "hook": "orders", "semantic": "is urgent"}])
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)   # no semantic_judge

    result = await dispatcher.dispatch("orders", {"text": "urgent!"})

    assert result == {"result": "no_match"}
    assert rt.fire_calls == []


async def test_semantic_judge_error_is_no_match(tmp_path):
    _save(tmp_path, triggers=[{"source": "webhook", "hook": "orders", "semantic": "is urgent"}])
    rt = FakeRuntime()

    async def judge(condition, summary):
        raise RuntimeError("boom")

    dispatcher = _dispatcher(tmp_path, rt, semantic_judge=judge)

    result = await dispatcher.dispatch("orders", {"text": "urgent!"})

    assert result == {"result": "no_match"}
    assert rt.fire_calls == []


async def test_correlate_wakes_even_when_semantic_would_reject(tmp_path):
    """Reproduces the wake-unreachable bug: a webhook trigger with both
    ``correlate`` and ``semantic`` set must still resume a waiting run when
    the correlate key matches an active claim — the semantic condition gates
    NEW fires only, never a wake, mirroring matcher.py's own claim-wake pass,
    which never evaluates ``semantic`` either."""
    _save(tmp_path, triggers=[{"source": "webhook", "hook": "orders",
                                "correlate": r"ORDER-(\d+)", "semantic": "is urgent"}])
    _park(tmp_path, "l1", "run1")
    claims.register(tmp_path, key="custom:l1:42", automation="l1", run_id="run1")
    rt = FakeRuntime()

    async def judge(condition, summary):
        return False  # would reject a NEW fire, but must not block the wake

    dispatcher = _dispatcher(tmp_path, rt, semantic_judge=judge)

    result = await dispatcher.dispatch("orders", {"text": "update for ORDER-42: shipped"})
    await _drain()

    assert result == {"result": "woken", "automation": "l1", "run_id": "run1"}
    assert rt.answer_calls == [("l1", "run1", "update for ORDER-42: shipped")]
    assert rt.fire_calls == []


async def test_no_claim_and_semantic_false_is_no_match(tmp_path):
    """Companion to the wake-bypasses-semantic fix above: with no claim to
    wake, the semantic condition must still gate a fresh fire."""
    _save(tmp_path, triggers=[{"source": "webhook", "hook": "orders",
                                "correlate": r"ORDER-(\d+)", "semantic": "is urgent"}])
    rt = FakeRuntime()

    async def judge(condition, summary):
        return False

    dispatcher = _dispatcher(tmp_path, rt, semantic_judge=judge)

    result = await dispatcher.dispatch("orders", {"text": "update for ORDER-42: shipped"})
    await _drain()

    assert result == {"result": "no_match"}
    assert rt.fire_calls == []
    assert rt.answer_calls == []


async def test_first_matching_automation_wins_alphabetically(tmp_path):
    _save(tmp_path, name="a-automation")
    _save(tmp_path, name="b-automation")
    rt = FakeRuntime()
    dispatcher = _dispatcher(tmp_path, rt)

    result = await dispatcher.dispatch("orders", {"text": "hi"})
    await _drain()

    assert result == {"result": "fired", "automation": "a-automation"}
    assert len(rt.fire_calls) == 1
