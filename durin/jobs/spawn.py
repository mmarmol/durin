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

__all__ = ["spawn_ocr_job", "respawn"]


def spawn_ocr_job(
    *, registry: JobRegistry, pdf_path: Path, pages: list[int],
    session_key: str | None, sidecar_dir: Path | None = None,
) -> Job:
    """Enqueue an OCR job and start its worker.

    ``sidecar_dir`` is the ingested entry directory. When set, the worker
    writes the assembled ``source.md`` there on success — the same sidecar the
    ordinary conversion path writes, produced later instead of inline.
    """
    job = registry.enqueue(
        kind="ocr",
        label=pdf_path.name,
        payload={
            "path": str(pdf_path),
            "pages": pages,
            "sidecar_dir": str(sidecar_dir) if sidecar_dir else None,
        },
        session_key=session_key,
        units_total=len(pages),
    )
    try:
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "durin.jobs.ocr_worker", job.id],
            start_new_session=True,
        )
    except OSError:
        # The row stays queued; reconcile picks it up on the next gateway start.
        logger.exception("could not start the OCR worker for job {}", job.id)
    return job


def respawn(job: Job) -> None:
    """Restart the worker for a job ``reconcile`` has already returned to ``queued``.

    Never claims the job itself — claiming is exclusively the worker's own
    job (see ``ocr_worker.run_job``), and a ``respawn`` that claimed first
    would lock the very worker it starts out of the job.
    """
    if job.kind == "ocr":
        try:
            subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "durin.jobs.ocr_worker", job.id],
                start_new_session=True,
            )
        except OSError:
            # Same contract as spawn_ocr_job: stays queued, next reconcile retries.
            logger.exception("could not restart the OCR worker for job {}", job.id)
        return
    raise ValueError(f"no worker for job kind {job.kind!r}")


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe: signal 0 sends nothing, it only checks
    whether *pid* exists and is reachable from this process."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
