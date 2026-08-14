"""Drain one OCR job, one page at a time.

Runs as its own process (``python -m durin.jobs.ocr_worker <job_id>``) for
three reasons: the ONNX model's memory is released when the process exits
instead of living in the gateway, a CPU-bound loop never touches the gateway's
event loop, and progress needs no invented channel — the worker writes the job
database the gateway is already reading.

Every page is committed as it finishes, so an interrupted run resumes instead
of restarting.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from loguru import logger

from durin.jobs.registry import JobRegistry
from durin.memory.ocr import transcribe_page
from durin.memory.pdf_coverage import page_texts
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
    todo = [p for p in pages if p not in already]

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

    registry.finish(job_id, error=error)
    _emit(job_id, len(pages), len(already), started, "failed" if error else "done")


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
