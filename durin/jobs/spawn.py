"""Start a worker process for a queued job.

Kept apart from the registry so the registry stays a pure data surface that a
test can drive without ever spawning anything.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

from durin.jobs.registry import Job, JobRegistry

__all__ = ["MAX_CONCURRENT_OCR_JOBS", "spawn_ocr_job", "respawn"]

# Measured (durin v0.6.0 audit): ~2.1 GB peak RSS and ~4.8 cores per OCR
# worker. Three or four running together saturates a laptop and OOMs small
# servers, so only one may run at a time -- enforced atomically by
# JobRegistry.claim's kind_cap, not by this constant alone. No config knob:
# raising it changes the resource budget this number is built from, which is
# a code change, not a per-install preference.
MAX_CONCURRENT_OCR_JOBS = 1


def _launch_worker(job_id: str) -> subprocess.Popen:
    """Start ``python -m durin.jobs.ocr_worker <job_id>`` as a detached
    subprocess.

    The one Popen call shared by every place that starts (or restarts) an
    OCR worker: ``spawn_ocr_job`` right after enqueueing, ``respawn`` after
    ``reconcile`` requeues an orphan or gateway startup finds a job nobody
    ever claimed, and the worker's own chain right after it finishes. Raises
    on failure rather than swallowing -- each caller has its own log message
    and its own contract for what happens to the row afterward, so recovery
    stays with them.
    """
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "durin.jobs.ocr_worker", job_id],
        start_new_session=True,
    )


def spawn_ocr_job(
    *, registry: JobRegistry, pdf_path: Path, pages: list[int],
    session_key: str | None, label: str, sidecar_dir: Path | None = None,
    units_total: int | None = None,
) -> Job:
    """Enqueue an OCR job and start its worker.

    ``sidecar_dir`` is the ingested entry directory. When set, the worker
    writes the assembled ``source.md`` there on success — the same sidecar the
    ordinary conversion path writes, produced later instead of inline.

    ``label`` is what a human reads in the tasks tray, and it is passed rather
    than derived: ``pdf_path`` points at the entry's normalized copy, which is
    named ``source.<ext>`` for every document there, so two books ingested in
    one session would be indistinguishable. Callers pass the name the user
    handed over.

    ``units_total`` is how much work the tray reports, and defaults to the
    number of pages handed over. A caller that knows ``pages`` is only a floor
    — the conversion path stops confirming pages once a document is plainly
    over the inline budget — passes the size it expects instead, so a scanned
    book does not show up as six pages of work. The worker corrects it against
    its own exhaustive pass before transcribing anything.
    """
    job = registry.enqueue(
        kind="ocr",
        label=label,
        payload={
            "path": str(pdf_path),
            "pages": pages,
            "sidecar_dir": str(sidecar_dir) if sidecar_dir else None,
        },
        session_key=session_key,
        units_total=len(pages) if units_total is None else units_total,
    )
    try:
        _launch_worker(job.id)
    except OSError:
        # The row stays queued; the next gateway start's queued-pickup loop
        # finds it (reconcile only ever selects `running` rows).
        logger.exception("could not start the OCR worker for job {}", job.id)
    return job


def respawn(job: Job) -> None:
    """(Re)start the worker for a job that needs one and is not this
    process's to claim: ``reconcile`` just returned it to ``queued`` from an
    orphaned ``running`` row, or gateway startup found it ``queued`` with
    nothing claiming it (its original spawn's ``Popen`` failed, or the
    worker that would have chained to it crashed first).

    Never claims the job itself — claiming is exclusively the worker's own
    job (see ``ocr_worker.run_job``), and a ``respawn`` that claimed first
    would lock the very worker it starts out of the job.
    """
    if job.kind == "ocr":
        try:
            _launch_worker(job.id)
        except OSError:
            # Same contract as spawn_ocr_job: stays queued, next queued-pickup
            # loop retries (not reconcile -- it only ever selects `running` rows).
            logger.exception("could not restart the OCR worker for job {}", job.id)
        return
    raise ValueError(f"no worker for job kind {job.kind!r}")


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe: signal 0 sends nothing, it only checks
    whether *pid* exists. A PermissionError means the process exists but is
    owned by another uid -- alive, just not ours to signal; only
    ProcessLookupError (and other OSErrors) mean the pid is actually free."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True
