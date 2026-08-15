"""The unified `tasks` tool: list / status / stop over sub-agents + workflow runs."""

import json
import time
from pathlib import Path

import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.tasks_tool import TasksTool
from durin.workflow import cancellation

SESSION = "websocket:chatA"


class _SAStatus:
    def __init__(self, task_id, label, phase, iteration=1, usage=None, tool_events=None,
                 error=None, stop_reason=None):
        self.task_id = task_id
        self.label = label
        self.phase = phase
        self.iteration = iteration
        self.session_key = f"subagent:{task_id}"
        self.started_at = time.monotonic()
        self.ended_at = None
        self.usage = usage or {}
        self.tool_events = tool_events or []
        self.error = error
        self.stop_reason = stop_reason


class _FakeManager:
    def __init__(self, statuses, running):
        self._statuses = {s.task_id: s for s in statuses}
        self._running = set(running)
        self.stopped = []

    def list_for_session(self, sk):
        return list(self._statuses.values())

    def get_status_for(self, tid, sk):
        return self._statuses.get(tid)

    def _is_running(self, tid):
        return tid in self._running

    async def stop_task(self, tid, sk):
        if tid not in self._statuses:
            return "unknown"
        self.stopped.append(tid)
        return "stopped" if tid in self._running else "not_running"


def _write_manifest(workspace, name, run_id, *, status, final_output=None, task="the task"):
    d = Path(workspace) / "workflows-runs" / name
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "schema": 2, "run_id": run_id, "workflow": name, "status": status,
        "root_session_key": SESSION, "started_at": time.time(),
        "finished_at": None if status == "running" else time.time(),
        "ts": time.time(), "task": task,
        "runs": [{"node_id": "n1", "iteration": 1, "status": "ok", "session_key": f"workflow:{run_id}:n1:1"}],
    }
    if final_output is not None:
        rec["final_output"] = final_output
    (d / f"{run_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def _tool(workspace, manager, jobs=None):
    t = TasksTool(workspace=str(workspace), subagent_manager=manager, sessions=None, jobs=jobs)
    t.set_context(RequestContext(channel="websocket", chat_id="chatA", session_key=SESSION))
    return t


def _job_registry(tmp_path):
    from durin.jobs.registry import JobRegistry
    return JobRegistry(tmp_path / "jobs.db")


@pytest.mark.asyncio
async def test_list_shows_both_kinds(tmp_path):
    mgr = _FakeManager([_SAStatus("sa01", "research", "awaiting_tools")], running=["sa01"])
    _write_manifest(tmp_path, "qa", "wf01abcd", status="running")
    out = await _tool(tmp_path, mgr).execute(action="list")
    assert "background task(s)" in out
    assert "sa01" in out and "subagent" in out
    assert "wf01abcd" in out and "workflow" in out


@pytest.mark.asyncio
async def test_list_shows_the_job_kind_too(tmp_path):
    """convert_to_markdown's tool description already tells the model a large
    scanned PDF "is transcribed as a background job you can follow with the
    tasks tool" -- list must actually carry it, or that description is a lie
    the model repeats to the user."""
    jobs = _job_registry(tmp_path)
    jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)
    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(action="list")
    assert "book.pdf" in out and "job" in out


@pytest.mark.asyncio
async def test_status_subagent_detail(tmp_path):
    mgr = _FakeManager(
        [_SAStatus("sa01", "research", "awaiting_tools", iteration=3,
                   tool_events=[{"name": "grep", "status": "ok", "detail": "x"}])],
        running=["sa01"],
    )
    out = await _tool(tmp_path, mgr).execute(action="status", id="sa01")
    assert "Sub-agent [sa01]" in out
    assert "phase:" in out and "iteration: 3" in out
    assert "grep" in out


@pytest.mark.asyncio
async def test_status_workflow_detail_includes_final_output(tmp_path):
    _write_manifest(tmp_path, "qa", "wf01abcd", status="completed", final_output="THE ANSWER 42")
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="status", id="wf01abcd")
    assert "Workflow run [wf01abcd]" in out
    assert "status: done" in out
    assert "THE ANSWER 42" in out


@pytest.mark.asyncio
async def test_status_unknown_id(tmp_path):
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="status", id="nope")
    assert "unknown task id" in out


@pytest.mark.asyncio
async def test_status_job_detail_shows_progress(tmp_path):
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)
    jobs.record_unit(job.id, 1, "page one text")
    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="status", id=job.id)
    assert f"Job [{job.id}]" in out
    assert "1/40" in out


@pytest.mark.asyncio
async def test_status_of_a_queued_job_says_it_is_waiting_and_shows_no_clock(tmp_path):
    """A job behind the OCR cap has not started. Reporting an age for it reads
    as time spent transcribing, which is exactly what nobody is doing."""
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="status", id=job.id)

    assert "status: queued" in out
    assert "age:" not in out
    assert "waiting" in out
    # Progress still shows: the denominator is what the job will be.
    assert "0/40" in out


@pytest.mark.asyncio
async def test_list_counts_a_queued_job_as_neither_running_nor_finished(tmp_path):
    """The header's two buckets used to absorb queued rows into "finished",
    which is the opposite of true for work that has not started."""
    jobs = _job_registry(tmp_path)
    started = jobs.enqueue(
        kind="ocr", label="first.pdf", payload={}, session_key=SESSION, units_total=1)
    jobs.claim(started.id, pid=4242)
    jobs.enqueue(kind="ocr", label="second.pdf", payload={}, session_key=SESSION, units_total=1)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(action="list")

    assert "1 running, 1 queued, 0 finished" in out
    assert "queued" in out.split("\n")[-1] or "queued" in out


@pytest.mark.asyncio
async def test_list_shows_no_age_for_a_queued_job(tmp_path):
    """The STATUS render already withholds the clock for a queued job (an age
    beside a job reads as time spent working, and a queued job has no worker
    at all); the LIST line must not print one either. The blank cell keeps
    the label column aligned with the other rows."""
    jobs = _job_registry(tmp_path)
    running = jobs.enqueue(
        kind="ocr", label="first.pdf", payload={}, session_key=SESSION, units_total=1)
    jobs.claim(running.id, pid=4242)
    queued = jobs.enqueue(
        kind="ocr", label="second.pdf", payload={}, session_key=SESSION, units_total=1)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(action="list")

    queued_line = next(ln for ln in out.split("\n") if queued.id in ln)
    running_line = next(ln for ln in out.split("\n") if running.id in ln)
    assert "age=" not in queued_line
    assert "age=" in running_line
    assert queued_line.index("second.pdf") == running_line.index("first.pdf")


@pytest.mark.asyncio
async def test_list_says_nothing_about_queueing_when_nothing_is_queued(tmp_path):
    """The count only earns its place when there is one -- an always-present
    "0 queued" is noise in a line the model reads on every check."""
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=1)
    jobs.claim(job.id, pid=4242)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(action="list")

    assert "1 running, 0 finished" in out


@pytest.mark.asyncio
async def test_status_of_a_failed_job_says_why_it_failed(tmp_path):
    """"failed" on its own is not an answer anyone can act on. The worker
    already records a usable reason; this is the surface that has to show it."""
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)
    jobs.claim(job.id, pid=4242)
    jobs.finish(job.id, pid=4242, error="page 7: OSError: no space left on device")

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="status", id=job.id)

    assert "no space left on device" in out


@pytest.mark.asyncio
async def test_stop_subagent_delegates_to_manager(tmp_path):
    mgr = _FakeManager([_SAStatus("sa01", "research", "awaiting_tools")], running=["sa01"])
    out = await _tool(tmp_path, mgr).execute(action="stop", id="sa01")
    assert "cancelled" in out
    assert mgr.stopped == ["sa01"]


@pytest.mark.asyncio
async def test_stop_running_workflow_requests_cancel(tmp_path):
    _write_manifest(tmp_path, "qa", "wf99cancel", status="running")
    try:
        out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="stop", id="wf99cancel")
        assert "asked to cancel" in out
        assert cancellation.is_cancelled("wf99cancel") is True
    finally:
        cancellation.clear("wf99cancel")


@pytest.mark.asyncio
async def test_stop_finished_workflow_is_noop(tmp_path):
    _write_manifest(tmp_path, "qa", "wfdone001", status="completed", final_output="done")
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="stop", id="wfdone001")
    assert "already" in out
    assert cancellation.is_cancelled("wfdone001") is False


@pytest.mark.asyncio
async def test_stop_running_job_requests_cancel(tmp_path):
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)
    # Claimed, so this really is the running path: the row used to reach the
    # tray as "running" whether or not anything had claimed it.
    jobs.claim(job.id, pid=4242)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="stop", id=job.id)

    assert "asked to cancel" in out
    assert "page boundary" in out
    assert jobs.get(job.id).status == "cancelled"
    # Unlike a sub-agent or workflow run, nothing in durin/jobs ever publishes
    # a follow-up message -- the stop message must not claim one is coming.
    assert "follow-up" not in out


@pytest.mark.asyncio
async def test_stop_queued_job_cancels_it_outright(tmp_path):
    """A job waiting for the OCR slot is the most cancellable state there is:
    nothing has claimed it, and `claim` only ever moves a row out of "queued",
    so the cancel is final with no page boundary to wait for. Naming it
    "queued" in the tray must not make it look uncancellable."""
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="stop", id=job.id)

    assert jobs.get(job.id).status == "cancelled"
    assert "nothing to cancel" not in out
    assert "page boundary" not in out


@pytest.mark.asyncio
async def test_stop_finished_job_is_noop(tmp_path):
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=1)
    jobs.claim(job.id, pid=4242)
    jobs.finish(job.id, pid=4242)
    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="stop", id=job.id)
    assert "already" in out
    assert jobs.get(job.id).status == "done"  # unchanged, not force-cancelled


@pytest.mark.asyncio
async def test_unknown_action(tmp_path):
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="frobnicate")
    assert "unknown action" in out


@pytest.mark.asyncio
async def test_status_workflow_shows_work_dir_durations_and_files(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "context.json").write_text("{}")
    _write_manifest(tmp_path, "qa", "wf01abcd", status="running")
    # Enrich the manifest with the fields the engine now records.
    p = tmp_path / "workflows-runs" / "qa" / "wf01abcd.json"
    rec = json.loads(p.read_text())
    rec["work_dir"] = str(wd)
    rec["runs"][0]["duration_s"] = 3.2
    p.write_text(json.dumps(rec), encoding="utf-8")
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="status", id="wf01abcd")
    assert f"work dir: {wd}" in out
    assert "(3.2s)" in out
    assert "context.json" in out and "2 B" in out


@pytest.mark.asyncio
async def test_status_workflow_shows_missing_declared_artifacts(tmp_path):
    _write_manifest(tmp_path, "qa", "wf01abcd", status="completed", final_output="ok")
    p = tmp_path / "workflows-runs" / "qa" / "wf01abcd.json"
    rec = json.loads(p.read_text())
    rec["missing_artifacts"] = ["evidence.json"]
    p.write_text(json.dumps(rec), encoding="utf-8")
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="status", id="wf01abcd")
    assert "evidence.json" in out and "not produced" in out.lower()


def _set_manifest_owner(workspace, name, run_id, owner):
    f = Path(workspace) / "workflows-runs" / name / f"{run_id}.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["owner"] = owner
    f.write_text(json.dumps(rec), encoding="utf-8")


_DEAD_OWNER = {"pid": 2**22 + 999, "started": "never"}


@pytest.mark.asyncio
async def test_stop_ghost_run_self_heals(tmp_path):
    """A "running" manifest whose owning process died (gateway crash) is
    repaired on stop — honest answer, no 'asked to cancel' for a corpse."""
    from durin.workflow import run_log

    _write_manifest(tmp_path, "qa", "ghost123", status="running")
    _set_manifest_owner(tmp_path, "qa", "ghost123", _DEAD_OWNER)
    out = await _tool(tmp_path, None).execute(action="stop", id="ghost123")
    assert "gone" in out or "crashed" in out
    assert "asked to cancel" not in out
    assert run_log.read_manifest(tmp_path, "qa", "ghost123")["status"] == "crashed"


@pytest.mark.asyncio
async def test_status_ghost_run_self_heals(tmp_path):
    from durin.workflow import run_log

    _write_manifest(tmp_path, "qa", "ghost456", status="running")
    _set_manifest_owner(tmp_path, "qa", "ghost456", _DEAD_OWNER)
    out = await _tool(tmp_path, None).execute(action="status", id="ghost456")
    assert "crashed" in out
    assert run_log.read_manifest(tmp_path, "qa", "ghost456")["status"] == "crashed"


@pytest.mark.asyncio
async def test_stop_live_owner_run_still_cancels(tmp_path):
    """A running manifest owned by a LIVE process keeps the normal cancel path."""
    import os

    from durin.utils.process_tree import process_identity

    _write_manifest(tmp_path, "qa", "alive789", status="running")
    _set_manifest_owner(tmp_path, "qa", "alive789", process_identity(os.getpid()))
    try:
        out = await _tool(tmp_path, None).execute(action="stop", id="alive789")
        assert "asked to cancel" in out
    finally:
        # No engine consumes this flag in the test — drop it so the global
        # cancellation registry stays empty for later tests.
        cancellation.clear("alive789")


@pytest.mark.asyncio
async def test_create_wires_a_real_jobs_registry(tmp_path, monkeypatch):
    """TasksTool.create(ctx) is the path the real agent loop uses to build
    every tool — proves the wiring the loop actually exercises, not just that
    __init__ accepts a jobs= argument when a test passes one explicitly."""
    from types import SimpleNamespace

    from durin.config.paths import jobs_db_path
    from durin.jobs.registry import JobRegistry

    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    JobRegistry(jobs_db_path()).enqueue(
        kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=5,
    )

    tool = TasksTool.create(SimpleNamespace(workspace=str(tmp_path)))
    tool.set_context(RequestContext(channel="websocket", chat_id="chatA", session_key=SESSION))
    out = await tool.execute(action="list")
    assert "book.pdf" in out


class _CountingPopen:
    """Stands in for the ``subprocess`` module inside ``durin.jobs.spawn`` so a
    test can assert how many workers were actually launched (see the sibling
    convention in test_ingestion_ocr.py)."""

    def __init__(self):
        self.calls = 0

    def Popen(self, *args, **kwargs):
        self.calls += 1
        return None


def _failed_job(jobs, *, units_done=0, error="page 3: engine exploded"):
    job = jobs.enqueue(
        kind="ocr", label="book.pdf", payload={"path": "/tmp/book.pdf"},
        session_key=SESSION, units_total=40,
    )
    jobs.claim(job.id, pid=4242)
    for unit in range(1, units_done + 1):
        jobs.record_unit(job.id, unit, f"page {unit}")
    jobs.finish(job.id, pid=4242, error=error)
    return job


@pytest.mark.asyncio
async def test_retry_of_a_failed_job_requeues_it_and_launches_a_worker(
    tmp_path, monkeypatch
):
    """The whole recovery in one gesture: the row goes back to queued with its
    finished pages kept, exactly one worker process is launched for it, and
    the response promises "queued" (the cap may refuse the claim) rather than
    "running", naming the progress that survives. The start promise is hedged
    with the periodic sweep, so it stays true even if this launch dies."""
    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    jobs = _job_registry(tmp_path)
    job = _failed_job(jobs, units_done=3)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="retry", id=job.id)

    assert jobs.get(job.id).status == "queued"
    assert launcher.calls == 1
    assert "requeued" in out
    assert "3/40" in out
    assert "running" not in out.lower()
    # The launch Popen can fail (swallowed by respawn's contract), so the
    # start promise must carry the sweep backstop instead of "immediately"
    # alone.
    assert "sweep" in out
    # Same contract as stop's wording: nothing pushes a completion message.
    assert "action=status" in out


@pytest.mark.asyncio
async def test_retry_refuses_a_running_job_without_spawning(tmp_path, monkeypatch):
    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)
    jobs.claim(job.id, pid=4242)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="retry", id=job.id)

    assert "is running" in out
    assert "only a failed or cancelled job can be retried" in out
    assert jobs.get(job.id).status == "running"
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_retry_refuses_a_queued_job(tmp_path, monkeypatch):
    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="retry", id=job.id)

    assert "is queued" in out
    assert jobs.get(job.id).status == "queued"
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_retry_of_a_done_job_points_at_the_finished_transcription(
    tmp_path, monkeypatch
):
    """A done job has nothing left to run: its transcription is on disk and
    indexed, and a re-ingest of the same document short-circuits on that
    sidecar and returns it — it does NOT spawn a fresh job. The refusal must
    promise exactly that outcome and nothing more."""
    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=1)
    jobs.claim(job.id, pid=4242)
    jobs.finish(job.id, pid=4242)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="retry", id=job.id)

    assert "is done" in out
    assert "already in the Library" in out
    assert "ingesting the document again returns it" in out
    # A re-ingest of a finished document is a no-op return — the refusal must
    # not claim it spawns anything.
    assert "fresh job" not in out
    assert jobs.get(job.id).status == "done"
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_retry_refuses_a_subagent_and_a_workflow(tmp_path):
    mgr = _FakeManager([_SAStatus("sa01", "research", "error")], running=[])
    _write_manifest(tmp_path, "qa", "wf01abcd", status="aborted")

    tool = _tool(tmp_path, mgr)
    for task_id, kind in (("sa01", "subagent"), ("wf01abcd", "workflow")):
        out = await tool.execute(action="retry", id=task_id)
        assert "retry only applies to background jobs" in out
        assert kind in out


@pytest.mark.asyncio
async def test_retry_of_an_unknown_id_is_the_ordinary_unknown_id_error(tmp_path):
    """A pruned terminal row is exactly an id the registry no longer has — the
    resolver's existing miss path must answer for it, no dedicated branch."""
    jobs = _job_registry(tmp_path)
    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="retry", id="pruned0000")
    assert "unknown task id" in out


@pytest.mark.asyncio
async def test_retry_that_loses_the_requeue_race_reports_the_fresh_status_and_never_spawns(
    tmp_path, monkeypatch
):
    """Between the tool's read (row: failed) and its requeue, another actor can
    move the row — here a competing retry lands first. The guarded UPDATE then
    writes nothing, and the tool must answer with the row's fresh status
    instead of respawning a worker for a requeue that never happened."""
    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    jobs = _job_registry(tmp_path)
    job = _failed_job(jobs)

    real_requeue = jobs.requeue

    def _racing_requeue(job_id):
        real_requeue(job_id)  # the competing retry lands first
        return real_requeue(job_id)  # this call's own write finds nothing to do

    monkeypatch.setattr(jobs, "requeue", _racing_requeue)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="retry", id=job.id)

    assert "is queued" in out
    assert launcher.calls == 0


@pytest.mark.asyncio
async def test_retry_requires_an_id(tmp_path):
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="retry")
    assert "'id' is required" in out


@pytest.mark.asyncio
async def test_description_action_enum_and_unknown_action_all_name_retry(tmp_path):
    tool = _tool(tmp_path, _FakeManager([], running=[]))
    assert "retry" in tool.description
    assert "retry" in tool.parameters["properties"]["action"]["enum"]
    assert "retry" in tool.parameters["properties"]["action"]["description"]
    out = await tool.execute(action="frobnicate")
    assert "retry" in out


@pytest.mark.asyncio
async def test_failed_job_status_names_the_recovery_and_keeps_the_stored_error_verbatim(
    tmp_path
):
    """The render layer is where recovery advice belongs: only the model reads
    it. The stored error is shown verbatim to humans in the webui tray, so
    "action=retry" must appear next to the error line, never inside it."""
    jobs = _job_registry(tmp_path)
    job = _failed_job(jobs, error="page 7: OSError: no space left on device")

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="status", id=job.id)

    assert "action=retry" in out
    assert "re-ingest" in out
    # Outcome phrasing only: what a re-ingest does internally depends on what
    # the failed attempt left on disk (it is a fresh job only when no sidecar
    # exists), so the advice must not claim a mechanism.
    assert "fresh job" not in out
    assert "page 7: OSError: no space left on device" in out
    assert jobs.get(job.id).error == "page 7: OSError: no space left on device"


@pytest.mark.asyncio
async def test_cancelled_job_status_names_the_recovery_too(tmp_path):
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)
    jobs.claim(job.id, pid=4242)
    jobs.cancel(job.id)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="status", id=job.id)

    assert "action=retry" in out


@pytest.mark.asyncio
async def test_a_running_job_status_offers_no_retry(tmp_path):
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION, units_total=40)
    jobs.claim(job.id, pid=4242)

    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="status", id=job.id)

    assert "action=retry" not in out


def test_description_does_not_promise_a_notification_for_a_finished_job(tmp_path):
    """Sub-agents and workflow runs genuinely publish a follow-up message on
    completion (durin/agent/subagent.py, durin/agent/tools/run_workflow.py);
    nothing in durin/jobs publishes anything at all. Extending the same
    "arrives on its own, do not loop waiting" promise to jobs would tell the
    model to end its turn and wait for a message that never comes."""
    tool = _tool(tmp_path, _FakeManager([], running=[]))
    desc = tool.description
    assert "job" in desc.lower()
    assert "a job pushes nothing" in desc.lower()


def test_id_parameter_description_names_all_three_kinds(tmp_path):
    tool = _tool(tmp_path, _FakeManager([], running=[]))
    id_desc = tool.parameters["properties"]["id"]["description"].lower()
    assert "sub-agent id" in id_desc
    assert "workflow run id" in id_desc
    assert "job id" in id_desc


@pytest.mark.asyncio
async def test_stop_force_requests_a_hard_cancel(tmp_path):
    _write_manifest(tmp_path, "qa", "wfforce01", status="running")
    try:
        out = await _tool(tmp_path, _FakeManager([], running=[])).execute(
            action="stop", id="wfforce01", force=True)
        assert "force-stopped" in out
        assert cancellation.is_hard_cancelled("wfforce01") is True
    finally:
        cancellation.clear("wfforce01")


@pytest.mark.asyncio
async def test_a_plain_stop_stays_graceful(tmp_path):
    """The default must not interrupt an in-flight node: only the plain flag is
    set, so the hard-only poll the engine hands work nodes stays false."""
    _write_manifest(tmp_path, "qa", "wfsoft001", status="running")
    try:
        await _tool(tmp_path, _FakeManager([], running=[])).execute(
            action="stop", id="wfsoft001")
        assert cancellation.is_cancelled("wfsoft001") is True
        assert cancellation.is_hard_cancelled("wfsoft001") is False
    finally:
        cancellation.clear("wfsoft001")


@pytest.mark.asyncio
async def test_repeat_stop_escalates_to_hard(tmp_path):
    """First stop is graceful; a second stop on the (now "stopping") run means
    "stop it NOW" and escalates without needing force=true."""
    _write_manifest(tmp_path, "qa", "wfrepeat01", status="running")
    tool = _tool(tmp_path, _FakeManager([], running=[]))
    try:
        first = await tool.execute(action="stop", id="wfrepeat01")
        assert "asked to cancel" in first
        assert cancellation.is_hard_cancelled("wfrepeat01") is False
        second = await tool.execute(action="stop", id="wfrepeat01")
        assert "force-stopped" in second
        assert cancellation.is_hard_cancelled("wfrepeat01") is True
    finally:
        cancellation.clear("wfrepeat01")


@pytest.mark.asyncio
async def test_list_counts_a_stopping_run_as_running(tmp_path):
    """A run winding down has not finished. Counting it among "finished" would
    tell the model the work is over while a node is still executing."""
    _write_manifest(tmp_path, "qa", "wfwind0001", status="running")
    tool = _tool(tmp_path, _FakeManager([], running=[]))
    try:
        cancellation.request_cancel("wfwind0001")
        out = await tool.execute(action="list")
        assert "1 running, 0 finished" in out
        assert "stopping" in out
    finally:
        cancellation.clear("wfwind0001")


@pytest.mark.asyncio
async def test_retry_refuses_a_stopping_run(tmp_path):
    """stop and retry compose: a run winding down is not a finished lifecycle,
    so it is not retryable (and retry is a jobs-only action besides)."""
    _write_manifest(tmp_path, "qa", "wfretrystop", status="running")
    tool = _tool(tmp_path, _FakeManager([], running=[]))
    try:
        cancellation.request_cancel("wfretrystop")
        out = await tool.execute(action="retry", id="wfretrystop")
        assert "retry only applies to background jobs" in out
    finally:
        cancellation.clear("wfretrystop")


@pytest.mark.asyncio
async def test_force_on_a_subagent_says_it_was_not_honored(tmp_path):
    """force only escalates a workflow stop. Answering a sub-agent stop with the
    plain wording would let the model believe the flag did something."""
    mgr = _FakeManager([_SAStatus("sa01", "research", "awaiting_tools")], running=["sa01"])
    out = await _tool(tmp_path, mgr).execute(action="stop", id="sa01", force=True)
    assert "Sub-agent [sa01] cancelled" in out
    assert "force" in out and "workflow" in out


@pytest.mark.asyncio
async def test_force_on_a_job_says_it_was_not_honored(tmp_path):
    """Same for a job: its cancel always waits for the current page boundary,
    and force cannot shorten that."""
    jobs = _job_registry(tmp_path)
    job = jobs.enqueue(kind="ocr", label="book.pdf", payload={}, session_key=SESSION,
                       units_total=40)
    out = await _tool(tmp_path, _FakeManager([], running=[]), jobs=jobs).execute(
        action="stop", id=job.id, force=True)
    assert "cancelled while it was still waiting" in out
    assert "force" in out and "workflow" in out


@pytest.mark.asyncio
async def test_a_plain_subagent_stop_does_not_mention_force(tmp_path):
    """The clause is only for a dropped flag — a stop that never passed force
    must not carry noise about it."""
    mgr = _FakeManager([_SAStatus("sa01", "research", "awaiting_tools")], running=["sa01"])
    out = await _tool(tmp_path, mgr).execute(action="stop", id="sa01")
    assert "force" not in out


@pytest.mark.asyncio
async def test_force_parameter_is_declared_and_documented(tmp_path):
    tool = _tool(tmp_path, _FakeManager([], running=[]))
    force = tool.parameters["properties"]["force"]
    assert "boolean" in force["type"]
    assert "force" in tool.description
