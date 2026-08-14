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

from durin.jobs.registry import JobRegistry
from durin.memory.ingestion import index_ingested_entry
from durin.memory.ocr import transcribe_page
from durin.memory.pdf_coverage import classify_coverage, page_texts
from durin.utils.atomic_write import atomic_write_text

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

    if not registry.claim(job_id, pid=os.getpid()):
        # Lost the race this pre-check cannot close: something else (a
        # cancel, or another worker instance) changed the status away from
        # "queued" between the read above and this claim's own conditional
        # UPDATE. Bailing here — rather than trusting the claim call
        # unconditionally — matters most when there is nothing left to do:
        # with every page already transcribed, the per-page loop below never
        # runs at all, so this is the only check that stops a job that just
        # lost a race from being marked "done" anyway.
        logger.warning(
            "ocr worker: job {} was claimed or changed status between the "
            "status check and the claim; skipping", job_id,
        )
        return
    started = time.monotonic()

    # The payload is a floor, not the whole set. The conversion path stops
    # confirming empty pages the moment a document is plainly over the inline
    # budget — that is what makes the enqueue decision cheap — and its cheap
    # probe can miss an empty page outright on a font it decodes and
    # pdfplumber does not. Neither gap may reach the finished document, and
    # here the exhaustive pass is affordable: seconds of extraction against
    # minutes of OCR. Union rather than replace, so a page already promised
    # stays promised.
    #
    # What comes out is exactly what this run will transcribe, so it is also
    # the honest denominator for progress — in both directions. The enqueued
    # count can overshoot as easily as undershoot (it is the probe's estimate,
    # and the probe over-flags), and a job that stops at "38 of 40" having done
    # all its work reads as one that gave up.
    pages = sorted(set(pages) | set(_empty_pages(pdf_path)))
    if len(pages) != job.units_total:
        registry.set_units_total(job_id, len(pages), pid=os.getpid())
    todo = [p for p in pages if p not in already]

    error: str | None = None
    for page in todo:
        # Re-read rather than trusting the local copy: cancellation arrives
        # from the gateway, in another process.
        current = registry.get(job_id)
        if current is None or current.status == "cancelled":
            _emit(job_id, len(pages), len(already), started, "cancelled")
            return
        try:
            registry.record_unit(job_id, page, transcribe_page(pdf_path, page))
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
        _emit(job_id, len(pages), len(already), started, "failed" if error else "done")
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


def _empty_pages(pdf_path: Path) -> list[int]:
    """Every 1-based page of *pdf_path* with no text layer, measured accurately.

    Best effort by design: a PDF the accurate extractor chokes on is exactly
    the kind of document that was sent for OCR in the first place, so failing
    to widen the page list must not stop the transcription that was already
    asked for.
    """
    try:
        return list(classify_coverage(page_texts(pdf_path)).empty_pages)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ocr worker: could not re-check {} for missed empty pages: {}",
            pdf_path.name, exc,
        )
        return []


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
