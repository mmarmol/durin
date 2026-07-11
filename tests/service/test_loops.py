"""Tests for LoopsService (list / get / save / delete / fire / answer / runs)."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from durin.cron.service import CronService
from durin.loops.runtime import LoopsRuntime
from durin.service.loops import (
    LoopAnswerCommand,
    LoopDeleteCommand,
    LoopFireCommand,
    LoopGetQuery,
    LoopRunsQuery,
    LoopSaveCommand,
    LoopsListQuery,
    LoopsRunsQuery,
    LoopsService,
)
from durin.service.principal import Principal
from durin.service.types import NotFoundError, ValidationFailedError
from durin.workflow.result import WorkflowResult

_VALID = {"name": "l1", "workflow": "w1", "goal": {"intent": "it is done"}}


def _cron(tmp_path) -> CronService:
    return CronService(Path(tmp_path) / "cron" / "jobs.json")


def _svc(tmp_path, *, runtime=None) -> LoopsService:
    return LoopsService(workspace=tmp_path, cron_service=_cron(tmp_path), runtime=runtime)


def _runtime(tmp_path, results, judge_verdict=None):
    calls = {"exec": []}

    async def workflow_exec(name, task, *, resume_run_id=None):
        calls["exec"].append((name, task, resume_run_id))
        return results.pop(0)

    async def judge(intent, assertions, evidence):
        return judge_verdict or {"intent_met": True, "assertions": {a: True for a in assertions}}

    ids = iter([f"lr{i}" for i in range(100)])
    rt = LoopsRuntime(tmp_path, workflow_exec=workflow_exec, judge=judge, keep_runs=20,
                      check_timeout_s=5, run_id_factory=lambda: next(ids))
    return rt, calls


def _wr(status, **kw):
    return WorkflowResult(status=status, final_output=kw.pop("out", "output"), run_id=kw.pop("run_id", "wf1"), **kw)


@pytest.mark.asyncio
async def test_save_list_get_round_trip_with_counts(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)

    listed = (await svc.list(LoopsListQuery(), p)).loops
    assert len(listed) == 1
    assert listed[0]["name"] == "l1"
    assert listed[0]["workflow"] == "w1"
    assert listed[0]["active_runs"] == 0
    assert listed[0]["needs_operator"] == 0

    got = await svc.get(LoopGetQuery(name="l1"), p)
    assert got.name == "l1"
    assert got.definition["workflow"] == "w1"
    assert got.definition["operator_to"] is None
    assert (tmp_path / "loops" / "l1.json").is_file()


@pytest.mark.asyncio
async def test_save_uses_the_url_name_over_a_mismatched_body_name(tmp_path):
    """The URL is authoritative for the loop's identity — same precedent as
    WorkflowsService.duplicate() overwriting the inner ``name`` field."""
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition={**_VALID, "name": "other"}), p)
    assert (await svc.list(LoopsListQuery(), p)).loops[0]["name"] == "l1"
    assert not (tmp_path / "loops" / "other.json").exists()


@pytest.mark.asyncio
async def test_save_rejects_an_invalid_loop(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    with pytest.raises(ValidationFailedError):
        await svc.save(LoopSaveCommand(name="bad", definition={"name": "bad"}), p)
    assert not (tmp_path / "loops" / "bad.json").exists()


@pytest.mark.asyncio
async def test_get_missing_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).get(LoopGetQuery(name="ghost"), Principal.local())


@pytest.mark.asyncio
async def test_delete_missing_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).delete(LoopDeleteCommand(name="ghost"), Principal.local())


@pytest.mark.asyncio
async def test_save_then_delete_round_trip(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    await svc.delete(LoopDeleteCommand(name="l1"), p)
    assert (await svc.list(LoopsListQuery(), p)).loops == []


@pytest.mark.asyncio
async def test_save_registers_a_cron_job_and_delete_removes_it(tmp_path):
    triggered = {**_VALID, "triggers": [
        {"source": "cron", "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}},
    ]}
    cron = _cron(tmp_path)
    svc, p = LoopsService(workspace=tmp_path, cron_service=cron), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=triggered), p)
    from durin.loops.cron_sync import loop_job_id
    assert cron.get_job(loop_job_id("l1", 0)) is not None

    await svc.delete(LoopDeleteCommand(name="l1"), p)
    assert cron.get_job(loop_job_id("l1", 0)) is None


@pytest.mark.asyncio
async def test_fire_without_a_runtime_is_unavailable(tmp_path):
    from durin.service.types import UnavailableError
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    with pytest.raises(UnavailableError):
        await svc.fire(LoopFireCommand(name="l1"), p)


@pytest.mark.asyncio
async def test_fire_runs_the_loop_and_returns_the_manifest(tmp_path):
    rt, calls = _runtime(tmp_path, [_wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)

    result = await svc.fire(LoopFireCommand(name="l1"), p)
    assert result.run["status"] == "done"
    assert calls["exec"][0][0] == "w1"

    listed = (await svc.list(LoopsListQuery(), p)).loops[0]
    assert listed["active_runs"] == 0


@pytest.mark.asyncio
async def test_fire_busy_raises_validation_error(tmp_path):
    rt, _ = _runtime(tmp_path, [_wr("needs_input", out="q?", needs_input_node="g")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    await svc.fire(LoopFireCommand(name="l1"), p)   # leaves an active needs_operator run

    with pytest.raises(ValidationFailedError):
        await svc.fire(LoopFireCommand(name="l1"), p)


@pytest.mark.asyncio
async def test_fire_missing_loop_raises_not_found(tmp_path):
    rt, _ = _runtime(tmp_path, [])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    with pytest.raises(NotFoundError):
        await svc.fire(LoopFireCommand(name="ghost"), p)


@pytest.mark.asyncio
async def test_answer_resumes_a_waiting_run(tmp_path):
    rt, calls = _runtime(tmp_path, [
        _wr("needs_input", out="approve?", needs_input_node="gate"), _wr("completed"),
    ])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    fired = await svc.fire(LoopFireCommand(name="l1"), p)
    run_id = fired.run["run_id"]

    result = await svc.answer(LoopAnswerCommand(name="l1", run_id=run_id, answer="yes"), p)
    assert result.run["status"] == "done"
    assert calls["exec"][1] == ("w1", "yes", "wf1")


@pytest.mark.asyncio
async def test_answer_of_a_non_waiting_run_raises_validation_error(tmp_path):
    rt, _ = _runtime(tmp_path, [_wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    fired = await svc.fire(LoopFireCommand(name="l1"), p)   # already terminal (done)

    with pytest.raises(ValidationFailedError):
        await svc.answer(LoopAnswerCommand(name="l1", run_id=fired.run["run_id"], answer="yes"), p)


@pytest.mark.asyncio
async def test_answer_without_a_runtime_is_unavailable(tmp_path):
    from durin.service.types import UnavailableError
    svc, p = _svc(tmp_path), Principal.local()
    with pytest.raises(UnavailableError):
        await svc.answer(LoopAnswerCommand(name="l1", run_id="r1", answer="yes"), p)


@pytest.mark.asyncio
async def test_answer_missing_loop_raises_not_found(tmp_path):
    rt, _ = _runtime(tmp_path, [])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    with pytest.raises(NotFoundError):
        await svc.answer(LoopAnswerCommand(name="ghost", run_id="r1", answer="yes"), p)


@pytest.mark.asyncio
async def test_runs_list_and_global_feed_shape(tmp_path):
    rt, _ = _runtime(tmp_path, [_wr("completed"), _wr("completed")])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    await svc.save(LoopSaveCommand(name="l2", definition={**_VALID, "name": "l2"}), p)
    await svc.fire(LoopFireCommand(name="l1"), p)
    await svc.fire(LoopFireCommand(name="l2"), p)

    per_loop = await svc.runs_list(LoopRunsQuery(name="l1"), p)
    assert len(per_loop.runs) == 1
    assert per_loop.runs[0]["loop"] == "l1"

    feed = await svc.runs_feed(LoopsRunsQuery(), p)
    assert {r["loop"] for r in feed.runs} == {"l1", "l2"}
    assert len(feed.runs) == 2


# --- route-order: /api/v1/loops/runs must not be shadowed by /api/v1/loops/{name} ---


def _http_client(tmp_path):
    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    registry = ServiceRegistry()
    registry.register("loops", LoopsService(workspace=tmp_path, cron_service=_cron(tmp_path)))
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="test-token")
    return TestClient(app, raise_server_exceptions=False)


def test_global_runs_feed_route_is_not_shadowed_by_loop_name_route(tmp_path):
    """A loop literally named 'runs' must not steal the GET /api/v1/loops/runs feed."""
    client = _http_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}

    # Seed a loop named "runs" — if {name} matched first, GET /api/v1/loops/runs
    # would return this loop's single-run list instead of the global feed shape.
    resp = client.put(
        "/api/v1/loops/runs",
        json={"definition": {**_VALID, "name": "runs"}},
        headers=headers,
    )
    assert resp.status_code == 200

    feed = client.get("/api/v1/loops/runs", headers=headers)
    assert feed.status_code == 200
    body = feed.json()
    assert "runs" in body
    assert body["runs"] == []   # the global feed (no runs fired yet), not a loop-get 404/definition shape
    assert "definition" not in body
