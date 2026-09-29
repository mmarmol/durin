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

    async def _handed_a_thread_late(run_id, func, /, *args, **kwargs):
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
        assert await _until(lambda: run_log.read_manifest(
            tmp_path, "wf", "paused1")["status"] != "running")

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
