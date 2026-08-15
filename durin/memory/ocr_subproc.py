"""Subprocess entry point for one-shot OCR transcription.

``durin.memory.ocr.transcribe_pages_detached`` spawns this as ``python -m
durin.memory.ocr_subproc`` and waits for it to exit. Doing the transcription
here rather than in the caller means the OCR engine's memory is released the
moment this process ends, instead of staying resident in whatever process
called it.

argv contract: ``<pdf_path> <dpi> <page> [<page>...]``. On success, prints one
JSON object to stdout and exits 0::

    {"pages": {"<page>": {"text": ..., "mean_score": ...,
                          "min_score": ..., "det_boxes": ...}, ...}}

— each inner object mirroring one ``TranscribedPage`` field for field (the
parent rebuilds them from exactly these keys; both sides ship in the same
install, so the shape changes in lockstep). On failure, prints ``{"error":
"<message>"}`` to stdout and exits non-zero. stdout carries nothing else —
the parent parses it as a single JSON document, so any engine logging
(RapidOCR logs INFO per model load) must land on stderr, never here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from durin.memory.ocr import transcribe_page

__all__ = ["main"]


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(json.dumps({"error": "usage: <pdf_path> <dpi> <page> [<page>...]"}))
        return 2

    pdf_path, dpi_arg, *page_args = argv
    try:
        dpi = int(dpi_arg)
        pages = [int(p) for p in page_args]
    except ValueError as exc:
        print(json.dumps({"error": f"invalid dpi or page argument: {exc}"}))
        return 2

    try:
        results = {page: transcribe_page(Path(pdf_path), page, dpi=dpi) for page in pages}
    except Exception as exc:  # noqa: BLE001 — every failure here is the parent's OcrUnavailable
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1

    print(json.dumps({
        "pages": {
            str(page): {
                "text": result.text,
                "mean_score": result.mean_score,
                "min_score": result.min_score,
                "det_boxes": result.det_boxes,
            }
            for page, result in results.items()
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
