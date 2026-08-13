"""The OCR worker: transcription, resumption, failure handling."""

import pytest

from durin.jobs.ocr_worker import run_job
from durin.jobs.registry import JobRegistry


@pytest.fixture()
def registry(tmp_path):
    return JobRegistry(tmp_path / "jobs.db")


@pytest.fixture()
def scanned_pdf(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "book.pdf"
    _write_text_pdf(pdf, ["", "", ""])
    return pdf


def _enqueue(registry, pdf, pages):
    return registry.enqueue(
        kind="ocr",
        label=pdf.name,
        payload={"path": str(pdf), "pages": pages},
        session_key="chat:1",
        units_total=len(pages),
    )


def test_worker_transcribes_every_requested_page(registry, scanned_pdf, monkeypatch):
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"text of page {page}",
    )
    job = _enqueue(registry, scanned_pdf, [1, 2, 3])

    run_job(job.id, registry=registry)

    assert registry.get(job.id).status == "done"
    assert registry.units(job.id) == [
        (1, "text of page 1"), (2, "text of page 2"), (3, "text of page 3"),
    ]


def test_worker_marks_itself_running_with_its_pid(registry, scanned_pdf, monkeypatch):
    seen = {}

    def _capture(path, page, **kw):
        seen["status"] = registry.get(job.id).status
        seen["pid"] = registry.get(job.id).pid
        return "x"

    monkeypatch.setattr("durin.jobs.ocr_worker.transcribe_page", _capture)
    job = _enqueue(registry, scanned_pdf, [1])

    run_job(job.id, registry=registry)

    assert seen["status"] == "running"
    assert seen["pid"] is not None


def test_worker_skips_pages_already_transcribed(registry, scanned_pdf, monkeypatch):
    called = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: called.append(page) or f"page {page}",
    )
    job = _enqueue(registry, scanned_pdf, [1, 2, 3])
    registry.record_unit(job.id, 1, "already done before the crash")

    run_job(job.id, registry=registry)

    assert called == [2, 3]
    assert registry.units(job.id)[0] == (1, "already done before the crash")


def test_worker_records_a_failure_and_keeps_finished_pages(registry, scanned_pdf, monkeypatch):
    def _boom(path, page, **kw):
        if page == 2:
            raise RuntimeError("engine exploded")
        return f"page {page}"

    monkeypatch.setattr("durin.jobs.ocr_worker.transcribe_page", _boom)
    job = _enqueue(registry, scanned_pdf, [1, 2, 3])

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "failed"
    assert "engine exploded" in reread.error
    assert registry.done_units(job.id) == {1}


def test_worker_stops_when_the_job_is_cancelled_midway(registry, scanned_pdf, monkeypatch):
    def _cancel_after_first(path, page, **kw):
        if page == 1:
            registry.cancel(job.id)
        return f"page {page}"

    monkeypatch.setattr("durin.jobs.ocr_worker.transcribe_page", _cancel_after_first)
    job = _enqueue(registry, scanned_pdf, [1, 2, 3])

    run_job(job.id, registry=registry)

    assert registry.get(job.id).status == "cancelled"
    assert registry.done_units(job.id) == {1}


def test_worker_on_an_unknown_job_is_a_noop(registry):
    run_job("does-not-exist", registry=registry)  # must not raise
