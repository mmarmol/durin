"""A resume through WorkflowsService moves the paused manifest to ``running``
(the claim) before the engine starts, so a racing cancel or second resume
refuses. When the engine then never writes the run's own manifest — the
resume is rejected before its walk, its setup fails, or it is cancelled
while it waits for a workflow-run thread — the pause comes back exactly as
it was: nothing else would ever end a ``running`` manifest owned by the live
gateway, and the person could neither answer nor cancel it.

Real engine, script nodes only: no LLM provider is involved.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.service.principal import Principal
from durin.service.types import ValidationFailedError
from durin.service.workflows import WorkflowsService
from durin.session.manager import SessionManager
from durin.workflow import run_log
from durin.workflow.loader import workflows_dir
from durin.workflow.resume_claim import ResumeClaim
from durin.workflow.run_threads import MAX_CONCURRENT_RUNS, run_on_workflow_thread

# A pid far above any real pid_max, so the owner reads as long gone: a pause
# is usually answered after the gateway that paused it has restarted.
_DEAD_OWNER = {"pid": 2**22 + 54321, "started": "never"}


def _svc(tmp_path):
    app_config = SimpleNamespace(
        resolve_default_preset=lambda: object(),
        tools=ToolsConfig(),
        workflow=WorkflowConfig(),
    )
    return WorkflowsService(workspace=tmp_path, app_config=app_config,
                            sessions=SessionManager(workspace=tmp_path))


def _provider():
    return patch("durin.providers.factory.make_provider",
                 return_value=SimpleNamespace(get_default_model=lambda: "m"))


def _paused_run(tmp_path, node: dict) -> dict:
    """Park run ``paused1`` of workflow ``wf`` on its only node, owned by a
    dead process; returns the manifest as parked."""
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "wf.json").write_text(json.dumps(
        {"name": "wf", "start": "only", "nodes": [{"id": "only", **node, "next": None}]}),
        encoding="utf-8")
    run_log.start_run(tmp_path, "wf", "paused1", root_session_key="s", started_at=1.0)
    f = tmp_path / "workflows-runs" / "wf" / "paused1.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(status="needs_input", needs_input_node="only", final_output="which env?",
               owner=_DEAD_OWNER)
    f.write_text(json.dumps(rec), encoding="utf-8")
    return run_log.read_manifest(tmp_path, "wf", "paused1")


def _assert_paused_as_before(tmp_path, before: dict) -> None:
    assert run_log.read_manifest(tmp_path, "wf", "paused1") == before
    # Nothing a sweep would act on, and the pause can still be answered.
    assert run_log.reconcile_running(
        tmp_path, now=time.time(), max_age_s=run_log.RECONCILE_AGE_S) == 0


async def _until(predicate, timeout: float = 5.0) -> bool:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_a_resume_rejected_before_its_walk_leaves_the_run_paused(tmp_path):
    """The engine's preflight rejects the resume (a script the workflow
    names is gone) and returns without writing the manifest."""
    before = _paused_run(tmp_path, {"kind": "script", "script": "gone.sh"})
    svc = _svc(tmp_path)

    with _provider():
        result = await svc.execute("wf", "prod", resume_run_id="paused1")

    assert result.status == "aborted"
    assert "script file not found" in (result.final_output or "")
    _assert_paused_as_before(tmp_path, before)


@pytest.mark.asyncio
async def test_a_resume_whose_setup_fails_leaves_the_run_paused(tmp_path):
    before = _paused_run(tmp_path, {"kind": "script", "command": "echo ok"})
    svc = _svc(tmp_path)

    with patch("durin.providers.factory.make_provider", side_effect=RuntimeError("no key")):
        with pytest.raises(RuntimeError, match="no key"):
            await svc.execute("wf", "prod", resume_run_id="paused1")

    _assert_paused_as_before(tmp_path, before)


@pytest.mark.asyncio
async def test_a_resume_cancelled_while_it_waits_for_a_thread_leaves_the_run_paused(tmp_path):
    marker = tmp_path / "walked.marker"
    before = _paused_run(tmp_path, {"kind": "script", "command": f"touch {marker}"})
    svc = _svc(tmp_path)
    gate = threading.Event()
    holders = [asyncio.create_task(run_on_workflow_thread(f"h{i}", gate.wait, 10.0))
               for i in range(MAX_CONCURRENT_RUNS)]
    try:
        with _provider():
            resume = asyncio.create_task(svc.execute("wf", "prod", resume_run_id="paused1"))
            # Claimed, and waiting for a thread.
            assert await _until(lambda: (run_log.read_manifest(tmp_path, "wf", "paused1")
                                         or {}).get("status") == "running")
            await asyncio.sleep(0.1)
            resume.cancel()
            with pytest.raises(asyncio.CancelledError):
                await resume
    finally:
        gate.set()
    await asyncio.wait_for(asyncio.gather(*holders), timeout=10.0)
    await asyncio.sleep(0.2)

    assert not marker.exists()
    _assert_paused_as_before(tmp_path, before)
    # Answerable again.
    with _provider():
        result = await svc.execute("wf", "prod", resume_run_id="paused1")
    assert result.status == "completed"
    assert marker.exists()


@pytest.mark.asyncio
async def test_a_run_handed_a_thread_just_as_its_caller_is_cancelled_never_walks(
        tmp_path, monkeypatch):
    """The cancellation can land while the run is already being handed a
    thread. The caller then releases the claim, and the thread, finding it
    released, does not start the walk."""
    marker = tmp_path / "walked.marker"
    before = _paused_run(tmp_path, {"kind": "script", "command": f"touch {marker}"})
    svc = _svc(tmp_path)
    late: list = []
    threads: list[threading.Thread] = []

    async def _handed_a_thread_late(run_id, func, /, *args, paced=False, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            def _late():
                # Runs once the cancelled caller has reacted.
                for _ in range(500):
                    if run_log.read_manifest(tmp_path, "wf", "paused1") == before:
                        break
                    time.sleep(0.01)
                late.append(func(*args, **kwargs))
            threads.append(threading.Thread(target=_late))
            threads[0].start()
            raise

    monkeypatch.setattr("durin.service.workflows.run_on_workflow_thread", _handed_a_thread_late)
    with _provider():
        resume = asyncio.create_task(svc.execute("wf", "prod", resume_run_id="paused1"))
        assert await _until(lambda: (run_log.read_manifest(tmp_path, "wf", "paused1")
                                     or {}).get("status") == "running")
        resume.cancel()
        with pytest.raises(asyncio.CancelledError):
            await resume
        threads[0].join(10.0)

    assert late == [None]
    assert not marker.exists()
    _assert_paused_as_before(tmp_path, before)


@pytest.mark.asyncio
async def test_a_cancel_landing_while_the_resume_builds_its_engine_wins(tmp_path):
    """The claim re-checks the run under the per-run lock: a cancel that
    lands between the resume's first check and its claim wins outright."""
    _paused_run(tmp_path, {"kind": "script", "command": "echo ok"})
    svc = _svc(tmp_path)
    cancelled: list[dict] = []

    def _provider_while_a_cancel_lands(*_a, **_kw):
        canceller = threading.Thread(target=lambda: cancelled.append(
            asyncio.run(svc.cancel_run("wf", "paused1", Principal.local()))))
        canceller.start()
        canceller.join(10.0)
        return SimpleNamespace(get_default_model=lambda: "m")

    with patch("durin.providers.factory.make_provider", side_effect=_provider_while_a_cancel_lands):
        with pytest.raises(ValidationFailedError, match="cancelled, or already resumed"):
            await svc.execute("wf", "prod", resume_run_id="paused1")

    assert cancelled[0]["status"] == "cancelled"
    assert run_log.read_manifest(tmp_path, "wf", "paused1")["status"] == "cancelled"


@pytest.mark.asyncio
async def test_a_resume_cancelled_after_its_walk_started_keeps_the_engines_record(tmp_path):
    """Once the walk runs, the manifest is the engine's: a cancelled caller
    leaves it running to the end, and its record stands."""
    started = tmp_path / "started.marker"
    gate_file = tmp_path / "gate.file"
    before = _paused_run(tmp_path, {
        "kind": "script", "timeout": 10,
        "command": f"touch {started} && while [ ! -f {gate_file} ]; do sleep 0.05; done"})
    svc = _svc(tmp_path)

    with _provider():
        resume = asyncio.create_task(svc.execute("wf", "prod", resume_run_id="paused1"))
        assert await _until(started.exists)
        resume.cancel()
        with pytest.raises(asyncio.CancelledError):
            await resume
        gate_file.touch()

        def finished() -> bool:
            # The engine rewrites the manifest in place, from the run's thread:
            # a read that lands mid-write finds it empty, which read_manifest
            # reports as None. That is not the end of the run; read again.
            manifest = run_log.read_manifest(tmp_path, "wf", "paused1")
            return manifest is not None and manifest["status"] != "running"

        assert await _until(finished)

    after = run_log.read_manifest(tmp_path, "wf", "paused1")
    assert after["status"] == "completed"
    assert after["started_at"] != before["started_at"]


@pytest.mark.asyncio
async def test_a_failing_release_never_replaces_the_runs_own_outcome(tmp_path, monkeypatch):
    _paused_run(tmp_path, {"kind": "script", "command": "echo ok"})
    svc = _svc(tmp_path)

    def _broken_release(*_a, **_kw):
        raise OSError("disk gone")

    monkeypatch.setattr(run_log, "release_resume_claim", _broken_release)
    with _provider():
        result = await svc.execute("wf", "prod", resume_run_id="paused1")

    assert result.status == "completed"


@pytest.mark.asyncio
async def test_a_released_pause_can_still_be_cancelled(tmp_path):
    before = _paused_run(tmp_path, {"kind": "script", "script": "gone.sh"})
    svc = _svc(tmp_path)
    with _provider():
        await svc.execute("wf", "prod", resume_run_id="paused1")
    assert run_log.read_manifest(tmp_path, "wf", "paused1") == before

    manifest = await svc.cancel_run("wf", "paused1", Principal.local())

    assert manifest["status"] == "cancelled"


def _aborted_run(tmp_path, node: dict) -> dict:
    """Run ``failed1`` of workflow ``wf``, aborted at its only node and owned by
    a dead process; returns the record as it stands."""
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "wf.json").write_text(json.dumps(
        {"name": "wf", "start": "only", "nodes": [{"id": "only", **node, "next": None}]}),
        encoding="utf-8")
    run_log.start_run(tmp_path, "wf", "failed1", root_session_key="s", started_at=1.0)
    f = tmp_path / "workflows-runs" / "wf" / "failed1.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(status="aborted", failed_node="only", resume_upstream="", final_output="boom",
               owner=_DEAD_OWNER,
               runs=[{"node_id": "only", "iteration": 1, "status": "node_failed"}])
    f.write_text(json.dumps(rec), encoding="utf-8")
    return run_log.read_manifest(tmp_path, "wf", "failed1")


@pytest.mark.asyncio
async def test_a_second_resume_landing_while_the_first_builds_its_engine_loses(tmp_path):
    """Same window the cancel test above covers, for an aborted run: another
    resume claims the record between this one's first read and its own claim."""
    before = _aborted_run(tmp_path, {"kind": "script", "command": "echo ok"})
    svc = _svc(tmp_path)

    def _provider_while_another_resume_claims(*_a, **_kw):
        ResumeClaim.take(tmp_path, "wf", "failed1", expected=before)
        return SimpleNamespace(get_default_model=lambda: "m")

    with patch("durin.providers.factory.make_provider",
               side_effect=_provider_while_another_resume_claims):
        with pytest.raises(ValidationFailedError, match="cancelled, or already resumed"):
            await svc.execute("wf", "", resume_run_id="failed1")
    assert run_log.read_manifest(tmp_path, "wf", "failed1")["status"] == "running"  # the other's claim


@pytest.mark.asyncio
async def test_an_aborted_resume_rejected_before_its_walk_leaves_the_run_aborted(tmp_path):
    before = _aborted_run(tmp_path, {"kind": "script", "script": "gone.sh"})
    with _provider():
        result = await _svc(tmp_path).execute("wf", "", resume_run_id="failed1")
    assert result.status == "aborted" and "script file not found" in (result.final_output or "")
    assert run_log.read_manifest(tmp_path, "wf", "failed1") == before


@pytest.mark.asyncio
async def test_an_aborted_run_resumed_through_the_service_runs_once(tmp_path):
    _aborted_run(tmp_path, {"kind": "script", "command": "echo ok"})
    with _provider():
        result = await _svc(tmp_path).execute("wf", "", resume_run_id="failed1")
    assert result.status == "completed"
    rec = run_log.read_manifest(tmp_path, "wf", "failed1")
    assert [(r["node_id"], r["iteration"]) for r in rec["runs"]] == [("only", 2)]


def _dead_run(tmp_path, node: dict, *, surface: str | None = None) -> dict:
    """Run ``dead1`` of workflow ``wf`` left ``running`` by a process that died
    in its only step: its position names that step, whose input is stored, and
    its working folder exists. Returns the record as left."""
    from durin.workflow import resume_store
    from durin.workflow.artifacts import artifact_dir

    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "wf.json").write_text(json.dumps(
        {"name": "wf", "start": "only", "nodes": [{"id": "only", **node, "next": None}]}),
        encoding="utf-8")
    work_dir = artifact_dir(tmp_path, "dead1", "work", None)
    work_dir.mkdir(parents=True, exist_ok=True)
    resume_store.save_input(resume_store.store_dir(tmp_path, "dead1"), 1, "only", "")
    run_log.start_run(tmp_path, "wf", "dead1", root_session_key="s", started_at=1.0,
                      task="the task", work_dir=str(work_dir),
                      position={"node": "only", "seq": 1}, surface=surface)
    f = tmp_path / "workflows-runs" / "wf" / "dead1.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(owner=_DEAD_OWNER, active_node={
        "node_id": "only", "label": "only", "started_at": 1.0,
        "iteration": 1, "session_key": None})
    f.write_text(json.dumps(rec), encoding="utf-8")
    return run_log.read_manifest(tmp_path, "wf", "dead1")


@pytest.mark.asyncio
async def test_a_run_whose_process_died_is_resumed_at_its_step(tmp_path):
    _dead_run(tmp_path, {"kind": "script", "command": "echo ok"})
    with _provider():
        result = await _svc(tmp_path).execute("wf", "", resume_run_id="dead1")
    assert result.status == "completed"
    rec = run_log.read_manifest(tmp_path, "wf", "dead1")
    assert [(r["node_id"], r["iteration"], bool(r.get("cut")))
            for r in rec["history"] + rec["runs"]] == [("only", 1, True), ("only", 2, False)]
    assert rec["surface"] is None          # kept from the first walk, which named none


@pytest.mark.asyncio
async def test_a_crashed_run_recorded_before_positions_is_refused_and_left_alone(tmp_path):
    _dead_run(tmp_path, {"kind": "script", "command": "echo ok"})
    f = tmp_path / "workflows-runs" / "wf" / "dead1.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.pop("position")
    rec.pop("owner")
    rec.update(status="crashed", active_node=None)
    f.write_text(json.dumps(rec), encoding="utf-8")
    before = f.read_bytes()
    with _provider():
        with pytest.raises(ValidationFailedError, match="before a run kept the step"):
            await _svc(tmp_path).execute("wf", "", resume_run_id="dead1")
    assert f.read_bytes() == before


@pytest.mark.asyncio
async def test_an_agent_run_with_mcp_steps_is_left_to_the_agent(tmp_path):
    _dead_run(tmp_path, {"kind": "work", "mcps": ["tracker"]}, surface="agent")
    with _provider(), patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        with pytest.raises(ValidationFailedError, match="only a run resumed by the agent"):
            await _svc(tmp_path).execute("wf", "", resume_run_id="dead1")
    fake_run.assert_not_called()
    assert run_log.read_manifest(tmp_path, "wf", "dead1")["status"] == "crashed"


@pytest.mark.asyncio
async def test_an_agent_run_with_mcp_steps_paused_for_approval_can_still_be_rejected(tmp_path):
    # A reject runs no step, so the tools the steps had do not matter.
    _paused_run(tmp_path, {"kind": "work", "mcps": ["tracker"]})
    f = tmp_path / "workflows-runs" / "wf" / "paused1.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(ask_kind="approval", surface="agent")
    f.write_text(json.dumps(rec), encoding="utf-8")
    with _provider():
        result = await _svc(tmp_path).execute("wf", "reject", resume_run_id="paused1")
    assert result.status == "cancelled" and result.rejected


@pytest.mark.asyncio
async def test_a_resume_keeps_the_surface_that_launched_the_run(tmp_path):
    # Launched by the agent, resumed through the service: no step names MCP
    # tools, so the service may resume it, and the record still says who launched it.
    _dead_run(tmp_path, {"kind": "script", "command": "echo ok"}, surface="agent")
    with _provider():
        result = await _svc(tmp_path).execute("wf", "", resume_run_id="dead1")
    assert result.status == "completed"
    assert run_log.read_manifest(tmp_path, "wf", "dead1")["surface"] == "agent"


@pytest.mark.asyncio
async def test_a_run_the_service_launches_is_recorded_as_the_services(tmp_path):
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "wf.json").write_text(json.dumps(
        {"name": "wf", "start": "only",
         "nodes": [{"id": "only", "kind": "script", "command": "echo ok", "next": None}]}),
        encoding="utf-8")
    with _provider():
        result = await _svc(tmp_path).execute("wf", "go", run_id="fresh1")
    assert result.status == "completed"
    assert run_log.read_manifest(tmp_path, "wf", "fresh1")["surface"] == "service"


# An agent-launched run of a workflow with an `mcps` step. Only a dead run is
# refused for it through the service; a paused or aborted one is resumed as it
# always was (its later `mcps` steps run without those tools, a known limit).

_MCP_FLOW = {"name": "wf", "start": "ask", "nodes": [
    {"id": "ask", "kind": "work", "approval": True, "next": "tool"},
    {"id": "tool", "kind": "work", "mcps": ["tracker"], "next": None}]}


def _agent_record(tmp_path, definition: dict, **fields) -> None:
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{definition['name']}.json").write_text(json.dumps(definition), encoding="utf-8")
    run_log.start_run(tmp_path, "wf", "agent1", root_session_key="s", started_at=1.0,
                      task="the task", surface="agent")
    f = tmp_path / "workflows-runs" / "wf" / "agent1.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(owner=_DEAD_OWNER, **fields)
    f.write_text(json.dumps(rec), encoding="utf-8")


def _engine_run_completes():
    from durin.workflow.result import WorkflowResult
    return patch("durin.workflow.engine.WorkflowEngine.run", return_value=WorkflowResult(
        status="completed", final_output="done", runs=[], run_id="agent1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["approve", "please shorten it"])
async def test_an_agent_approval_pause_with_mcp_steps_is_answered_through_the_service(
        tmp_path, reply):
    _agent_record(tmp_path, _MCP_FLOW, status="needs_input", needs_input_node="ask",
                  ask_kind="approval", final_output="the proposal")
    with _provider(), _engine_run_completes() as fake_run:
        result = await _svc(tmp_path).execute("wf", reply, resume_run_id="agent1")
    assert result.status == "completed"
    fake_run.assert_called_once()


@pytest.mark.asyncio
async def test_an_agent_question_pause_with_mcp_steps_is_answered_through_the_service(tmp_path):
    flow = {**_MCP_FLOW, "nodes": [{**_MCP_FLOW["nodes"][0], "approval": False},
                                   _MCP_FLOW["nodes"][1]]}
    _agent_record(tmp_path, flow, status="needs_input", needs_input_node="ask",
                  ask_kind="question", final_output="which env?")
    with _provider(), _engine_run_completes() as fake_run:
        result = await _svc(tmp_path).execute("wf", "prod", resume_run_id="agent1")
    assert result.status == "completed"
    fake_run.assert_called_once()


@pytest.mark.asyncio
async def test_an_agent_aborted_run_with_mcp_steps_is_retried_through_the_service(tmp_path):
    _agent_record(tmp_path, _MCP_FLOW, status="aborted", failed_node="tool",
                  resume_upstream="x", final_output="boom",
                  runs=[{"node_id": "tool", "iteration": 1, "status": "node_failed"}])
    with _provider(), _engine_run_completes() as fake_run:
        result = await _svc(tmp_path).execute("wf", "", resume_run_id="agent1")
    assert result.status == "completed"
    fake_run.assert_called_once()


def _write_flow(tmp_path, definition: dict) -> None:
    (workflows_dir(tmp_path) / f"{definition['name']}.json").write_text(
        json.dumps(definition), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_dead_agent_run_whose_sub_workflow_has_mcp_steps_is_left_to_the_agent(tmp_path):
    _dead_run(tmp_path, {"kind": "subworkflow", "workflow": "child"}, surface="agent")
    _write_flow(tmp_path, {"name": "child", "start": "x", "nodes": [
        {"id": "x", "kind": "work", "mcps": ["tracker"], "next": None}]})
    with _provider(), patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        with pytest.raises(ValidationFailedError, match="only a run resumed by the agent"):
            await _svc(tmp_path).execute("wf", "", resume_run_id="dead1")
    fake_run.assert_not_called()


@pytest.mark.asyncio
async def test_a_dead_agent_run_whose_sub_workflow_has_no_mcp_steps_is_resumed(tmp_path):
    _dead_run(tmp_path, {"kind": "subworkflow", "workflow": "child"}, surface="agent")
    _write_flow(tmp_path, {"name": "child", "start": "x", "nodes": [
        {"id": "x", "kind": "script", "command": "echo child-ok", "next": None}]})
    with _provider():
        result = await _svc(tmp_path).execute("wf", "", resume_run_id="dead1")
    assert result.status == "completed" and "child-ok" in (result.final_output or "")
