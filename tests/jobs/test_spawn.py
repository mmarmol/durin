"""Starting a job's worker process, including restarting one after reconcile."""

import subprocess
import sys

import pytest

from durin.jobs.registry import Job, JobRegistry
from durin.jobs.spawn import (
    MAX_CONCURRENT_OCR_JOBS,
    _launch_worker,
    respawn,
    spawn_ocr_job,
)
from durin.utils.process import pid_alive


def _job(**overrides):
    fields = dict(
        id="j1", kind="ocr", status="queued", label="book.pdf",
        payload={"path": "/tmp/book.pdf", "pages": [1, 2], "sidecar_dir": None},
        session_key="chat:1", units_total=2, units_done=0, pid=None,
        created_at=1.0, started_at=None, ended_at=None, error=None,
    )
    fields.update(overrides)
    return Job(**fields)


# ---------------------------------------------------------------------------
# _launch_worker — the Popen call shared by spawn_ocr_job, respawn, and the
# worker's own chain
# ---------------------------------------------------------------------------


def test_max_concurrent_ocr_jobs_is_one():
    # No config knob (YAGNI) -- this pins the measured-rationale constant
    # itself, so a change to it is a deliberate edit, not a silent drift.
    assert MAX_CONCURRENT_OCR_JOBS == 1


def test_launch_worker_starts_the_ocr_worker_module_with_the_job_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append((args, kw)),
    )
    _launch_worker("j99")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [sys.executable, "-m", "durin.jobs.ocr_worker", "j99"]
    assert kwargs.get("start_new_session") is True


def test_launch_worker_raises_on_a_popen_failure(monkeypatch):
    # Unlike spawn_ocr_job/respawn, _launch_worker itself does not swallow --
    # each of its three callers has its own log message and its own contract
    # for what "queued" means afterward, so the decision to catch and how to
    # log it stays with them.
    def _boom(*_args, **_kwargs):
        raise OSError("no more processes")

    monkeypatch.setattr("durin.jobs.spawn.subprocess.Popen", _boom)
    with pytest.raises(OSError):
        _launch_worker("j1")


# ---------------------------------------------------------------------------
# spawn_ocr_job
# ---------------------------------------------------------------------------


def test_the_job_is_labelled_with_the_name_it_is_given(tmp_path, monkeypatch):
    """The path the worker gets is the ingested entry's normalized copy, and
    every one of those is named source.pdf -- so the label cannot be derived
    from it, or two books in one session read identically in the tray."""
    registry = JobRegistry(tmp_path / "jobs.db")
    monkeypatch.setattr("durin.jobs.spawn.subprocess.Popen", lambda args, **kw: None)
    entry_dir = tmp_path / "ingested" / "a3f9c0112b44"
    entry_dir.mkdir(parents=True)

    job = spawn_ocr_job(
        registry=registry, pdf_path=entry_dir / "source.pdf", pages=[1, 2],
        session_key="chat:1", sidecar_dir=entry_dir,
        label="Critique of Pure Reason.pdf",
    )

    assert job.label == "Critique of Pure Reason.pdf"
    assert job.payload["path"].endswith("source.pdf")  # the worker still reads the copy


# ---------------------------------------------------------------------------
# respawn — dispatch and process-launch behaviour (subprocess.Popen mocked)
# ---------------------------------------------------------------------------


def test_respawn_starts_the_ocr_worker_with_the_job_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append((args, kw)),
    )
    respawn(_job(id="j42"))
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [sys.executable, "-m", "durin.jobs.ocr_worker", "j42"]
    assert kwargs.get("start_new_session") is True


def test_respawn_raises_on_an_unknown_kind(monkeypatch):
    calls = []
    monkeypatch.setattr("durin.jobs.spawn.subprocess.Popen", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError):
        respawn(_job(kind="reindex"))
    assert calls == []  # no worker started for a kind respawn doesn't know


def test_respawn_does_not_raise_when_the_launch_fails(monkeypatch):
    # Mirrors spawn_ocr_job's own contract: a launch failure is logged, not
    # raised — the row stays queued and a later reconcile pass tries again.
    def _boom(*_args, **_kwargs):
        raise OSError("no more processes")

    monkeypatch.setattr("durin.jobs.spawn.subprocess.Popen", _boom)
    respawn(_job())  # must not raise


# ---------------------------------------------------------------------------
# reconcile -> respawn: the property that matters is that a reconciled job
# actually gets a live worker again, not just that the row flips to queued.
# ---------------------------------------------------------------------------


def test_a_reconciled_job_is_respawned_with_its_own_id(tmp_path, monkeypatch):
    registry = JobRegistry(tmp_path / "jobs.db")
    job = registry.enqueue(
        kind="ocr", label="book.pdf",
        payload={"path": "/tmp/book.pdf", "pages": [1, 2, 3], "sidecar_dir": None},
        session_key="chat:1", units_total=3,
    )
    registry.claim(job.id, pid=999999)
    registry.record_unit(job.id, 1, "page one")  # progress before the crash

    calls = []
    monkeypatch.setattr(
        "durin.jobs.spawn.subprocess.Popen",
        lambda args, **kw: calls.append(args),
    )

    orphans = registry.reconcile(alive=pid_alive)  # 999999 is not alive
    for orphan in orphans:
        respawn(orphan)

    assert calls == [[sys.executable, "-m", "durin.jobs.ocr_worker", job.id]]
    reread = registry.get(job.id)
    assert reread.status == "queued"  # ready for the new worker to claim
    assert reread.units_done == 1  # finished work survives the restart


def test_a_reconciled_job_gets_a_real_live_worker_again(tmp_path, monkeypatch):
    """End-to-end with a real subprocess: reconcile finds the orphaned job,
    respawn launches a real ``python -m durin.jobs.ocr_worker`` process for
    it, and that process claims and finishes the job for real.

    An empty page list keeps this independent of the OCR engine (already
    exercised, mocked, in test_ocr_worker.py) while still proving the exact
    property this task adds: a reconciled job gets a live worker again, not
    merely a status flip. The document itself has to be real and readable
    though — a worker that cannot read the document it was given cannot work
    out what it was meant to transcribe, and fails the job rather than
    reporting a success it did not have.
    """
    monkeypatch.setenv("DURIN_HOME", str(tmp_path))
    from durin.config.paths import jobs_db_path
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "book.pdf"
    _write_text_pdf(pdf, ["Plenty of ordinary body text on this page"])
    registry = JobRegistry(jobs_db_path())
    job = registry.enqueue(
        kind="ocr", label="book.pdf",
        payload={"path": str(pdf), "pages": [], "sidecar_dir": None},
        session_key="chat:1", units_total=0,
    )
    registry.claim(job.id, pid=999999)  # the "old" worker that crashed

    real_popen = subprocess.Popen
    procs: list[subprocess.Popen] = []

    def _capturing_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr("durin.jobs.spawn.subprocess.Popen", _capturing_popen)

    orphans = registry.reconcile(alive=pid_alive)
    assert [o.id for o in orphans] == [job.id]
    for orphan in orphans:
        respawn(orphan)

    assert len(procs) == 1
    returncode = procs[0].wait(timeout=30)
    assert returncode == 0

    finished = registry.get(job.id)
    assert finished.status == "done"
    assert finished.pid is None  # finish() clears it
