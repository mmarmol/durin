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
    entry_dir, pdf = _ingested_entry(
        tmp_path / "ws", original_name="mixed.pdf",
        pages=("Real text on page one", "", "Real text on page three"),
    )

    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"OCR'd text for page {page}",
    )
    job = _enqueue(registry, pdf, [2], sidecar_dir=entry_dir)

    run_job(job.id, registry=registry)

    assert registry.get(job.id).status == "done"
    content = (entry_dir / "source.md").read_text()
    assert content == (
        "Real text on page one\n\nOCR'd text for page 2\n\nReal text on page three"
    )


def _ingested_entry(workspace, original_name="zorpbook.pdf", pages=("", "", "")):
    """Build the ``ingested/<id>/`` entry ``ingest_artifact`` leaves behind for
    a scanned PDF whose OCR was deferred: the verbatim original copied in as
    ``source.pdf``, plus the ``meta.json`` naming the file the user handed
    over (which is what the Library entry is titled after)."""
    import json

    from tests.tools.test_read_enhancements import _write_text_pdf

    entry_dir = workspace / "ingested" / "e1"
    entry_dir.mkdir(parents=True)
    pdf = entry_dir / "source.pdf"
    _write_text_pdf(pdf, list(pages))
    (entry_dir / "meta.json").write_text(
        json.dumps({
            "id": "e1",
            "derived": {"source_path": f"/somewhere/{original_name}", "size_bytes": 1},
        }),
        encoding="utf-8",
    )
    return entry_dir, pdf


def test_worker_makes_the_finished_transcription_searchable(
    registry, tmp_path, monkeypatch,
):
    """Writing the sidecar is not the end of the job: until the transcription
    is in the Library and indexed, the user's scanned book is still not
    findable, which is the only outcome they asked for."""
    from durin.memory.fts_index import FTSIndex

    ws = tmp_path / "ws"
    entry_dir, pdf = _ingested_entry(ws)
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"Page {page} of the zorptastic protocol.",
    )
    job = _enqueue(registry, pdf, [1, 2, 3], sidecar_dir=entry_dir)

    run_job(job.id, registry=registry)

    assert registry.get(job.id).status == "done"
    ref_md = ws / "memory" / "references" / "zorpbook.md"
    assert "zorptastic protocol" in ref_md.read_text(encoding="utf-8")
    with FTSIndex.open(ws) as idx:
        assert [h.uri for h in idx.search("zorptastic")] == ["reference:zorpbook"]


def test_worker_records_a_failure_when_the_transcription_produced_no_text(
    registry, tmp_path, monkeypatch,
):
    """An OCR pass that read nothing has nothing to put in the Library. The
    inline conversion path already refuses a document with no extractable
    text; a deferred one must not quietly report "done" over an empty entry
    the user would go looking for."""
    ws = tmp_path / "ws"
    entry_dir, pdf = _ingested_entry(ws)
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page", lambda path, page, **kw: "")
    job = _enqueue(registry, pdf, [1, 2, 3], sidecar_dir=entry_dir)

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "failed"
    assert "no text" in reread.error
    assert not (ws / "memory" / "references").exists()


def test_worker_records_a_failure_when_the_library_write_breaks(
    registry, tmp_path, monkeypatch,
):
    """Same contract as the sidecar step: an unguarded failure here would
    escape run_job, skip registry.finish() and wedge the job at "running"
    forever. A recorded failure is retryable -- and honest, because a job
    reported "done" whose document is not searchable is the exact defect this
    step exists to close."""
    ws = tmp_path / "ws"
    entry_dir, pdf = _ingested_entry(ws)
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )

    def _boom(entry_dir):
        raise OSError("references directory is read-only")

    monkeypatch.setattr("durin.jobs.ocr_worker.index_ingested_entry", _boom)
    job = _enqueue(registry, pdf, [1], sidecar_dir=entry_dir)

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "failed"
    assert "read-only" in reread.error
    # The transcription itself survived: a retry resumes instead of redoing it.
    assert registry.done_units(job.id) == {1}
    assert (entry_dir / "source.md").exists()


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


def _lose_the_claim_race_to_a_cancel(registry, monkeypatch):
    """Makes the worker's own claim() call, at the exact moment it runs,
    trigger a cancel first — indistinguishable from the outside from a real
    concurrent cancel landing in the window between the worker's status read
    and its claim, since claim()'s own conditional UPDATE is what decides
    who wins either way."""
    real_claim = registry.claim

    def _claim_after_a_race_lost_cancel(job_id, *, pid):
        registry.cancel(job_id)
        return real_claim(job_id, pid=pid)

    monkeypatch.setattr(registry, "claim", _claim_after_a_race_lost_cancel)


def test_worker_bails_when_a_cancel_wins_the_claim_race(registry, scanned_pdf, monkeypatch):
    """The interleaving that matters: status read says "queued", then a
    cancel lands, then the claim itself runs and — now that claim() is
    conditional on status='queued' — loses. The worker must not transcribe
    anything after that, and must not leave the row silently flipped back to
    "running"."""
    called = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: called.append(page) or f"page {page}",
    )
    job = _enqueue(registry, scanned_pdf, [1, 2, 3])
    _lose_the_claim_race_to_a_cancel(registry, monkeypatch)

    run_job(job.id, registry=registry)

    assert called == []
    reread = registry.get(job.id)
    assert reread.status == "cancelled"
    assert reread.pid is None


def test_worker_bails_when_a_cancel_wins_the_claim_race_with_no_pages_left(
    registry, scanned_pdf, monkeypatch,
):
    """The case that specifically requires checking claim()'s return value,
    rather than relying on the per-page loop's own cancellation check: with
    every page already done, `todo` is empty and that loop never runs at
    all, so a lost claim that went unchecked would fall straight through to
    `registry.finish(job_id, error=None)` — marking a cancelled job "done"."""
    job = _enqueue(registry, scanned_pdf, [1])
    registry.record_unit(job.id, 1, "already transcribed")
    _lose_the_claim_race_to_a_cancel(registry, monkeypatch)

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "cancelled"  # not silently marked "done"


def test_worker_on_an_unknown_job_is_a_noop(registry):
    run_job("does-not-exist", registry=registry)  # must not raise


def test_a_cancel_during_the_post_loop_work_is_not_overwritten(
    registry, tmp_path, monkeypatch,
):
    """The per-page loop is not the last thing that happens. After its final
    cancellation check the worker still writes the sidecar and hands the
    document to the Library, which chunks, FTS-indexes and embeds a whole book
    — minutes of work, all of it after the last chance to notice a cancel. A
    cancel landing in that window must survive the finish that follows it, and
    the worker must not report a success it did not have."""
    ws = tmp_path / "ws"
    entry_dir, pdf = _ingested_entry(ws)
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )

    def _cancel_midway(entry_dir):
        registry.cancel(job.id)

    monkeypatch.setattr("durin.jobs.ocr_worker.index_ingested_entry", _cancel_midway)
    job = _enqueue(registry, pdf, [1], sidecar_dir=entry_dir)

    emitted = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker._emit",
        lambda job_id, pages, resumed, started, status: emitted.append(status),
    )

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "cancelled"
    assert emitted == ["cancelled"]
