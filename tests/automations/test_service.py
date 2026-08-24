"""Tests for AutomationsService (list / get / save / delete / fire / answer / runs).

Built the way tests/service/test_loops.py is: same route/scope/error shape,
adapted to automations' own vocabulary (achieved/completed/failed/rejected/
paused, not loops' done/no_goal/escalated/waiting_info) and run_log/runtime
call signatures.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from durin.automations import queue as automation_queue
from durin.automations import run_log as automation_run_log
from durin.automations.runtime import AutomationsRuntime
from durin.automations.spec import automation_to_dict, parse_automation
from durin.cron.service import CronService
from durin.service.automations import (
    AutomationAnswerCommand,
    AutomationDeleteCommand,
    AutomationFireCommand,
    AutomationGetQuery,
    AutomationRunsQuery,
    AutomationSaveCommand,
    AutomationsListQuery,
    AutomationsRunsQuery,
    AutomationsService,
    AutomationStopCommand,
)
from durin.service.principal import Principal, Scope
from durin.service.types import (
    ForbiddenError,
    NotFoundError,
    UnavailableError,
    ValidationFailedError,
)
from durin.workflow.result import WorkflowResult

_VALID = {"name": "a1", "workflow": "w1"}


def _cron(tmp_path) -> CronService:
    return CronService(Path(tmp_path) / "cron" / "jobs.json")


def _svc(tmp_path, *, runtime=None) -> AutomationsService:
    return AutomationsService(workspace=tmp_path, cron_service=_cron(tmp_path), runtime=runtime)


def _runtime(tmp_path, results):
    calls = {"exec": []}

    async def workflow_exec(name, task, *, resume_run_id=None, run_id=None,
                             work_key=None, root_session_key=None):
        calls["exec"].append((name, task, resume_run_id))
        return results.pop(0)

    ids = iter([f"ar{i}" for i in range(100)])
    rt = AutomationsRuntime(tmp_path, workflow_exec=workflow_exec, keep_runs=20,
                             run_id_factory=lambda: next(ids))
    return rt, calls


def _wr(status, **kw):
    return WorkflowResult(status=status, final_output=kw.pop("out", "output"),
                          run_id=kw.pop("run_id", "wf1"), **kw)


def _seed_run(tmp_path, name, run_id, status, started_at=0.0):
    """Write a run record directly to run_log, bypassing the runtime, so tests
    can seed an arbitrary mix of statuses/timestamps for the list route's live
    counts and life state (attempts/achieved/stuck)."""
    automation_run_log.start_run(tmp_path, name, run_id, cause={"kind": "manual", "excerpt": ""})
    automation_run_log.update_run(tmp_path, name, run_id, status=status, started_at=started_at)


# --- save / list / get / delete round trip ----------------------------------


@pytest.mark.asyncio
async def test_save_list_get_round_trip_with_counts_and_life_state(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)

    listed = (await svc.list(AutomationsListQuery(), p)).automations
    assert len(listed) == 1
    row = listed[0]
    assert row["name"] == "a1"
    assert row["workflow"] == "w1"
    assert row["active_runs"] == 0
    assert row["paused"] == 0
    assert row["pending_events"] == 0
    assert row["attempts"] == 0
    assert row["achieved"] is False
    assert row["stuck"] is False

    got = await svc.get(AutomationGetQuery(name="a1"), p)
    assert got.name == "a1"
    assert got.definition["workflow"] == "w1"
    assert (tmp_path / "automations" / "a1.json").is_file()


@pytest.mark.asyncio
async def test_list_row_shape_is_definition_plus_exactly_six_extra_keys(tmp_path):
    """The list row's shape is `{**definition, active_runs, paused, pending_events,
    attempts, achieved, stuck}` — nothing more, nothing less."""
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)

    row = (await svc.list(AutomationsListQuery(), p)).automations[0]
    definition_keys = set(automation_to_dict(parse_automation(_VALID)))
    assert set(row) - definition_keys == {
        "active_runs", "paused", "pending_events", "attempts", "achieved", "stuck",
    }


@pytest.mark.asyncio
async def test_save_uses_the_url_name_over_a_mismatched_body_name(tmp_path):
    """The URL is authoritative for the automation's identity — same precedent
    as WorkflowsService.duplicate() overwriting the inner "name" field."""
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition={**_VALID, "name": "other"}), p)
    assert (await svc.list(AutomationsListQuery(), p)).automations[0]["name"] == "a1"
    assert not (tmp_path / "automations" / "other.json").exists()


@pytest.mark.asyncio
async def test_save_rejects_an_invalid_automation(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    with pytest.raises(ValidationFailedError):
        await svc.save(AutomationSaveCommand(name="bad", definition={"name": "bad"}), p)
    assert not (tmp_path / "automations" / "bad.json").exists()


@pytest.mark.asyncio
async def test_save_rejects_a_chain_cycle(tmp_path):
    """save_automation's own chain-cycle validation (durin.automations.chains)
    raises the same AutomationError as a parse-time error, via the one except
    clause in AutomationsService.save."""
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition={
        **_VALID, "triggers": [{"source": "chain", "chain_automation": "a2"}]}), p)

    with pytest.raises(ValidationFailedError):
        await svc.save(AutomationSaveCommand(name="a2", definition={
            "name": "a2", "workflow": "w2",
            "triggers": [{"source": "chain", "chain_automation": "a1"}]}), p)
    assert not (tmp_path / "automations" / "a2.json").exists()


@pytest.mark.asyncio
async def test_get_missing_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).get(AutomationGetQuery(name="ghost"), Principal.local())


@pytest.mark.asyncio
async def test_delete_missing_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).delete(AutomationDeleteCommand(name="ghost"), Principal.local())


@pytest.mark.asyncio
async def test_save_then_delete_round_trip(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    await svc.delete(AutomationDeleteCommand(name="a1"), p)
    assert (await svc.list(AutomationsListQuery(), p)).automations == []


@pytest.mark.asyncio
async def test_save_registers_a_cron_job_and_delete_removes_it(tmp_path):
    triggered = {**_VALID, "triggers": [
        {"source": "schedule", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
         "task": "run it"},
    ]}
    cron = _cron(tmp_path)
    svc, p = AutomationsService(workspace=tmp_path, cron_service=cron), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=triggered), p)
    from durin.automations.cron_sync import automation_job_id
    assert cron.get_job(automation_job_id("a1", 0)) is not None

    await svc.delete(AutomationDeleteCommand(name="a1"), p)
    assert cron.get_job(automation_job_id("a1", 0)) is None


# --- fire --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_without_a_runtime_is_unavailable(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    with pytest.raises(UnavailableError):
        await svc.fire(AutomationFireCommand(name="a1"), p)


@pytest.mark.asyncio
async def test_fire_runs_the_automation_and_returns_the_record(tmp_path):
    rt, calls = _runtime(tmp_path, [_wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)

    result = await svc.fire(AutomationFireCommand(name="a1"), p)
    assert result.run["status"] == "completed"
    assert calls["exec"][0][0] == "w1"

    listed = (await svc.list(AutomationsListQuery(), p)).automations[0]
    assert listed["active_runs"] == 0


@pytest.mark.asyncio
async def test_fire_busy_raises_validation_error(tmp_path):
    rt, _ = _runtime(tmp_path, [_wr("needs_input", out="q?", needs_input_node="g")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    await svc.fire(AutomationFireCommand(name="a1"), p)   # leaves an active paused run

    with pytest.raises(ValidationFailedError):
        await svc.fire(AutomationFireCommand(name="a1"), p)


@pytest.mark.asyncio
async def test_fire_missing_automation_raises_not_found(tmp_path):
    rt, _ = _runtime(tmp_path, [])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    with pytest.raises(NotFoundError):
        await svc.fire(AutomationFireCommand(name="ghost"), p)


@pytest.mark.asyncio
async def test_fire_with_no_task_falls_back_to_the_first_schedule_triggers_task(tmp_path):
    """F1c: the webui's "Run now" button never asks for a task — a scheduled
    automation fired manually with none must still get the same prompt its
    clock trigger would have sent, not run with no task at all."""
    rt, calls = _runtime(tmp_path, [_wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    definition = {**_VALID, "triggers": [
        {"source": "schedule", "schedule": {"kind": "cron", "expr": "0 7 * * *"}, "task": "run the digest"},
    ]}
    await svc.save(AutomationSaveCommand(name="a1", definition=definition), p)

    await svc.fire(AutomationFireCommand(name="a1"), p)

    assert calls["exec"][0][1] == "run the digest"


@pytest.mark.asyncio
async def test_fire_with_an_explicit_task_never_falls_back(tmp_path):
    rt, calls = _runtime(tmp_path, [_wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    definition = {**_VALID, "triggers": [
        {"source": "schedule", "schedule": {"kind": "cron", "expr": "0 7 * * *"}, "task": "scheduled task"},
    ]}
    await svc.save(AutomationSaveCommand(name="a1", definition=definition), p)

    await svc.fire(AutomationFireCommand(name="a1", task="explicit task"), p)

    assert calls["exec"][0][1] == "explicit task"


@pytest.mark.asyncio
async def test_fire_with_no_task_and_no_schedule_trigger_synthesizes_a_run_task(tmp_path):
    """E3: a trigger-less automation fired with no explicit task must never
    reach the workflow with task=None — the node runner renders a None task
    as the literal string "None" in the draft node's user message. Falls back
    to the same "Run the <workflow> workflow" synthesis the loops->automations
    migration uses for a cron trigger with no task text of its own."""
    rt, calls = _runtime(tmp_path, [_wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)  # no triggers at all

    await svc.fire(AutomationFireCommand(name="a1"), p)

    assert calls["exec"][0][1] == "Run the w1 workflow"


# --- answer ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_returns_running_immediately_then_resumes_in_the_background(tmp_path):
    """The route must not block for the whole resume: it returns the record
    re-read as `running` right away, and the actual resume — same workflow
    call a blocking answer would have made — completes afterward."""
    import asyncio

    rt, calls = _runtime(tmp_path, [
        _wr("needs_input", out="which env?", needs_input_node="gate"), _wr("completed"),
    ])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    fired = await svc.fire(AutomationFireCommand(name="a1"), p)
    run_id = fired.run["run_id"]

    result = await svc.answer(AutomationAnswerCommand(name="a1", run_id=run_id, text="prod"), p)
    assert result.run["status"] == "running"
    assert len(calls["exec"]) == 1   # the resume hasn't run yet

    # Wait for the backgrounded continuation directly rather than sleep(0):
    # sleep(0) only works because this file's fake workflow_exec never
    # actually suspends — gather is correct regardless of how many awaits
    # the resume path takes internally (mirrors test_tool.py's own fix for
    # the identical pattern).
    await asyncio.gather(*rt._bg_tasks)

    # AutomationsRuntime mints and persists its own workflow_run_id at fire time
    # (run_id_factory's SECOND draw, "ar1" — the first, "ar0", is the automation's
    # own run_id) and resumes with THAT id, independent of whatever run_id the
    # (fake) workflow result object itself carries.
    assert calls["exec"][1] == ("w1", "prod", "ar1")
    final = automation_run_log.read_run(tmp_path, "a1", run_id)
    assert final["status"] == "completed"


@pytest.mark.asyncio
async def test_answer_with_explicit_action_bypasses_keyword_parsing(tmp_path):
    """An explicit `action` (webui buttons) rides through as the canonical resume
    text regardless of what `text` says — see AutomationsRuntime._answer_prologue.
    The approval verdict is recorded in the prologue, so it is already on the
    record returned immediately, even though the resume itself is backgrounded."""
    import asyncio

    rt, calls = _runtime(tmp_path, [
        _wr("needs_input", out="proceed?", needs_input_node="gate", ask_kind="approval"),
        _wr("completed"),
    ])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    fired = await svc.fire(AutomationFireCommand(name="a1"), p)
    run_id = fired.run["run_id"]

    result = await svc.answer(
        AutomationAnswerCommand(name="a1", run_id=run_id, text="whatever, ignored", action="approve"), p)
    assert result.run["status"] == "running"
    assert result.run["approval"]["action"] == "approve"
    assert result.run["approval"]["by"] == "operator"

    # Wait for the backgrounded continuation directly rather than sleep(0) —
    # see the identical comment above.
    await asyncio.gather(*rt._bg_tasks)
    assert calls["exec"][1][1] == "approve"   # not "whatever, ignored"


@pytest.mark.asyncio
async def test_answer_of_a_non_waiting_run_raises_validation_error(tmp_path):
    rt, _ = _runtime(tmp_path, [_wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    fired = await svc.fire(AutomationFireCommand(name="a1"), p)   # already terminal

    with pytest.raises(ValidationFailedError):
        await svc.answer(AutomationAnswerCommand(name="a1", run_id=fired.run["run_id"], text="yes"), p)


@pytest.mark.asyncio
async def test_answer_without_a_runtime_is_unavailable(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    with pytest.raises(UnavailableError):
        await svc.answer(AutomationAnswerCommand(name="a1", run_id="r1", text="yes"), p)


@pytest.mark.asyncio
async def test_answer_missing_automation_raises_not_found(tmp_path):
    rt, _ = _runtime(tmp_path, [])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    with pytest.raises(NotFoundError):
        await svc.answer(AutomationAnswerCommand(name="ghost", run_id="r1", text="yes"), p)


@pytest.mark.asyncio
async def test_stop_paused_run_finalizes_interrupted(tmp_path):
    rt, _ = _runtime(tmp_path, [
        _wr("needs_input", out="which env?", needs_input_node="gate"),
    ])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    fired = await svc.fire(AutomationFireCommand(name="a1"), p)
    run_id = fired.run["run_id"]

    result = await svc.stop(AutomationStopCommand(name="a1", run_id=run_id), p)
    assert result.run["status"] == "interrupted"
    assert result.run["detail"] == "stopped by operator"


@pytest.mark.asyncio
async def test_stop_of_a_terminal_run_raises_validation_error(tmp_path):
    rt, _ = _runtime(tmp_path, [_wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    fired = await svc.fire(AutomationFireCommand(name="a1"), p)   # already terminal

    with pytest.raises(ValidationFailedError):
        await svc.stop(AutomationStopCommand(name="a1", run_id=fired.run["run_id"]), p)


@pytest.mark.asyncio
async def test_stop_without_a_runtime_is_unavailable(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    with pytest.raises(UnavailableError):
        await svc.stop(AutomationStopCommand(name="a1", run_id="r1"), p)


@pytest.mark.asyncio
async def test_stop_missing_run_raises_not_found(tmp_path):
    rt, _ = _runtime(tmp_path, [])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    with pytest.raises(NotFoundError):
        await svc.stop(AutomationStopCommand(name="a1", run_id="ghost-run"), p)


# --- list: live counts + life state ------------------------------------------


@pytest.mark.asyncio
async def test_list_counts_active_paused_and_pending_events(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    _seed_run(tmp_path, "a1", "r1", "running", 1.0)
    _seed_run(tmp_path, "a1", "r2", "paused", 2.0)
    automation_queue.push(tmp_path, "a1", {"content": "queued event"})

    listed = (await svc.list(AutomationsListQuery(), p)).automations[0]
    assert listed["active_runs"] == 1
    assert listed["paused"] == 1
    assert listed["pending_events"] == 1


@pytest.mark.asyncio
async def test_list_attempts_counts_the_consecutive_unachieved_streak(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    _seed_run(tmp_path, "a1", "r1", "failed", 1.0)
    _seed_run(tmp_path, "a1", "r2", "completed", 2.0)

    listed = (await svc.list(AutomationsListQuery(), p)).automations[0]
    assert listed["attempts"] == 2
    assert listed["achieved"] is False
    assert listed["stuck"] is False


@pytest.mark.asyncio
async def test_list_achieved_true_only_when_latest_terminal_run_achieved_and_disabled(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    _seed_run(tmp_path, "a1", "r1", "achieved", 1.0)

    # An achieved run alone is not enough: AutomationsRuntime._disable only
    # flips enabled=False AFTER delivering/dispatching, so "achieved" reads
    # true only once both conditions hold.
    listed = (await svc.list(AutomationsListQuery(), p)).automations[0]
    assert listed["achieved"] is False

    await svc.save(AutomationSaveCommand(name="a1", definition={**_VALID, "enabled": False}), p)
    listed = (await svc.list(AutomationsListQuery(), p)).automations[0]
    assert listed["achieved"] is True


@pytest.mark.asyncio
async def test_list_stuck_true_once_attempts_reaches_max_attempts(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    definition = {**_VALID, "life": {"intent": "keep the inbox at zero", "max_attempts": 2}}
    await svc.save(AutomationSaveCommand(name="a1", definition=definition), p)
    _seed_run(tmp_path, "a1", "r1", "failed", 1.0)
    _seed_run(tmp_path, "a1", "r2", "failed", 2.0)

    listed = (await svc.list(AutomationsListQuery(), p)).automations[0]
    assert listed["attempts"] == 2
    assert listed["stuck"] is True


@pytest.mark.asyncio
async def test_list_stuck_false_without_life_configured_regardless_of_attempts(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    for i in range(5):
        _seed_run(tmp_path, "a1", f"r{i}", "failed", float(i))

    listed = (await svc.list(AutomationsListQuery(), p)).automations[0]
    assert listed["attempts"] == 5
    assert listed["stuck"] is False


# --- runs: per-automation + global feed --------------------------------------


@pytest.mark.asyncio
async def test_runs_list_and_global_feed_shape(tmp_path):
    rt, _ = _runtime(tmp_path, [_wr("completed"), _wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)
    await svc.save(AutomationSaveCommand(name="a2", definition={**_VALID, "name": "a2"}), p)
    await svc.fire(AutomationFireCommand(name="a1"), p)
    await svc.fire(AutomationFireCommand(name="a2"), p)

    per_automation = await svc.runs_list(AutomationRunsQuery(name="a1"), p)
    assert len(per_automation.runs) == 1
    assert per_automation.runs[0]["automation"] == "a1"

    feed = await svc.runs_feed(AutomationsRunsQuery(), p)
    assert {r["automation"] for r in feed.runs} == {"a1", "a2"}
    assert len(feed.runs) == 2


# --- scope enforcement --------------------------------------------------------


@pytest.mark.asyncio
async def test_list_requires_automations_read_scope(tmp_path):
    svc = AutomationsService(workspace=tmp_path)
    p = Principal.remote("t", frozenset())
    with pytest.raises(ForbiddenError):
        await svc.list(AutomationsListQuery(), p)


@pytest.mark.asyncio
async def test_save_requires_automations_write_scope(tmp_path):
    svc = AutomationsService(workspace=tmp_path)
    p = Principal.remote("t", frozenset({Scope.AUTOMATIONS_READ.value}))
    with pytest.raises(ForbiddenError):
        await svc.save(AutomationSaveCommand(name="a1", definition=_VALID), p)


# --- HTTP: route shadowing + hooks-secret ------------------------------------


def _http_client(tmp_path, *, hooks_secret=None):
    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    registry = ServiceRegistry()
    registry.register("automations", AutomationsService(
        workspace=tmp_path, cron_service=_cron(tmp_path), hooks_secret=hooks_secret))
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="test-token")
    return TestClient(app, raise_server_exceptions=False)


def test_global_runs_feed_route_is_not_shadowed_by_automation_name_route(tmp_path):
    """An automation literally named 'runs' must not steal the GET
    /api/v1/automations/runs feed."""
    client = _http_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}

    resp = client.put(
        "/api/v1/automations/runs",
        json={"definition": {**_VALID, "name": "runs"}},
        headers=headers,
    )
    assert resp.status_code == 200

    feed = client.get("/api/v1/automations/runs", headers=headers)
    assert feed.status_code == 200
    body = feed.json()
    assert body["runs"] == []   # the global feed, not a get-by-name 404/definition shape
    assert "definition" not in body


def test_hooks_secret_literal_route_is_not_shadowed_by_an_automation_named_hooks_secret(tmp_path):
    client = _http_client(tmp_path, hooks_secret=lambda: "s3cr3t")
    headers = {"Authorization": "Bearer test-token"}

    resp = client.put(
        "/api/v1/automations/hooks-secret",
        json={"definition": {**_VALID, "name": "hooks-secret"}},
        headers=headers,
    )
    assert resp.status_code == 200

    secret_resp = client.get("/api/v1/automations/hooks-secret", headers=headers)
    assert secret_resp.status_code == 200
    body = secret_resp.json()
    assert body == {"secret": "s3cr3t", "path_template": "/api/v1/hooks/{hook}"}
    assert "definition" not in body

    # The automation itself is still reachable by its own name, unaffected.
    got = client.get("/api/v1/automations/hooks-secret/runs", headers=headers)
    assert got.status_code == 200
    assert got.json()["runs"] == []


def test_hooks_secret_route_returns_secret_and_path_template(tmp_path):
    client = _http_client(tmp_path, hooks_secret=lambda: "s3cr3t")
    headers = {"Authorization": "Bearer test-token"}

    resp = client.get("/api/v1/automations/hooks-secret", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"secret": "s3cr3t", "path_template": "/api/v1/hooks/{hook}"}


def test_hooks_secret_route_unavailable_without_an_accessor(tmp_path):
    client = _http_client(tmp_path, hooks_secret=None)
    headers = {"Authorization": "Bearer test-token"}

    resp = client.get("/api/v1/automations/hooks-secret", headers=headers)

    assert resp.status_code == 503


def test_hooks_secret_route_requires_automations_write_scope(tmp_path):
    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    _, read_only_token = store.issue([Scope.AUTOMATIONS_READ.value], label="read-only")
    registry = ServiceRegistry()
    registry.register("automations", AutomationsService(
        workspace=tmp_path, cron_service=_cron(tmp_path), hooks_secret=lambda: "s3cr3t"))
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="")
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(
        "/api/v1/automations/hooks-secret", headers={"Authorization": f"Bearer {read_only_token}"}
    )
    assert resp.status_code == 403


def test_answer_route_accepts_an_explicit_action_over_http(tmp_path):
    """End-to-end: the webui's approve/reject buttons post an explicit `action`
    alongside `text`, and the route must forward it to the runtime unchanged.
    The route returns immediately (status `running`) rather than waiting for
    the resume to finish; the approval verdict is recorded in the prologue,
    so it is already on the record in this same response."""
    import asyncio

    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    cron = _cron(tmp_path)
    rt, _ = _runtime(tmp_path, [
        _wr("needs_input", out="proceed?", needs_input_node="gate", ask_kind="approval"),
        _wr("completed"),
    ])
    svc = AutomationsService(workspace=tmp_path, cron_service=cron, runtime=rt)
    asyncio.run(svc.save(AutomationSaveCommand(name="a1", definition=_VALID), Principal.local()))
    record = asyncio.run(rt.fire("a1", source="manual"))

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    registry = ServiceRegistry()
    registry.register("automations", svc)
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="test-token")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer test-token"}

    resp = client.post(
        f"/api/v1/automations/a1/runs/{record['run_id']}/answer",
        json={"text": "ignored", "action": "approve"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["run"]["status"] == "running"
    assert resp.json()["run"]["approval"]["action"] == "approve"


def test_stop_route_finalizes_a_paused_run_over_http(tmp_path):
    """End-to-end: POST .../stop on a paused run finalizes it `interrupted`
    with no delivery, mirroring the answer route's request/response shape."""
    import asyncio

    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    cron = _cron(tmp_path)
    rt, _ = _runtime(tmp_path, [
        _wr("needs_input", out="proceed?", needs_input_node="gate"),
    ])
    svc = AutomationsService(workspace=tmp_path, cron_service=cron, runtime=rt)
    asyncio.run(svc.save(AutomationSaveCommand(name="a1", definition=_VALID), Principal.local()))
    record = asyncio.run(rt.fire("a1", source="manual"))

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    registry = ServiceRegistry()
    registry.register("automations", svc)
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="test-token")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer test-token"}

    resp = client.post(
        f"/api/v1/automations/a1/runs/{record['run_id']}/stop",
        json={},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["run"]["status"] == "interrupted"
    assert resp.json()["run"]["detail"] == "stopped by operator"


def test_stop_route_of_an_unknown_run_is_404_over_http(tmp_path):
    import asyncio

    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    cron = _cron(tmp_path)
    rt, _ = _runtime(tmp_path, [])
    svc = AutomationsService(workspace=tmp_path, cron_service=cron, runtime=rt)
    asyncio.run(svc.save(AutomationSaveCommand(name="a1", definition=_VALID), Principal.local()))

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    registry = ServiceRegistry()
    registry.register("automations", svc)
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="test-token")
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/api/v1/automations/a1/runs/ghost-run/stop",
        json={},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 404
