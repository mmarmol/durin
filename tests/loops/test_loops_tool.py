from pathlib import Path

from durin.agent.tools.context import ToolContext
from durin.agent.tools.loops import LoopsTool
from durin.cron.service import CronService
from durin.loops.cron_sync import loop_job_id, sync_loop_jobs
from durin.loops.runtime import LoopsRuntime
from durin.loops.spec import parse_loop
from durin.loops.store import save_loop
from durin.workflow.result import WorkflowResult


def _cron(tmp_path) -> CronService:
    return CronService(Path(tmp_path) / "cron" / "jobs.json")


def _runtime(tmp_path, results):
    async def workflow_exec(name, task, *, resume_run_id=None):
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
    save_loop(tmp_path, parse_loop({"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))
    rt = _runtime(tmp_path, [_wr("completed")])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="fire", name="l1", task="do it")
    assert "l1" in out
    assert "done" in out
    assert "goal_reached: True" in out


async def test_fire_busy_returns_readable_message_not_traceback(tmp_path):
    save_loop(tmp_path, parse_loop({"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))
    rt = _runtime(tmp_path, [_wr("needs_input", out="q?", needs_input_node="g")])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    await tool.execute(action="fire", name="l1")
    out = await tool.execute(action="fire", name="l1")

    assert "busy" in out.lower()
    assert "Traceback" not in out


async def test_answer_resumes_run(tmp_path):
    save_loop(tmp_path, parse_loop({"name": "l1", "workflow": "w1", "goal": {"intent": "done"}}))
    rt = _runtime(tmp_path, [_wr("needs_input", out="approve?", needs_input_node="g"), _wr("completed")])
    tool = LoopsTool.create(_ctx(tmp_path, runtime=rt))

    fired = await tool.execute(action="fire", name="l1")
    run_id = fired.split("run ")[1].split(":")[0]

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
