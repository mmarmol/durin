"""The OCR worker: transcription, resumption, failure handling."""

import os
import subprocess
import sys

import pytest

from durin.jobs.ocr_worker import run_job
from durin.jobs.registry import JobRegistry
from durin.jobs.spawn import MAX_CONCURRENT_OCR_JOBS
from durin.memory.pdf_coverage import page_texts


@pytest.fixture()
def registry(tmp_path):
    return JobRegistry(tmp_path / "jobs.db")


@pytest.fixture()
def scanned_pdf(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "book.pdf"
    _write_text_pdf(pdf, ["", "", ""])
    return pdf


def _enqueue(registry, pdf, pages, *, sidecar_dir=None, units_total=None):
    payload = {"path": str(pdf), "pages": pages}
    if sidecar_dir is not None:
        payload["sidecar_dir"] = str(sidecar_dir)
    return registry.enqueue(
        kind="ocr",
        label=pdf.name,
        payload=payload,
        session_key="chat:1",
        units_total=len(pages) if units_total is None else units_total,
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


def test_worker_transcribes_the_empty_pages_its_payload_left_out(
    registry, tmp_path, monkeypatch,
):
    """The payload is a floor, not the whole set: the conversion path stops
    confirming empty pages the moment a document is plainly over the inline
    budget, and a probe that reads a font pdfplumber cannot can hide an empty
    page from it entirely. Background time is free next to OCR, so the worker
    settles the exhaustive list itself before transcribing — otherwise the
    pages nobody confirmed stay missing from the document forever."""
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "gapped.pdf"
    _write_text_pdf(
        pdf,
        ["Real body text on page one, plenty of it", "", "",
         "Real body text on page four, plenty of it"],
    )
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"text of page {page}",
    )
    job = _enqueue(registry, pdf, [2])  # page 3 is just as empty and left out

    run_job(job.id, registry=registry)

    assert registry.done_units(job.id) == {2, 3}
    assert registry.get(job.id).units_total == 2


def test_worker_resizes_a_job_that_was_enqueued_expecting_more_pages(
    registry, tmp_path, monkeypatch,
):
    """The enqueued count is the probe's estimate, and the probe over-flags:
    it can name more pages than really have no text layer. A job that
    transcribes everything there was and still reads "1 of 5" looks like one
    that gave up partway."""
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "one_gap.pdf"
    _write_text_pdf(pdf, ["", *["Real body text on this page, plenty of it"] * 4])
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"text of page {page}",
    )
    job = _enqueue(registry, pdf, [1], units_total=5)

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.units_total == 1
    assert reread.units_done == 1


def test_worker_refuses_to_publish_a_book_it_could_not_check(
    registry, tmp_path, monkeypatch,
):
    """The payload is a floor. If the pass that turns it into the whole set
    cannot run, transcribing the floor and reporting "done" publishes a
    forty-page book with six pages in it and no way for anyone to tell — the
    remaining pages would have been decided by omission, from the probe's
    unsafe direction, with nothing confirming them. A recorded failure is
    visible and resumable; a silent partial success is neither.

    A transient failure is the case that matters: a permanently unreadable PDF
    already fails at the sidecar step, which reads the same way."""
    entry_dir, pdf = _ingested_entry(tmp_path / "ws", pages=("",) * 40)
    real_page_texts = page_texts
    calls = []

    def _fails_the_first_time(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("transient read failure")
        return real_page_texts(path)

    monkeypatch.setattr("durin.jobs.ocr_worker.page_texts", _fails_the_first_time)
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"text of page {page}",
    )
    job = _enqueue(
        registry, pdf, [1, 2, 3, 4, 5, 6], sidecar_dir=entry_dir, units_total=40,
    )

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "failed"
    assert "confirm" in reread.error
    assert registry.done_units(job.id) == set()
    assert not (entry_dir / "source.md").exists()
    # The count nobody could verify is left alone rather than rewritten down to
    # the floor -- that rewrite is what would have made "6 of 6, done" look
    # like a finished book.
    assert reread.units_total == 40


def test_worker_never_reports_more_pages_done_than_it_has_to_do(
    registry, tmp_path, monkeypatch,
):
    """A resumed run re-derives the page set, and it can come back smaller than
    what an earlier run already recorded. Sizing the job from that alone puts
    the tray at "3 of 1"; pages already transcribed are this job's work
    whatever the recheck says about them now."""
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "one_gap.pdf"
    _write_text_pdf(pdf, ["", *["Real body text on this page, plenty of it"] * 2])
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"text of page {page}",
    )
    job = _enqueue(registry, pdf, [1], units_total=1)
    for page in (1, 2, 3):  # an earlier run found more than this one will
        registry.record_unit(job.id, page, f"already transcribed {page}")

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.units_done == 3
    assert reread.units_total == 3


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
    # All three pages of this entry are empty, and the worker widens a payload
    # that named only one of them before it starts.
    assert registry.done_units(job.id) == {1, 2, 3}
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

    def _boom(path, text):
        raise OSError("disk gone")

    # The write itself, not the extraction that feeds it: an extraction that
    # fails is caught earlier now, by the completeness check, and would never
    # reach this step.
    monkeypatch.setattr("durin.jobs.ocr_worker.atomic_write_text", _boom)
    job = _enqueue(registry, scanned_pdf, [1], sidecar_dir=sidecar_dir)

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "failed"
    assert "disk gone" in reread.error
    # The pages were safely transcribed and recorded before the sidecar step
    # broke — a retry would not need to redo them. All three of this fixture's
    # pages are empty, and the worker widens a payload that named one.
    assert registry.done_units(job.id) == {1, 2, 3}
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

    def _claim_after_a_race_lost_cancel(job_id, *, pid, **kwargs):
        registry.cancel(job_id)
        return real_claim(job_id, pid=pid, **kwargs)

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


def test_worker_chains_even_when_finish_loses_to_something_else(
    registry, tmp_path, scanned_pdf, monkeypatch,
):
    """Important: finish() returning False means something else already
    decided this job's outcome (the late cancel above, or a second worker
    via reconcile's age fallback) -- the row is terminal either way, so the
    cap slot it held is free either way too. This worker is the one about to
    exit; if it does not chain, nothing else here is going to notice a
    queued sibling is waiting."""
    ws = tmp_path / "ws"
    entry_dir, pdf = _ingested_entry(ws)
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )

    def _cancel_midway(entry_dir):
        registry.cancel(job.id)

    monkeypatch.setattr("durin.jobs.ocr_worker.index_ingested_entry", _cancel_midway)
    launched = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker._launch_worker",
        lambda job_id: launched.append(job_id),
    )
    job = _enqueue(registry, pdf, [1], sidecar_dir=entry_dir)
    next_job = _enqueue(registry, scanned_pdf, [1])  # queued sibling behind it

    run_job(job.id, registry=registry)

    reread = registry.get(job.id)
    assert reread.status == "cancelled"
    assert launched == [next_job.id]


# ---------------------------------------------------------------------------
# The concurrency cap (MAX_CONCURRENT_OCR_JOBS) and the chain that drains a
# queue behind it one fresh process at a time.
# ---------------------------------------------------------------------------


def _dead_pid() -> int:
    """A pid that definitely does not belong to a live process: spawn a
    child and wait on it, rather than guessing an arbitrary "probably free"
    integer (mirrors tests/jobs/test_spawn.py's own ``_pid_alive`` fixture).
    Load-bearing here specifically: the self-healing cap-refusal path below
    calls the real ``_pid_alive`` probe, so a pid that merely *looks* free by
    convention is not good enough -- it has to actually be dead."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    return proc.pid


def test_worker_exits_quietly_when_the_cap_refuses_its_claim(registry, scanned_pdf, monkeypatch):
    """MAX_CONCURRENT_OCR_JOBS=1: a worker that finds another ocr job already
    running -- genuinely alive, unlike the dead-holder case below -- must not
    claim its own. It leaves the row exactly as enqueue left it, for the
    chain (or a later startup pickup) to claim later."""
    assert MAX_CONCURRENT_OCR_JOBS == 1  # the scenario this test builds
    called = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: called.append(page) or f"page {page}",
    )
    running = _enqueue(registry, scanned_pdf, [1])
    # This test's own process: guaranteed alive for its whole duration, so
    # the cap-refused claim's inline self-healing reconcile (see the test
    # below) correctly finds nothing to clean up and refuses again.
    registry.claim(running.id, pid=os.getpid())

    job = _enqueue(registry, scanned_pdf, [1, 2, 3])
    run_job(job.id, registry=registry)

    assert called == []
    reread = registry.get(job.id)
    assert reread.status == "queued"
    assert reread.pid is None
    assert reread.started_at is None
    # The job holding the cap slot is untouched by the refusal.
    running_reread = registry.get(running.id)
    assert running_reread.status == "running"
    assert running_reread.pid == os.getpid()


def test_worker_reclaims_the_cap_slot_from_a_dead_holder(registry, scanned_pdf, monkeypatch):
    """Critical: a worker that dies without finish() -- OOM-killed, kill -9,
    power loss, or an escaping DB error -- the very failure the cap makes
    MORE likely, since it is now the one thing standing between "one worker"
    and the resource pressure that kills workers -- leaves its row "running"
    forever. reconcile has exactly one other call site, gateway startup,
    which could be hours away. Before this fix a cap refusal just walked
    away, and every later job of this kind stayed queued forever behind a
    dead row with no symptom but an info log. A refused claim must instead
    self-heal: run the same probe inline and retry once."""
    called = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: called.append(page) or f"page {page}",
    )
    stale = _enqueue(registry, scanned_pdf, [1])
    registry.claim(stale.id, pid=_dead_pid())  # holds the one cap slot, but is dead

    job = _enqueue(registry, scanned_pdf, [1, 2, 3])
    run_job(job.id, registry=registry)

    assert called == [1, 2, 3]
    reread = registry.get(job.id)
    assert reread.status == "done"
    # The stale holder was requeued by the inline reconcile -- not silently
    # left "running" forever, wedging every job behind it.
    stale_reread = registry.get(stale.id)
    assert stale_reread.status == "queued"
    assert stale_reread.pid is None


def test_worker_chains_to_the_next_queued_job_after_finishing(registry, scanned_pdf, monkeypatch):
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )
    launched = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker._launch_worker",
        lambda job_id: launched.append(job_id),
    )
    job = _enqueue(registry, scanned_pdf, [1])
    next_job = _enqueue(registry, scanned_pdf, [1])  # left queued behind it

    run_job(job.id, registry=registry)

    assert launched == [next_job.id]


def test_worker_does_not_chain_when_nothing_is_queued(registry, scanned_pdf, monkeypatch):
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )
    launched = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker._launch_worker",
        lambda job_id: launched.append(job_id),
    )
    job = _enqueue(registry, scanned_pdf, [1])

    run_job(job.id, registry=registry)

    assert launched == []


def test_worker_chains_only_after_its_own_finish_write(registry, scanned_pdf, monkeypatch):
    """The chain launches a fresh process while cap=1 has exactly one slot --
    if the launch happened before finish()'s write, that slot would briefly
    look held by two jobs at once. Proving the order rather than just "both
    happened eventually" is what rules that out."""
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )
    order = []
    real_finish = registry.finish

    def _tracked_finish(*args, **kwargs):
        result = real_finish(*args, **kwargs)
        order.append("finish")
        return result

    monkeypatch.setattr(registry, "finish", _tracked_finish)
    monkeypatch.setattr(
        "durin.jobs.ocr_worker._launch_worker",
        lambda job_id: order.append("launch"),
    )
    job = _enqueue(registry, scanned_pdf, [1])
    _enqueue(registry, scanned_pdf, [1])  # next queued job to chain to

    run_job(job.id, registry=registry)

    assert order == ["finish", "launch"]


def test_worker_chains_after_a_cancel_is_noticed_mid_loop(registry, scanned_pdf, monkeypatch):
    """Important: the per-page loop's own cancellation exit is a terminal
    write too (registry.cancel already ran), and the cap slot the cancelled
    job held is free from that instant -- exactly like a normal finish. Under
    cap=1, "one job running, one queued behind it" is the NORMAL state for a
    multi-book ingest, and stop is a user-facing button: a queued sibling
    must not sit there forever just because the job ahead of it was
    cancelled instead of finishing."""
    def _cancel_after_first(path, page, **kw):
        if page == 1:
            registry.cancel(job.id)
        return f"page {page}"

    monkeypatch.setattr("durin.jobs.ocr_worker.transcribe_page", _cancel_after_first)
    launched = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker._launch_worker",
        lambda job_id: launched.append(job_id),
    )
    job = _enqueue(registry, scanned_pdf, [1, 2, 3])
    next_job = _enqueue(registry, scanned_pdf, [1])  # queued sibling behind it

    run_job(job.id, registry=registry)

    assert registry.get(job.id).status == "cancelled"
    assert launched == [next_job.id]


def test_worker_does_not_chain_when_its_own_claim_was_refused(registry, scanned_pdf, monkeypatch):
    """The refused-claim exit must NOT chain: this worker never held a cap
    slot to free, so launching from here would be an extra process on top of
    whatever the live holder is already going to chain to when it finishes --
    the live holder owns the chain, not a worker that never claimed
    anything."""
    called = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: called.append(page) or f"page {page}",
    )
    running = _enqueue(registry, scanned_pdf, [1])
    registry.claim(running.id, pid=os.getpid())  # genuinely alive: refused, not self-healed
    launched = []
    monkeypatch.setattr(
        "durin.jobs.ocr_worker._launch_worker",
        lambda job_id: launched.append(job_id),
    )
    job = _enqueue(registry, scanned_pdf, [1])

    run_job(job.id, registry=registry)

    assert called == []
    assert launched == []


def test_worker_chain_failure_is_logged_and_swallowed(registry, scanned_pdf, monkeypatch):
    """Same contract spawn_ocr_job already documents for its own Popen call:
    a launch failure must not raise out of run_job -- the row it would have
    started stays queued, and the next reconcile/startup sweep retries it."""
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )

    def _boom(job_id):
        raise OSError("no more processes")

    monkeypatch.setattr("durin.jobs.ocr_worker._launch_worker", _boom)
    job = _enqueue(registry, scanned_pdf, [1])
    next_job = _enqueue(registry, scanned_pdf, [1])

    run_job(job.id, registry=registry)  # must not raise

    assert registry.get(job.id).status == "done"
    reread_next = registry.get(next_job.id)
    assert reread_next.status == "queued"
    assert reread_next.pid is None


def test_worker_chain_drains_a_queue_of_several_jobs(registry, scanned_pdf, monkeypatch):
    """Startup under cap=1 with several queued jobs only ever needs to launch
    one worker directly -- the chain drains the rest, one fresh process at a
    time. Simulated here by making _launch_worker re-invoke run_job
    synchronously instead of spawning a real subprocess, which proves the
    drain property without needing real processes: dropping the chain call
    would leave the second and third job queued forever."""
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"page {page}",
    )
    monkeypatch.setattr(
        "durin.jobs.ocr_worker._launch_worker",
        lambda job_id: run_job(job_id, registry=registry),
    )
    jobs = [_enqueue(registry, scanned_pdf, [1]) for _ in range(3)]

    run_job(jobs[0].id, registry=registry)  # only this one is "launched" directly

    assert [registry.get(j.id).status for j in jobs] == ["done", "done", "done"]
