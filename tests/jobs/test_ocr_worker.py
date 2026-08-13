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


def _enqueue(registry, pdf, pages, *, sidecar_dir=None):
    payload = {"path": str(pdf), "pages": pages}
    if sidecar_dir is not None:
        payload["sidecar_dir"] = str(sidecar_dir)
    return registry.enqueue(
        kind="ocr",
        label=pdf.name,
        payload=payload,
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


def test_worker_writes_the_sidecar_with_pages_merged_in_order(registry, tmp_path, monkeypatch):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "mixed.pdf"
    _write_text_pdf(pdf, ["Real text on page one", "", "Real text on page three"])
    sidecar_dir = tmp_path / "entry"
    sidecar_dir.mkdir()

    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"OCR'd text for page {page}",
    )
    job = _enqueue(registry, pdf, [2], sidecar_dir=sidecar_dir)

    run_job(job.id, registry=registry)

    assert registry.get(job.id).status == "done"
    content = (sidecar_dir / "source.md").read_text()
    assert content == (
        "Real text on page one\n\nOCR'd text for page 2\n\nReal text on page three"
    )


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


def test_worker_records_a_failure_when_the_sidecar_write_breaks(registry, scanned_pdf, tmp_path, monkeypatch):
    # The per-page loop above this step is guarded; this step must be too.
    # An unguarded failure here would escape run_job entirely, skip
    # registry.finish(), and leave the job stuck at "running" forever — a
    # resumed run would find `todo` empty and hit the same break again on
    # every retry, with no error ever recorded.
    sidecar_dir = tmp_path / "entry"
    sidecar_dir.mkdir()

    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )

    def _boom(path):
        raise OSError("disk gone")

    monkeypatch.setattr("durin.jobs.ocr_worker.page_texts", _boom)
    job = _enqueue(registry, scanned_pdf, [1], sidecar_dir=sidecar_dir)

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "failed"
    assert "disk gone" in reread.error
    # The page was safely transcribed and recorded before the sidecar step
    # broke — a retry would not need to redo it.
    assert registry.done_units(job.id) == {1}
    assert not (sidecar_dir / "source.md").exists()


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


def test_worker_respects_a_cancellation_that_landed_before_it_started(registry, scanned_pdf, monkeypatch):
    called = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: called.append(page) or f"page {page}",
    )
    job = _enqueue(registry, scanned_pdf, [1, 2, 3])
    registry.cancel(job.id)

    run_job(job.id, registry=registry)

    assert registry.get(job.id).status == "cancelled"
    assert called == []
    assert registry.done_units(job.id) == set()


def test_worker_does_not_reclaim_a_job_already_running_elsewhere(registry, scanned_pdf, monkeypatch):
    called = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: called.append(page) or f"page {page}",
    )
    job = _enqueue(registry, scanned_pdf, [1, 2, 3])
    registry.claim(job.id, pid=999999)  # another process already owns this job

    run_job(job.id, registry=registry)

    assert called == []
    reread = registry.get(job.id)
    assert reread.status == "running"
    assert reread.pid == 999999


def test_worker_on_an_unknown_job_is_a_noop(registry):
    run_job("does-not-exist", registry=registry)  # must not raise
