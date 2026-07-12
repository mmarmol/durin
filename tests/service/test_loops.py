"""Tests for LoopsService (list / get / save / delete / fire / answer / runs / stats)."""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from durin.cron.service import CronService
from durin.loops import run_log
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
    LoopStatsQuery,
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


def _seed_run(tmp_path, loop: str, run_id: str, status: str, started_at: float, *, goal_reached=None) -> None:
    """Write a run manifest directly to run_log, bypassing the runtime, so tests
    can seed an arbitrary mix of statuses/timestamps for stats math."""
    run_log.start_run(tmp_path, loop, run_id, source="manual", task="t")
    if status != "running":
        run_log.finalize_run(tmp_path, loop, run_id, status=status, goal_reached=goal_reached)
    run_log.update_run(tmp_path, loop, run_id, started_at=started_at)


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
async def test_list_counts_waiting_info_and_pending_events(tmp_path):
    from durin.loops import queue

    rt, _ = _runtime(tmp_path, [
        _wr("needs_input", out="[TO:counterpart] need more info", needs_input_node="g"),
    ])
    svc, p = _svc(tmp_path, runtime=rt), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    queue.push(tmp_path, "l1", {"content": "queued event"})

    record = await rt.fire("l1", source="channel", origin={"thread": "t1", "channel": "test"})
    assert record["status"] == "waiting_info"

    listed = (await svc.list(LoopsListQuery(), p)).loops[0]
    assert listed["active_runs"] == 1
    assert listed["waiting_info"] == 1
    assert listed["needs_operator"] == 0
    assert listed["pending_events"] == 1


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


@pytest.mark.asyncio
async def test_stats_math_covers_every_status(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)

    _seed_run(tmp_path, "l1", "r1", "running", 1.0)
    _seed_run(tmp_path, "l1", "r2", "needs_operator", 2.0)
    _seed_run(tmp_path, "l1", "r3", "waiting_info", 3.0)
    _seed_run(tmp_path, "l1", "r4", "done", 4.0, goal_reached=True)
    _seed_run(tmp_path, "l1", "r5", "no_goal", 5.0, goal_reached=False)
    _seed_run(tmp_path, "l1", "r6", "escalated", 6.0, goal_reached=False)
    _seed_run(tmp_path, "l1", "r7", "error", 7.0, goal_reached=None)

    stats = await svc.stats(LoopStatsQuery(name="l1"), p)
    assert stats.name == "l1"
    assert stats.counts == {
        "running": 1, "needs_operator": 1, "waiting_info": 1,
        "done": 1, "no_goal": 1, "escalated": 1, "error": 1,
    }
    # terminal = done + no_goal + escalated + error = 4
    assert stats.convergence == pytest.approx(1 / 4)
    assert stats.escalation_rate == pytest.approx(1 / 4)
    assert stats.pending_events == 0

    assert [o["run_id"] for o in stats.outcomes] == ["r7", "r6", "r5", "r4"]
    assert stats.outcomes[0] == {
        "run_id": "r7", "status": "error", "goal_reached": None,
        "started_at": 7.0, "finished_at": pytest.approx(stats.outcomes[0]["finished_at"]),
    }
    assert stats.outcomes[-1]["run_id"] == "r4"
    assert stats.outcomes[-1]["goal_reached"] is True


@pytest.mark.asyncio
async def test_stats_null_when_no_runs_at_all(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)

    stats = await svc.stats(LoopStatsQuery(name="l1"), p)
    assert stats.convergence is None
    assert stats.escalation_rate is None
    assert stats.outcomes == []
    assert stats.counts == {
        "running": 0, "needs_operator": 0, "waiting_info": 0,
        "done": 0, "no_goal": 0, "escalated": 0, "error": 0,
    }


@pytest.mark.asyncio
async def test_stats_null_when_only_active_runs(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    _seed_run(tmp_path, "l1", "r1", "running", 1.0)
    _seed_run(tmp_path, "l1", "r2", "needs_operator", 2.0)
    _seed_run(tmp_path, "l1", "r3", "waiting_info", 3.0)

    stats = await svc.stats(LoopStatsQuery(name="l1"), p)
    assert stats.convergence is None
    assert stats.escalation_rate is None
    assert stats.outcomes == []
    assert stats.counts["running"] == 1
    assert stats.counts["needs_operator"] == 1
    assert stats.counts["waiting_info"] == 1


@pytest.mark.asyncio
async def test_stats_outcomes_capped_at_last_20_terminal_runs_newest_first(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(LoopSaveCommand(name="l1", definition=_VALID), p)
    for i in range(25):
        _seed_run(tmp_path, "l1", f"r{i}", "done", float(i), goal_reached=True)

    stats = await svc.stats(LoopStatsQuery(name="l1"), p)
    assert stats.counts["done"] == 25
    assert len(stats.outcomes) == 20
    assert [o["run_id"] for o in stats.outcomes] == [f"r{i}" for i in range(24, 4, -1)]


@pytest.mark.asyncio
async def test_stats_missing_loop_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).stats(LoopStatsQuery(name="ghost"), Principal.local())


def test_stats_route_http_roundtrip(tmp_path):
    client = _http_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    resp = client.put("/api/v1/loops/l1", json={"definition": _VALID}, headers=headers)
    assert resp.status_code == 200

    _seed_run(tmp_path, "l1", "r1", "done", 1.0, goal_reached=True)
    _seed_run(tmp_path, "l1", "r2", "error", 2.0)

    resp = client.get("/api/v1/loops/l1/stats", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "l1"
    assert body["convergence"] == pytest.approx(0.5)
    assert body["escalation_rate"] == pytest.approx(0.0)
    assert body["pending_events"] == 0
    assert [o["run_id"] for o in body["outcomes"]] == ["r2", "r1"]
    assert body["counts"]["done"] == 1
    assert body["counts"]["error"] == 1


def test_stats_route_missing_loop_returns_404(tmp_path):
    client = _http_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    resp = client.get("/api/v1/loops/ghost/stats", headers=headers)
    assert resp.status_code == 404


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


def test_loop_named_runs_stats_route_resolves_to_the_loop_not_the_feed(tmp_path):
    """A loop literally named 'runs' must still resolve GET /loops/runs/stats
    to its own per-loop stats — the {name}/stats route (5 segments) is
    distinct from the literal /loops/runs feed (4 segments), but this proves
    it end-to-end rather than by segment-count reasoning alone."""
    client = _http_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    resp = client.put(
        "/api/v1/loops/runs",
        json={"definition": {**_VALID, "name": "runs"}},
        headers=headers,
    )
    assert resp.status_code == 200
    _seed_run(tmp_path, "runs", "r1", "done", 1.0, goal_reached=True)

    stats = client.get("/api/v1/loops/runs/stats", headers=headers)
    assert stats.status_code == 200
    body = stats.json()
    assert body["name"] == "runs"
    assert body["counts"]["done"] == 1
    assert "runs" not in body   # not the global feed's {runs: [...]} shape

    feed = client.get("/api/v1/loops/runs", headers=headers)
    assert feed.status_code == 200
    feed_body = feed.json()
    assert "definition" not in feed_body   # still the global feed shape, not a loop-get
    assert [r["run_id"] for r in feed_body["runs"]] == ["r1"]


def test_save_route_accepts_an_enabled_loop_with_a_channel_trigger(tmp_path):
    """Regression: sync_loop_jobs used to do CronSchedule(**trig.schedule) on
    every trigger regardless of source — a channel trigger's empty schedule
    raised TypeError, which surfaced as a 500 on this route."""
    client = _http_client(tmp_path)
    headers = {"Authorization": "Bearer test-token"}
    definition = {
        **_VALID,
        "name": "chan1",
        "enabled": True,
        "triggers": [{"source": "channel", "channel": "email"}],
    }

    resp = client.put("/api/v1/loops/chan1", json={"definition": definition}, headers=headers)
    assert resp.status_code == 200

    got = client.get("/api/v1/loops/chan1", headers=headers)
    assert got.status_code == 200
    assert got.json()["definition"]["triggers"] == [
        {"source": "channel", "channel": "email", "filters": {}, "match": "wake_or_new"}
    ]


def test_answer_route_accepts_a_waiting_info_run(tmp_path):
    """A run parked as waiting_info (counterpart-bound ask) must resolve through
    the same /answer route as a needs_operator run — the route must not
    special-case status; only the runtime does."""
    import asyncio

    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    cron = _cron(tmp_path)
    rt, _ = _runtime(tmp_path, [
        _wr("needs_input", out="[TO:counterpart] need more info", needs_input_node="g"),
        _wr("completed"),
    ])
    svc = LoopsService(workspace=tmp_path, cron_service=cron, runtime=rt)
    asyncio.run(svc.save(LoopSaveCommand(name="l1", definition=_VALID), Principal.local()))
    record = asyncio.run(rt.fire("l1", source="channel", origin={"thread": "t1", "channel": "test"}))
    assert record["status"] == "waiting_info"

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    registry = ServiceRegistry()
    registry.register("loops", svc)
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="test-token")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer test-token"}

    resp = client.post(
        f"/api/v1/loops/l1/runs/{record['run_id']}/answer",
        json={"answer": "yes"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["run"]["status"] == "done"


# --- GET /api/v1/loops/hooks-secret -----------------------------------------


def _http_client_with_hooks_secret(tmp_path, secret: str | None = "s3cr3t"):
    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    registry = ServiceRegistry()
    hooks_secret = (lambda: secret) if secret is not None else None
    registry.register("loops", LoopsService(
        workspace=tmp_path, cron_service=_cron(tmp_path), hooks_secret=hooks_secret))
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="test-token")
    return TestClient(app, raise_server_exceptions=False)


def test_hooks_secret_route_returns_secret_and_path_template(tmp_path):
    client = _http_client_with_hooks_secret(tmp_path, secret="s3cr3t")
    headers = {"Authorization": "Bearer test-token"}

    resp = client.get("/api/v1/loops/hooks-secret", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"secret": "s3cr3t", "path_template": "/api/v1/hooks/{hook}"}


def test_hooks_secret_route_unavailable_without_an_accessor(tmp_path):
    client = _http_client_with_hooks_secret(tmp_path, secret=None)
    headers = {"Authorization": "Bearer test-token"}

    resp = client.get("/api/v1/loops/hooks-secret", headers=headers)

    assert resp.status_code == 503


def test_hooks_secret_route_requires_loops_write_scope(tmp_path):
    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.principal import Scope
    from durin.service.registry import ServiceRegistry

    store = ApiTokenStore(path=tmp_path / "tokens.json")
    auth = AuthService(store=store)
    _, read_only_token = store.issue([Scope.LOOPS_READ.value], label="read-only")
    registry = ServiceRegistry()
    registry.register("loops", LoopsService(
        workspace=tmp_path, cron_service=_cron(tmp_path), hooks_secret=lambda: "s3cr3t"))
    registry.register("auth", auth)
    app = build_api_app(registry, auth=auth, static_token="")
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get(
        "/api/v1/loops/hooks-secret", headers={"Authorization": f"Bearer {read_only_token}"}
    )
    assert resp.status_code == 403


def test_hooks_secret_literal_route_is_not_shadowed_by_a_loop_named_hooks_secret(tmp_path):
    """A loop literally named "hooks-secret" must not steal the literal
    GET /api/v1/loops/hooks-secret route (mirrors the "runs" shadow tests
    above — a {name} param route must never win over a literal segment)."""
    client = _http_client_with_hooks_secret(tmp_path, secret="s3cr3t")
    headers = {"Authorization": "Bearer test-token"}

    resp = client.put(
        "/api/v1/loops/hooks-secret",
        json={"definition": {**_VALID, "name": "hooks-secret"}},
        headers=headers,
    )
    assert resp.status_code == 200

    secret_resp = client.get("/api/v1/loops/hooks-secret", headers=headers)
    assert secret_resp.status_code == 200
    body = secret_resp.json()
    assert body == {"secret": "s3cr3t", "path_template": "/api/v1/hooks/{hook}"}
    assert "definition" not in body   # not a loop-get response for the loop named hooks-secret

    # The loop itself is still reachable by its own name, unaffected.
    got = client.get("/api/v1/loops/hooks-secret/stats", headers=headers)
    assert got.status_code == 200
    assert got.json()["name"] == "hooks-secret"
