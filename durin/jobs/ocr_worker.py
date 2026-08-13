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

__all__ = ["run_job"]


def run_job(job_id: str, *, registry: JobRegistry | None = None) -> None:
    """Transcribe every page named in the job's payload that is not done yet."""
    registry = registry or JobRegistry()
    job = registry.get(job_id)
    if job is None:
        logger.warning("ocr worker: unknown job {}", job_id)
        return

    pdf_path = Path(job.payload["path"])
    pages: list[int] = list(job.payload["pages"])
    already = registry.done_units(job_id)
    todo = [p for p in pages if p not in already]

    registry.claim(job_id, pid=os.getpid())
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
