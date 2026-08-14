"""Local OCR for PDF pages that carry no text layer.

Rasterise with pypdfium2, transcribe with RapidOCR on CPU. Both stay inside
this module so the rest of the codebase never imports the engine directly and
an install without the [ocr] extra fails in exactly one place.

The engine is loaded lazily and held per process, so which process calls
``transcribe_page`` decides where that memory ends up. The OCR worker
(``durin/jobs/ocr_worker.py``) calls it directly: that worker is itself a
short-lived subprocess, so its engine goes away when it exits. The gateway is
not short-lived, so inline callers never call ``transcribe_page`` in this
process — they call ``transcribe_pages_detached``, which runs the engine in
its own short-lived child (``durin/memory/ocr_subproc.py``) and hands back
only the transcribed text, keeping the engine's memory out of the gateway
either way.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

__all__ = [
    "OcrUnavailable",
    "engine_available",
    "render_page",
    "transcribe_page",
    "transcribe_pages_detached",
]

# 200 dpi is the usual floor for reliable OCR of body text; below it small
# type starts dropping characters, above it the cost grows with no gain.
_DEFAULT_DPI = 200

_engine = None

# Measured (durin v0.6.0 audit): one OCR engine costs ~1.4 GB resident and
# ~4.8 cores for as long as it runs, wherever it runs -- the same budget the
# background lane's cap is built from (~2.1 GB peak over a whole book). Two or
# three of them together saturate a laptop and OOM small servers. That cap is
# enforced in the job registry and never sees this path, so inline
# transcription admits one child at a time here: callers reach this from
# ``asyncio.to_thread``'s executor, whose several worker threads would
# otherwise let N scanned attachments spawn N children at once. A threading
# semaphore, not an asyncio one, because that executor is what contends. No
# config knob, for the same reason the job cap has none: raising it changes
# the resource budget the number is built from, which is a code change, not a
# per-install preference.
_INLINE_OCR_SLOT = threading.Semaphore(1)


class OcrUnavailable(RuntimeError):
    """Raised when OCR is requested but the [ocr] extra is not installed."""


def engine_available() -> bool:
    """Whether the OCR engine can be imported in this interpreter."""
    try:
        import rapidocr  # noqa: F401

        return True
    except ImportError:
        return False


def _get_engine():
    global _engine
    if _engine is None:
        if not engine_available():
            raise OcrUnavailable(
                "local OCR needs the [ocr] extra: install it via the "
                "Settings > Documents toggle, or manually with `pipx inject "
                'durin-agent "durin-agent[ocr]"` / `uv tool install '
                '"durin-agent[ocr]"`'
            )
        from rapidocr import RapidOCR

        _engine = RapidOCR()
    return _engine


def render_page(pdf_path: Path, page: int, *, dpi: int = _DEFAULT_DPI) -> bytes:
    """Render one 1-based PDF page to PNG bytes."""
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        if page < 1 or page > len(doc):
            raise ValueError(
                f"page {page} out of range for {pdf_path.name} ({len(doc)} pages)"
            )
        # pypdfium2 renders at a scale relative to 72 dpi.
        bitmap = doc[page - 1].render(scale=dpi / 72)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


def transcribe_page(pdf_path: Path, page: int, *, dpi: int = _DEFAULT_DPI) -> str:
    """Transcribe one 1-based PDF page, top to bottom.

    Returns an empty string for a page the engine finds no text on — a blank
    scan is a legitimate outcome, not a failure.
    """
    engine = _get_engine()
    result = engine(render_page(pdf_path, page, dpi=dpi))
    lines = getattr(result, "txts", None)
    if not lines:
        return ""
    return "\n".join(str(line) for line in lines).strip()


def transcribe_pages_detached(
    pdf_path: Path,
    pages: Sequence[int],
    *,
    dpi: int = _DEFAULT_DPI,
    timeout_s: float | None = None,
) -> dict[int, str]:
    """Transcribe *pages* of *pdf_path* in a short-lived child process.

    Spawns ``python -m durin.memory.ocr_subproc`` and waits for it
    synchronously — one process per call, never pooled or cached, so the
    engine's memory is released the moment the child exits rather than
    living in the caller. The child renders its own pages from *pdf_path*;
    only the page numbers cross the process boundary, not image data.

    Raises :class:`OcrUnavailable` — the same exception a broken in-process
    engine raises — on a non-zero exit, a timeout, an unparseable result, or
    a result missing one of the requested pages. Every one of those is a
    failure of the whole call: there is no partial result to salvage, so
    existing callers degrade exactly as they already do for a missing
    engine, via the ``except (OcrUnavailable, ImportError)`` they already
    have. That reuse is deliberate — this adds no new exception type.

    Holds ``_INLINE_OCR_SLOT`` for the whole call, so concurrent callers
    queue instead of putting several engines on the machine at once.
    """
    with _INLINE_OCR_SLOT:
        if timeout_s is None:
            # 60s covers interpreter startup plus RapidOCR's model load; 10s
            # per page covers rendering and inference on CPU with slack for a
            # slow host. Measured from the child's own start, not from this
            # call's: waiting for the slot above spends none of it. Not
            # configurable: a hung child should fail loudly into the
            # coverage-note path, not wait on a knob nobody will tune
            # correctly.
            timeout_s = 60 + 10 * len(pages)

        cmd = [
            sys.executable, "-m", "durin.memory.ocr_subproc",
            str(pdf_path), str(dpi), *(str(page) for page in pages),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            tail = (exc.stderr or "")[-500:]
            raise OcrUnavailable(
                f"OCR subprocess timed out after {timeout_s:.0f}s: {tail}"
            ) from exc

        if proc.returncode != 0:
            tail = (proc.stderr or "")[-500:]
            raise OcrUnavailable(f"OCR subprocess exited {proc.returncode}: {tail}")

        try:
            payload = json.loads(proc.stdout)
            result = {int(page): text for page, text in payload["pages"].items()}
        except Exception as exc:  # noqa: BLE001 — any parse-shape surprise is OcrUnavailable too
            tail = (proc.stderr or "")[-500:]
            raise OcrUnavailable(
                f"OCR subprocess produced no parseable result: {tail}"
            ) from exc

        missing = [page for page in pages if page not in result]
        if missing:
            raise OcrUnavailable(
                f"OCR subprocess did not return page(s) {missing} of the "
                f"{len(pages)} requested"
            )
        return result
