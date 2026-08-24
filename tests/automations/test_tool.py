import asyncio
from pathlib import Path

import pytest

from durin.agent.tools.automations import AutomationsTool
from durin.agent.tools.context import RequestContext, ToolContext
from durin.automations import queue as automation_queue
from durin.automations import run_log as rl
from durin.automations.cron_sync import automation_job_id, sync_automation_jobs
from durin.automations.runtime import AutomationsRuntime
from durin.automations.spec import parse_automation
from durin.automations.store import load_automation, save_automation
from durin.cron.service import CronService
from durin.workflow.result import WorkflowResult


def _cron(tmp_path) -> CronService:
    return CronService(Path(tmp_path) / "cron" / "jobs.json")


def _runtime(tmp_path, results, **kw):
    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None,
                             work_key=None, root_session_key=None):
        return results.pop(0)

    ids = iter([f"ar{i}" for i in range(100)])
    return AutomationsRuntime(
        tmp_path, workflow_exec=workflow_exec, keep_runs=20,
        run_id_factory=lambda: next(ids), **kw,
    )


def _wr(status, **kw):
    return WorkflowResult(status=status, final_output=kw.pop("out", "output"),
                           run_id=kw.pop("run_id", "wf1"), **kw)


def _ctx(tmp_path, runtime=None, cron=None):
    return ToolContext(config=None, workspace=str(tmp_path), cron_service=cron, automations_runtime=runtime)


_DEFINITION = (
    '{"name": "briefing", "workflow": "w1", '
    '"triggers": [{"source": "schedule", "schedule": '
    '{"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}, "task": "send the briefing"}]}'
)


def test_disabled_without_runtime(tmp_path):
    ctx = _ctx(tmp_path, runtime=None)
    assert AutomationsTool.enabled(ctx) is False


def test_enabled_with_runtime(tmp_path):
    ctx = _ctx(tmp_path, runtime=_runtime(tmp_path, []))
    assert AutomationsTool.enabled(ctx) is True


def test_description_states_single_case_doctrine(tmp_path):
    """The single-case doctrine ("chase invoice X" -> one dedicated,
    self-disabling automation for X, never a shared template) must be
    legible from the tool description alone — the agent reads this before
    ever calling the tool."""
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=_runtime(tmp_path, [])))
    assert "single-case" in tool.description.lower()


async def test_create_list_status_flow(tmp_path):
    cron = _cron(tmp_path)
    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt, cron=cron))

    out = await tool.execute(action="create", definition=_DEFINITION)
    assert "Created automation 'briefing'" in out
    assert "workflow: w1" in out

    out = await tool.execute(action="list")
    assert "briefing" in out and "enabled" in out

    out = await tool.execute(action="status", name="briefing")
    assert "Automation 'briefing'" in out
    assert "Triggers: 1" in out
    assert "No runs yet." in out


async def test_create_validates_and_persists_via_store(tmp_path):
    """create's own reply is not proof of a save — read the definition
    back through the store, the same way a later fire/status/list would."""
    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="create", definition='{"name": "a1", "workflow": "w1"}')
    assert out.startswith("Created automation 'a1'")

    spec = load_automation(tmp_path, "a1")
    assert spec.name == "a1"
    assert spec.workflow == "w1"
    assert spec.enabled is True


async def test_create_name_param_overrides_definition_name(tmp_path):
    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="create", name="overridden", definition='{"name": "original", "workflow": "w1"}')
    assert "Created automation 'overridden'" in out
    spec = load_automation(tmp_path, "overridden")
    assert spec.name == "overridden"


async def test_fire_delegates_to_runtime_and_returns_status_text(tmp_path):
    """The tool call itself only reports the launch; the terminal status
    lands in the run log once the backgrounded fire completes."""
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))
    rt = _runtime(tmp_path, [_wr("completed")])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="fire", name="a1", task="do it")
    assert "a1" in out
    assert "started" in out.lower()

    # Wait for the backgrounded fire's task directly rather than sleep(0):
    # sleep(0) only yields long enough to work because the fakes here never
    # actually suspend — gather is correct regardless of how many awaits the
    # fire path takes internally.
    await asyncio.gather(*tool._fires)
    run = rl.list_runs(tmp_path, "a1", limit=1)[0]
    assert run["status"] == "completed"


async def test_answer_resumes_run(tmp_path):
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))
    rt = _runtime(tmp_path, [
        _wr("needs_input", out="need more info", ask_kind="question"),
        _wr("completed"),
    ])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    await tool.execute(action="fire", name="a1")
    await asyncio.gather(*tool._fires)  # let the backgrounded fire reach 'paused'
    run_id = rl.list_runs(tmp_path, "a1", limit=1)[0]["run_id"]

    out = await tool.execute(action="answer", name="a1", run_id=run_id, answer="here's the info")
    assert "completed" in out


async def test_answer_forwards_resolution_and_by_agent(tmp_path):
    """An explicit resolution bypasses keyword parsing of the free-text
    answer and must be attributed to the agent (the chat surface that
    answered), not to a human operator — verified against the run's own
    recorded approval, not a mock."""
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))
    rt = _runtime(tmp_path, [
        _wr("needs_input", out="approve this?", ask_kind="approval"),
        _wr("completed"),
    ])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    await tool.execute(action="fire", name="a1")
    await asyncio.gather(*tool._fires)
    run_id = rl.list_runs(tmp_path, "a1", limit=1)[0]["run_id"]

    out = await tool.execute(
        action="answer", name="a1", run_id=run_id,
        answer="whatever, ignored by an explicit resolution", resolution="approve",
    )

    assert "completed" in out
    record = rl.read_run(tmp_path, "a1", run_id)
    assert record["approval"]["action"] == "approve"
    assert record["approval"]["by"] == "agent"


async def test_pause_syncs_cron_jobs_off(tmp_path):
    cron = _cron(tmp_path)
    spec = parse_automation({
        "name": "briefing", "workflow": "w1",
        "triggers": [{"source": "schedule", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                       "task": "send the briefing"}],
    })
    save_automation(tmp_path, spec)
    sync_automation_jobs(cron, spec)
    assert cron.get_job(automation_job_id("briefing", 0)) is not None

    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt, cron=cron))

    out = await tool.execute(action="pause", name="briefing")

    assert "paused" in out.lower()
    assert cron.get_job(automation_job_id("briefing", 0)) is None


async def test_enable_syncs_cron_jobs_on(tmp_path):
    cron = _cron(tmp_path)
    spec = parse_automation({
        "name": "briefing", "workflow": "w1", "enabled": False,
        "triggers": [{"source": "schedule", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
                       "task": "send the briefing"}],
    })
    save_automation(tmp_path, spec)
    assert cron.get_job(automation_job_id("briefing", 0)) is None

    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt, cron=cron))

    out = await tool.execute(action="enable", name="briefing")
    assert "enabled" in out.lower()
    assert cron.get_job(automation_job_id("briefing", 0)) is not None


async def test_pause_without_cron_service_still_saves(tmp_path):
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))
    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt, cron=None))

    out = await tool.execute(action="pause", name="a1")

    assert "paused" in out.lower()
    assert load_automation(tmp_path, "a1").enabled is False


async def test_create_invalid_json_returns_readable_error(tmp_path):
    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="create", definition="not json")
    assert out.startswith("Error:")
    assert "Traceback" not in out


async def test_create_invalid_definition_returns_readable_error(tmp_path):
    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="create", definition='{"name": "Bad Name!"}')
    assert out.startswith("Error:")
    assert "Traceback" not in out


async def test_create_rejects_a_chain_cycle_at_save_time(tmp_path):
    """parse_automation alone cannot see sibling automations, so a
    self-referential chain trigger only surfaces as an error once
    save_automation's cross-automation cycle check runs — the tool must
    catch that too, not just parse-time errors."""
    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(
        action="create",
        definition='{"name": "a1", "workflow": "w1", '
                   '"triggers": [{"source": "chain", "chain_automation": "a1"}]}',
    )
    assert out.startswith("Error:")
    assert "cycle" in out.lower()
    assert "Traceback" not in out


async def test_status_shows_awaiting_answer_and_queued_counts(tmp_path):
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))
    rt = _runtime(tmp_path, [_wr("needs_input", out="need more info", ask_kind="question")])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))
    automation_queue.push(tmp_path, "a1", {"content": "queued event"})

    await rt.fire("a1", source="channel", origin={"thread": "t1", "channel": "test"})

    out = await tool.execute(action="status", name="a1")
    assert "1 awaiting an answer" in out
    assert "Queued events: 1" in out


async def test_status_unknown_automation_returns_readable_error(tmp_path):
    rt = _runtime(tmp_path, [])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))

    out = await tool.execute(action="status", name="nope")
    assert out.startswith("Error:")


@pytest.mark.asyncio
async def test_chat_fire_records_the_firing_session_as_origin(tmp_path):
    """Without this the outcome has nowhere to go back to."""
    rt = _runtime(tmp_path, [_wr("completed")])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))
    tool.set_context(RequestContext(
        channel="websocket", chat_id="abc", session_key="websocket:abc"))
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))

    await tool.execute(action="fire", name="a1")
    await asyncio.gather(*tool._fires)  # let the backgrounded fire record its origin

    run = rl.list_runs(tmp_path, "a1", limit=1)[0]
    assert run["origin"] == {
        "kind": "session", "session_key": "websocket:abc",
        "channel": "websocket", "chat_id": "abc",
    }


@pytest.mark.asyncio
async def test_chat_fire_without_a_context_records_no_origin(tmp_path):
    """A surface that never injects a context must fall through to the
    automation's declared destination, not to a half-built origin."""
    rt = _runtime(tmp_path, [_wr("completed")])
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))

    await tool.execute(action="fire", name="a1")
    await asyncio.gather(*tool._fires)  # let the backgrounded fire record its origin

    assert rl.list_runs(tmp_path, "a1", limit=1)[0]["origin"] is None


@pytest.mark.asyncio
async def test_fire_returns_before_the_workflow_finishes(tmp_path):
    """The automation is read, not driven: the agent must not hold its turn
    open for the length of the run."""
    released = asyncio.Event()

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None,
                             work_key=None, root_session_key=None):
        await released.wait()
        return _wr("completed", run_id=run_id)

    ids = iter([f"ar{i}" for i in range(10)])
    rt = AutomationsRuntime(tmp_path, workflow_exec=workflow_exec, keep_runs=20,
                            run_id_factory=lambda: next(ids))
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))

    out = await asyncio.wait_for(tool.execute(action="fire", name="a1"), timeout=1.0)

    assert "started" in out.lower()
    assert "ar0" in out
    released.set()
    await asyncio.gather(*tool._fires)


@pytest.mark.asyncio
async def test_busy_automation_still_reports_synchronously(tmp_path):
    """AutomationBusy is known before anything is launched — it must not be
    swallowed into the background."""
    rt = _runtime(tmp_path, [_wr("completed")])
    save_automation(tmp_path, parse_automation({
        "name": "a1", "workflow": "w1", "concurrency": "single"}))
    rl.start_run(tmp_path, "a1", "existing", cause={"kind": "cron", "excerpt": "t"})

    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))
    out = await tool.execute(action="fire", name="a1")

    assert "busy" in out.lower()
    assert "Traceback" not in out


@pytest.mark.asyncio
async def test_a_chat_fire_that_loses_the_race_is_retracted_to_the_session(tmp_path):
    """The tool's reply tells the agent the outcome arrives as a follow-up and
    to stop polling. When the backgrounded fire loses the concurrency race the
    run never exists, so no outcome is ever emitted for it — the agent waits
    forever on a message that only a log line records. The retraction has to
    travel the same delivery path the outcome would have."""
    started = asyncio.Event()
    released = asyncio.Event()
    outcomes = []

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None,
                             work_key=None, root_session_key=None):
        started.set()
        await released.wait()
        return _wr("completed", run_id=run_id)

    async def on_outcome(outcome):
        outcomes.append(outcome)

    ids = iter([f"ar{i}" for i in range(10)])
    rt = AutomationsRuntime(tmp_path, workflow_exec=workflow_exec, keep_runs=20,
                            run_id_factory=lambda: next(ids), on_outcome=on_outcome)
    tool = AutomationsTool.create(_ctx(tmp_path, runtime=rt))
    tool.set_context(RequestContext(
        channel="websocket", chat_id="abc", session_key="websocket:abc"))
    save_automation(tmp_path, parse_automation({"name": "a1", "workflow": "w1"}))

    await tool.execute(action="fire", name="a1")
    await tool.execute(action="fire", name="a1")   # loses the race inside the runtime
    pending = set(tool._fires)
    await started.wait()
    released.set()
    await asyncio.gather(*pending)

    retractions = [o for o in outcomes if o.run_id == "ar1"]
    assert len(retractions) == 1, f"ar1 was announced and never retracted: {outcomes}"
    assert "produced no outcome" in retractions[0].summary
    # Routed back to the session that asked, exactly like a real outcome.
    assert retractions[0].origin == {
        "kind": "session", "session_key": "websocket:abc",
        "channel": "websocket", "chat_id": "abc",
    }
