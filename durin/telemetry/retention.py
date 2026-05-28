"""Telemetry log retention + rotation (P7.2 / doc 07 §10).

Two age horizons:

- ``COMPRESSION_AGE_DAYS`` (30) — ``.jsonl`` files older than this
  are gzip-compressed in place to ``.jsonl.gz``.
- ``DELETION_AGE_DAYS`` (90) — ``.jsonl.gz`` files older than this
  are removed. This caps disk usage at ~2 months of compressed
  telemetry per workspace.

The rotation runs as part of the health-check tick (P2.4) so there's
no separate scheduler thread. Failures inside the rotation are
counted (``errors``) but never propagate — a broken rotation must
not break the agent.
"""

from __future__ import annotations

import gzip
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "COMPRESSION_AGE_DAYS",
    "DELETION_AGE_DAYS",
    "run_retention",
]


COMPRESSION_AGE_DAYS: int = 30
DELETION_AGE_DAYS: int = 90


def run_retention(telemetry_dir: Path) -> dict[str, int]:
    """Apply the retention policy to *telemetry_dir*.

    Returns a summary dict with ``compressed``, ``deleted``, ``errors``
    counters. Missing dir returns zeros (no-op).
    """
    summary = {"compressed": 0, "deleted": 0, "errors": 0}
    telemetry_dir = Path(telemetry_dir)
    if not telemetry_dir.is_dir():
        return summary

    now = time.time()
    compression_cutoff = now - COMPRESSION_AGE_DAYS * 86400
    deletion_cutoff = now - DELETION_AGE_DAYS * 86400

    for path in sorted(telemetry_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            logger.warning("retention: stat %s failed: %s", path, exc)
            summary["errors"] += 1
            continue

        if name.endswith(".jsonl.gz"):
            if mtime < deletion_cutoff:
                try:
                    path.unlink()
                    summary["deleted"] += 1
                except OSError as exc:
                    logger.warning(
                        "retention: delete %s failed: %s", path, exc,
                    )
                    summary["errors"] += 1
            continue

        if name.endswith(".jsonl"):
            if mtime < compression_cutoff:
                try:
                    raw = path.read_bytes()
                    compressed = gzip.compress(raw)
                    gz_path = path.with_suffix(".jsonl.gz")
                    gz_path.write_bytes(compressed)
                    os.utime(gz_path, (mtime, mtime))
                    path.unlink()
                    summary["compressed"] += 1
                except OSError as exc:
                    logger.warning(
                        "retention: compress %s failed: %s", path, exc,
                    )
                    summary["errors"] += 1
            continue
        # Anything else (e.g. plain .txt) — leave alone.

    return summary
