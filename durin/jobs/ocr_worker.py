"""Drain one OCR job, one page at a time.

Runs as its own process (``python -m durin.jobs.ocr_worker <job_id>``) for
three reasons: the ONNX model's memory is released when the process exits
instead of living in the gateway, a CPU-bound loop never touches the gateway's
event loop, and progress needs no invented channel — the worker writes the job
database the gateway is already reading.

Every page is committed as it finishes, so an interrupted run resumes instead
of restarting.

The last step of a successful run hands the assembled text back to the memory
layer, which stores and indexes it: transcribing a scanned book is only worth
anything once the book is searchable, and this process is where its text first
exists.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from loguru import logger

from durin.config.loader import load_config
from durin.jobs.registry import JobRegistry
from durin.jobs.spawn import MAX_CONCURRENT_OCR_JOBS, _launch_worker
from durin.memory.ingestion import index_ingested_entry
from durin.memory.ocr import score_str, transcribe_page
from durin.memory.pdf_coverage import classify_coverage, page_texts
from durin.utils.atomic_write import atomic_write_text
from durin.utils.process import pid_alive

__all__ = ["run_job"]


def run_job(job_id: str, *, registry: JobRegistry | None = None) -> None:
    """Transcribe every page named in the job's payload that is not done yet."""
    if registry is None:
        registry = JobRegistry()
    job = registry.get(job_id)
    if job is None:
        logger.warning("ocr worker: unknown job {}", job_id)
        return

    pdf_path = Path(job.payload["path"])
    pages: list[int] = list(job.payload["pages"])
    already = registry.done_units(job_id)

    # This worker is its own process, so the recognition language comes from
    # its own config load — once per run, before the claim, so a config that
    # cannot be read leaves the job queued (retryable) rather than stuck
    # running. Resolved here and not in the payload: the job records which
    # pages to transcribe, and a language changed mid-queue should apply.
    language = load_config().documents.ocr.language

    # Re-fetch immediately before claiming rather than trusting `job` above:
    # interpreter boot, imports and the done_units() round trip all take real
    # wall-clock time. `queued` is the only status a fresh invocation may
    # claim — `running` means another process already owns this job,
    # `done`/`failed` mean it already has an outcome, and `cancelled` means
    # nobody should touch it further. This is a fast, informative pre-check,
    # not the actual guarantee: registry.claim() below is what makes the
    # read-then-write atomic against a cancel (or a second worker) landing
    # in the gap between this read and that call.
    current = registry.get(job_id)
    if current is None or current.status != "queued":
        logger.warning(
            "ocr worker: job {} is {} at claim time, not queued; skipping",
            job_id, current.status if current is not None else "gone",
        )
        return

    won = registry.claim(
        job_id, pid=os.getpid(), kind_cap=("ocr", MAX_CONCURRENT_OCR_JOBS),
    )
    if not won:
        # False here means one of two things, and claim()'s own atomic
        # UPDATE is what makes them indistinguishable from the return value
        # alone: something else changed the row's status away from "queued"
        # (a cancel, or another worker instance) between the read above and
        # this claim, or the row is still "queued" but MAX_CONCURRENT_OCR_JOBS
        # is already reached. The re-read below is only to tell those two
        # apart for logging (nothing below decides what to do based on it
        # beyond the self-heal case) — a row still "queued" can only mean
        # the cap refused it, since a status race would have left some other
        # status behind.
        after = registry.get(job_id)
        if after is not None and after.status == "queued":
            # A cap refusal specifically can mean the "running" job holding
            # the slot is not really running any more: its worker was
            # OOM-killed, kill -9'd, or lost power without ever reaching
            # finish() — the very failure this cap makes MORE likely, being
            # now the one thing standing between "one worker" and the
            # resource pressure that kills workers. Left alone, that row
            # stays "running" forever and the cap's own COUNT keeps counting
            # it, wedging every later job of this kind — reconcile has
            # exactly one other call site, gateway startup, which could be
            # hours away. So a refused claim runs the same probe right here,
            # at the moment someone is actually being blocked by a stale
            # holder, and retries once if it requeued anything: a dead
            # holder is cleaned at exactly the moment its absence matters. A
            # live holder means reconcile requeues nothing and the retry
            # refuses again for the ordinary reason — walk away exactly as
            # before.
            #
            # Accepted edge, same one gateway startup's own reconcile call
            # already accepts: the 6h age fallback can requeue a holder that
            # is genuinely alive but slow, briefly letting two workers hold
            # one job. finish()'s pid guard and record_unit's idempotence
            # are what keep that window safe, not this retry.
            if registry.reconcile(alive=pid_alive):
                won = registry.claim(
                    job_id, pid=os.getpid(), kind_cap=("ocr", MAX_CONCURRENT_OCR_JOBS),
                )
            if not won:
                logger.info(
                    "ocr worker: another ocr worker is running; leaving {} queued", job_id,
                )
        else:
            logger.warning(
                "ocr worker: job {} was claimed or changed status between the "
                "status check and the claim; skipping", job_id,
            )
        if not won:
            return
    started = time.monotonic()

    # The payload is a floor, not the whole set. The conversion path stops
    # confirming empty pages the moment a document is plainly over the inline
    # budget — that is what makes the enqueue decision cheap — and its cheap
    # probe can miss an empty page outright on a font it decodes and
    # pdfplumber does not. Neither gap may reach the finished document, and
    # here the exhaustive pass is affordable: seconds of extraction against
    # minutes of OCR.
    accurate_empty = _empty_pages(pdf_path)

    error: str | None = None
    todo: list[int] = []
    if accurate_empty is None:
        # Without that pass there is no way to tell a floor from a finished
        # list, and transcribing the floor would publish a forty-page book
        # holding six pages, reported "done". The pages left out would have
        # been decided by omission — from the probe's unsafe direction, with
        # nothing confirming them — and nobody downstream could tell. So this
        # is a job failure: visible, resumable, and honest about what it is.
        #
        # It costs little. Every path that enqueues one of these jobs read the
        # document with this same extractor first, so a PDF it cannot read is
        # a change of circumstances rather than the norm; and a permanent
        # failure already fails this job at the sidecar step below, which
        # reads the document the same way. Failing here just says so sooner,
        # before spending an hour of OCR on a document that cannot be
        # published.
        error = (
            f"completeness check: could not read {pdf_path.name} to confirm "
            "which pages need transcribing"
        )
    else:
        # Union rather than replace, so a page already promised stays promised
        # — including pages an earlier run already transcribed, which are this
        # job's work whatever this run's re-check says about them now. That is
        # also what keeps the denominator from falling below the numerator.
        pages = sorted(set(pages) | set(accurate_empty) | already)
        # What comes out is exactly what this run will transcribe, so it is
        # the honest denominator for progress — in both directions. The
        # enqueued count can overshoot as easily as undershoot (it is the
        # probe's estimate, and the probe over-flags), and a job that stops at
        # "38 of 40" having done all its work reads as one that gave up.
        if len(pages) != job.units_total and not registry.set_units_total(
            job_id, len(pages), pid=os.getpid()
        ):
            # Same contract as claim() and finish(): a write that did not
            # happen is not a write to assume. The row stopped being this
            # worker's between the claim and here — the per-page loop's own
            # check below is what acts on that.
            logger.warning(
                "ocr worker: job {} was not this worker's to resize; "
                "its progress count is left as it was", job_id,
            )
        todo = [p for p in pages if p not in already]

    # Detector box counts for the pages THIS run transcribed empty. Pages an
    # earlier run recorded kept only their text, so if the whole book comes
    # out blank below, this is all the detector evidence that exists.
    det_boxes_seen: dict[int, int] = {}
    for page in todo:
        # Re-read rather than trusting the local copy: cancellation arrives
        # from the gateway, in another process.
        current = registry.get(job_id)
        if current is None or current.status == "cancelled":
            _emit(job_id, len(pages), len(already), started, "cancelled")
            # registry.cancel() already wrote the terminal status, so the cap
            # slot this job held is free from this instant -- chain exactly
            # as a normal finish would, or a queued sibling waits for a
            # trigger that may never come (stop is user-facing, and under
            # cap=1 "one running, one queued behind it" is the ordinary
            # state for a multi-book ingest).
            _chain_to_next_queued(registry)
            return
        try:
            result = transcribe_page(pdf_path, page, language=language)
            registry.record_unit(job_id, page, result.text)
            if result.det_boxes is None:
                # score_str, not a float format: a text page can carry None
                # scores, and this line runs after record_unit committed —
                # a TypeError here would fail a page that transcribed fine.
                logger.info(
                    "ocr worker: page {} of {}: mean score {}, min {}",
                    page, pdf_path.name,
                    score_str(result.mean_score), score_str(result.min_score),
                )
            else:
                det_boxes_seen[page] = result.det_boxes
                logger.info(
                    "ocr worker: page {} of {}: no text recognized, "
                    "{} text region(s) detected",
                    page, pdf_path.name, result.det_boxes,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("ocr worker: page {} of {} failed", page, pdf_path.name)
            error = f"page {page}: {type(exc).__name__}: {exc}"
            break

    sidecar_dir = job.payload.get("sidecar_dir")
    if error is None and sidecar_dir:
        # The same sidecar the ordinary conversion path writes at ingest time
        # (durin.memory.ingestion), produced later: the document's full
        # per-page text with the transcribed pages filled in, joined the same
        # way convert_file_to_markdown joins them for the inline case.
        #
        # Guarded like the per-page loop above: an unguarded failure here
        # would escape run_job entirely, skip registry.finish(), and leave
        # the job stuck at "running" with no error recorded. Worse, a
        # resumed run finds `todo` already empty and hits this same step
        # again immediately — a persistent cause (permissions, a removed
        # entry directory) would then loop forever with nothing to
        # diagnose from. Recording it as a normal job failure instead means
        # a retry is a deliberate requeue, not an infinite silent retry.
        try:
            texts = page_texts(pdf_path)
            for unit, text in registry.units(job_id):
                texts[unit - 1] = text
            markdown = "\n\n".join(t for t in texts if t.strip())
            if not markdown:
                # Written empty, this sidecar would fail the job at the
                # indexing step below with nothing but "no text to index" to
                # show for a whole transcription pass. Failing before
                # anything is written keeps the diagnosis the det pass paid
                # for: blank paper, or print the engine could not read.
                error = _no_text_error(det_boxes_seen, language)
            else:
                atomic_write_text(Path(sidecar_dir) / "source.md", markdown)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ocr worker: sidecar write failed for job {}", job_id)
            error = f"sidecar write: {type(exc).__name__}: {exc}"

    if error is None and sidecar_dir:
        # A sidecar nobody indexed is not a document anybody can find, and
        # being findable is the only reason these pages were transcribed. The
        # memory layer owns what a Library entry is and how it is indexed;
        # this hands it the finished entry and lets it decide.
        #
        # Guarded like the sidecar write above, for the same reason, and
        # recorded as a job failure rather than swallowed: a job reported
        # "done" whose document cannot be found is precisely the outcome this
        # step exists to prevent, so it must not be reported that way.
        try:
            index_ingested_entry(Path(sidecar_dir))
        except Exception as exc:  # noqa: BLE001
            logger.exception("ocr worker: library indexing failed for job {}", job_id)
            error = f"library index: {type(exc).__name__}: {exc}"

    if registry.finish(job_id, pid=os.getpid(), error=error):
        if error is None:
            # A done job's units are scratch nothing reads again — the
            # sidecar and the Library index written above are the durable
            # artifacts, and for a book the units are megabytes of page
            # text. A FAILED job must keep its units: they are the resume
            # data a retry's requeued run starts from (deleting them would
            # silently turn "retry resumes from page k" into "retry redoes
            # the whole book") and the diagnosis of what the failed pass
            # produced; a cancelled job keeps its units the same way, since
            # it never reaches this write at all. Both are pruned with the row
            # itself once the retention window passes. The units_done/
            # units_total counters are columns on the job row, so the
            # tray's finished-progress display survives this delete.
            #
            # Guarded because finish() above already committed "done": a
            # failure here (a locked database, say) must not crash the
            # worker before the emit and the chain below run — under cap=1
            # a queued sibling would wait on the periodic sweep for a slot
            # that is already free. The units a failed delete leaves behind
            # cost nothing but space, and the retention prune removes them
            # with the row.
            try:
                registry.delete_units(job_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "ocr worker: unit cleanup failed for job {}", job_id,
                )
        _emit(job_id, len(pages), len(already), started, "failed" if error else "done")
        # Chain: hand the cap slot this job just freed straight to the next
        # queued job, so a backlog drains one fresh process at a time instead
        # of waiting for the next external trigger (another finish, or a
        # gateway restart's queued pickup). Strictly after finish()'s write
        # above, never before — launching earlier could let two workers
        # briefly hold cap=1's one slot at once.
        _chain_to_next_queued(registry)
        return
    # The row is no longer this worker's to finish: a cancel landed while the
    # sidecar and the Library indexing were running (the per-page loop's checks
    # are all behind us by then), or a second worker took the job over. Either
    # way something else already decided the outcome, and that decision stands
    # — reporting this run's own result over it would be a lie.
    final = registry.get(job_id)
    status = final.status if final is not None else "gone"
    logger.warning(
        "ocr worker: job {} was {} by the time this run finished; "
        "not recording its outcome", job_id, status,
    )
    _emit(job_id, len(pages), len(already), started, status)
    # This is usually terminal -- cancelled, or finished by whoever actually
    # won it -- but not always: a takeover via reconcile's age fallback can
    # leave the row `running` under its new owner instead, so the cap slot
    # this job held may not really be free. Chaining anyway is safe: the
    # launched worker still has to win claim()'s own atomic cap check before
    # doing any work, so a chain fired while the slot is not really free
    # just costs that worker a refused claim -- one wasted spawn, not a
    # second worker on the same job. This worker is the one about to exit;
    # if it does not chain here, nothing else is positioned to notice a
    # queued sibling waiting behind a job it never got to finish itself.
    _chain_to_next_queued(registry)


def _chain_to_next_queued(registry: JobRegistry) -> None:
    """Launch a fresh worker for the oldest still-``queued`` OCR job, if any.

    Called from every exit of ``run_job`` that follows a successful claim
    and reaches a terminal state for the row this worker held: its own
    successful ``finish()``, a cancellation noticed mid-loop, and a
    ``finish()`` that lost to something else. The first two genuinely leave
    the row no longer ``running``; the third might not -- a takeover via
    ``reconcile``'s age fallback can leave it ``running`` under its new
    owner instead of freeing the cap slot. Calling this anyway is safe: the
    worker launched below still has to win ``claim()``'s own atomic cap
    check, so a chain fired while the slot is not really free just costs
    that worker a refused claim -- one wasted spawn, not a second worker on
    the same job. Deliberately NOT called from the refused-claim exit above:
    a worker whose own claim was refused never held a slot to free, so
    chaining from there would be an extra launch on top of whatever the
    live holder already does when it finishes — the live holder owns the
    chain, not a worker that never claimed anything.
    """
    next_job = registry.next_queued("ocr")
    if next_job is None:
        return
    try:
        _launch_worker(next_job.id)
    except OSError:
        # Same contract as spawn_ocr_job's own Popen call: the row stays
        # queued. Not reconcile's to retry -- it only ever selects `running`
        # rows -- but the gateway's periodic sweep picks it up within the
        # minute, the same pickup runs again at the next gateway start, and
        # any worker that finishes before either one chains to it.
        logger.exception(
            "ocr worker: could not chain to the next queued job {}", next_job.id,
        )


def _no_text_error(det_boxes_seen: dict[int, int], language: str | None) -> str:
    """The honest account of a transcription pass that produced no text.

    *det_boxes_seen* maps each page this run transcribed empty to the
    detector's box count for it. Pages an earlier run transcribed have no
    entry — only their empty text was recorded — so the unreadable message
    scopes its counts to this run rather than claiming the whole book was
    measured. With no boxes anywhere (or no detector data at all), blank
    paper is what the evidence says, and what the message keeps saying.

    *language* is the recognition language this run transcribed with: when
    one is selected, its model did the reading, so the unreadable message
    names it instead of blaming the built-in pack that was never consulted.
    """
    unreadable = sum(1 for boxes in det_boxes_seen.values() if boxes)
    if unreadable:
        if language:
            cause = (
                f"the selected recognition language ({language!r}) read none "
                "of it — the pages may be in a different script, or genuinely "
                "unreadable"
            )
        else:
            cause = (
                "a script outside its built-in models (they read Chinese, "
                "Japanese and Latin-script languages) is the usual cause"
            )
        return (
            "transcription produced no text: the engine detected printed text "
            f"on {unreadable} of the {len(det_boxes_seen)} page(s) transcribed "
            f"in this run but could not read it; {cause}"
        )
    return "transcription produced no text: every transcribed page came back blank"


def _empty_pages(pdf_path: Path) -> list[int] | None:
    """Every 1-based page of *pdf_path* with no text layer, measured accurately.

    ``None`` when the document could not be read, which is emphatically not
    the same answer as ``[]``. "No page needs transcribing" and "nobody could
    find out which pages need transcribing" lead to opposite decisions, and a
    caller handed the same value for both takes the wrong one silently.
    """
    try:
        return list(classify_coverage(page_texts(pdf_path)).empty_pages)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ocr worker: could not re-check {} for missed empty pages: {}",
            pdf_path.name, exc,
        )
        return None


def _emit(job_id: str, pages: int, resumed: int, started: float, status: str) -> None:
    # Best-effort, matching every other emit site in the codebase: telemetry
    # must never be the reason a finished job reports as failed.
    try:
        from durin.agent.tools._telemetry import emit_tool_event

        emit_tool_event(
            "documents.ocr.job",
            {
                "job_id": job_id,
                "pages": pages,
                "pages_resumed": resumed,
                "duration_s": round(time.monotonic() - started, 3),
                "status": status,
            },
        )
    except Exception:  # pragma: no cover
        pass


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m durin.jobs.ocr_worker <job_id>", file=sys.stderr)
        raise SystemExit(2)
    run_job(sys.argv[1])
