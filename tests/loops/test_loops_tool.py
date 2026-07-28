import asyncio
from pathlib import Path

import pytest

from durin.agent.tools.context import RequestContext, ToolContext
from durin.agent.tools.loops import LoopsTool
from durin.cron.service import CronService
from durin.loops import run_log as rl
from durin.loops.cron_sync import loop_job_id, sync_loop_jobs
from durin.loops.runtime import LoopsRuntime
from durin.loops.spec import parse_loop
from durin.loops.store import save_loop
from durin.workflow.result import WorkflowResult


def _cron(tmp_path) -> CronService:
    return CronService(Path(tmp_path) / "cron" / "jobs.json")


def _runtime(tmp_path, results):
    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None):
        return results.pop(0)

    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {a: True for a in assertions}}

    ids = iter([f"lr{i}" for i in range(100)])
    return LoopsRuntime(
        tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
        check_timeout_s=5, run_id_factory=lambda: next(ids),
    )


def _wr(status, **kw):
    return WorkflowResult(status=status, final_output=kw.pop("out", "output"),
                           run_id=kw.pop("run_id", "wf1"), **kw)


def _ctx(tmp_path, runtime=None, cron=None):
    return ToolContext(config=None, workspace=str(tmp_path), cron_service=cron, loops_runtime=runtime)


_DEFINITION = (
    '{"name": "briefing", "workflow": "w1", "goal": {"intent": "briefed"}, '
    '"triggers": [{"source": "cron", "schedule": '
    '{"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}}]}'
)


def test_disabled_without_runtime(tmp_path):
    ctx = _ctx(tmp_path, runtime=None)
    assert LoopsTool.enabled(ctx) is False


def test_enabled_with_runtime(tmp_path):
    ctx = _ctx(tmp_path, runtime=_runtime(tmp_path, []))
    assert LoopsTool.enabled(ctx) is True


async def test_create_list_status_flow(tmp_path):
    cron = _cron(tmp_path)
    rt = _runtime(tmp_path, [])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt, cron=cron))

    out = await tool.execute(action="create", definition=_DEFINITION)
    assert "Created loop 'briefing'" in out
    assert "workflow: w1" in out

    out = await tool.execute(action="list")
    assert "briefing" in out and "enabled" in out

    out = await tool.execute(action="status", name="briefing")
    assert "Loop 'briefing'" in out
    assert "Goal: briefed" in out
    assert "No runs yet." in out


async def test_fire_delegates_to_runtime_and_returns_status_text(tmp_path):
    """The tool call itself only reports the launch; the terminal status
    lands in the run log once the backgrounded fire completes."""
    save_loop(tmp_path, parse_loop({"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))
    rt = _runtime(tmp_path, [_wr("completed")])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="fire", name="l1", task="do it")
    assert "l1" in out
    assert "started" in out.lower()

    await asyncio.sleep(0)  # let the backgrounded fire run to completion
    run = rl.list_runs(tmp_path, "l1", limit=1)[0]
    assert run["status"] == "done"
    assert run["goal_reached"] is True


async def test_fire_busy_returns_readable_message_not_traceback(tmp_path):
    save_loop(tmp_path, parse_loop({"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))
    rt = _runtime(tmp_path, [_wr("needs_input", out="q?", needs_input_node="g")])
    # Seed an active run directly: a backgrounded fire has not yet recorded
    # its own run_log entry by the time it returns, so a second immediate
    # fire can no longer rely on the first call's side effects for busy.
    rl.start_run(tmp_path, "l1", "existing", source="cron", task="t")
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="fire", name="l1")

    assert "busy" in out.lower()
    assert "Traceback" not in out


async def test_answer_resumes_run(tmp_path):
    save_loop(tmp_path, parse_loop({"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))
    rt = _runtime(tmp_path, [_wr("needs_input", out="approve?", needs_input_node="g"), _wr("completed")])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    await tool.execute(action="fire", name="l1")
    await asyncio.sleep(0)  # let the backgrounded fire reach needs_operator
    run_id = rl.list_runs(tmp_path, "l1", limit=1)[0]["run_id"]

    out = await tool.execute(action="answer", name="l1", run_id=run_id, answer="yes")
    assert "done" in out


async def test_pause_syncs_cron_jobs_off(tmp_path):
    cron = _cron(tmp_path)
    spec = parse_loop({
        "name": "briefing", "workflow": "w1", "goal": {"intent": "briefed"},
        "triggers": [{"source": "cron", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}}],
    })
    save_loop(tmp_path, spec)
    sync_loop_jobs(cron, spec)
    assert cron.get_job(loop_job_id("briefing", 0)) is not None

    rt = _runtime(tmp_path, [])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt, cron=cron))

    out = await tool.execute(action="pause", name="briefing")

    assert "paused" in out.lower()
    assert cron.get_job(loop_job_id("briefing", 0)) is None


async def test_enable_syncs_cron_jobs_on(tmp_path):
    cron = _cron(tmp_path)
    spec = parse_loop({
        "name": "briefing", "workflow": "w1", "goal": {"intent": "briefed"}, "enabled": False,
        "triggers": [{"source": "cron", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}}],
    })
    save_loop(tmp_path, spec)
    assert cron.get_job(loop_job_id("briefing", 0)) is None

    rt = _runtime(tmp_path, [])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt, cron=cron))

    out = await tool.execute(action="enable", name="briefing")
    assert "enabled" in out.lower()
    assert cron.get_job(loop_job_id("briefing", 0)) is not None


async def test_create_invalid_json_returns_readable_error(tmp_path):
    rt = _runtime(tmp_path, [])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="create", definition="not json")
    assert out.startswith("Error:")
    assert "Traceback" not in out


async def test_create_invalid_definition_returns_loop_error_message(tmp_path):
    rt = _runtime(tmp_path, [])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="create", definition='{"name": "Bad Name!"}')
    assert out.startswith("Error:")
    assert "Traceback" not in out


async def test_status_shows_waiting_info_and_queued_counts(tmp_path):
    from durin.loops import queue

    save_loop(tmp_path, parse_loop({"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))
    rt = _runtime(tmp_path, [_wr("needs_input", out="[TO:counterpart] need more info", needs_input_node="g")])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))
    queue.push(tmp_path, "l1", {"content": "queued event"})

    await rt.fire("l1", source="channel", origin={"thread": "t1", "channel": "test"})

    out = await tool.execute(action="status", name="l1")
    assert "1 waiting_info" in out
    assert "Queued events: 1" in out


async def test_status_unknown_loop_returns_readable_error(tmp_path):
    rt = _runtime(tmp_path, [])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="status", name="nope")
    assert out.startswith("Error:")


@pytest.mark.asyncio
async def test_chat_fire_records_the_firing_session_as_origin(tmp_path):
    """Without this the outcome has nowhere to go back to."""
    rt = _runtime(tmp_path, [_wr("completed")])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))
    tool.set_context(RequestContext(
        channel="websocket", chat_id="abc", session_key="websocket:abc"))
    save_loop(tmp_path, parse_loop(
        {"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))

    await tool.execute(action="fire", name="l1")
    await asyncio.sleep(0)  # let the backgrounded fire record its origin

    run = rl.list_runs(tmp_path, "l1", limit=1)[0]
    assert run["origin"] == {
        "kind": "session", "session_key": "websocket:abc",
        "channel": "websocket", "chat_id": "abc",
    }


@pytest.mark.asyncio
async def test_chat_fire_without_a_context_records_no_origin(tmp_path):
    """A surface that never injects a context must fall through to the
    loop's declared destination, not to a half-built origin."""
    rt = _runtime(tmp_path, [_wr("completed")])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))
    save_loop(tmp_path, parse_loop(
        {"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))

    await tool.execute(action="fire", name="l1")
    await asyncio.sleep(0)  # let the backgrounded fire record its origin

    assert rl.list_runs(tmp_path, "l1", limit=1)[0]["origin"] is None


@pytest.mark.asyncio
async def test_fire_returns_before_the_workflow_finishes(tmp_path):
    """The loop is read, not driven: the agent must not hold its turn open
    for the length of the run."""
    import asyncio

    released = asyncio.Event()

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None):
        await released.wait()
        return _wr("completed", run_id=run_id)

    async def judge(intent, assertions, evidence):
        return {"intent_met": True, "assertions": {}}

    ids = iter([f"lr{i}" for i in range(10)])
    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge,
                      keep_runs=20, check_timeout_s=5, run_id_factory=lambda: next(ids))
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))
    save_loop(tmp_path, parse_loop(
        {"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))

    out = await asyncio.wait_for(tool.execute(action="fire", name="l1"), timeout=1.0)

    assert "started" in out.lower()
    assert "lr0" in out
    released.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_busy_loop_still_reports_synchronously(tmp_path):
    """LoopBusy is known before anything is launched — it must not be
    swallowed into the background."""
    rt = _runtime(tmp_path, [_wr("completed")])
    save_loop(tmp_path, parse_loop({
        "name": "l1", "workflow": "w1", "goal": {"intent": "done"},
        "concurrency": "single"}))
    rl.start_run(tmp_path, "l1", "existing", source="cron", task="t")

    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))
    out = await tool.execute(action="fire", name="l1")

    assert "busy" in out.lower()
