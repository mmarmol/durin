"""Local OCR for PDF pages that carry no text layer.

Rasterise with pypdfium2, transcribe with RapidOCR on CPU. Both stay inside
this module so the rest of the codebase never imports the engine directly and
an install without the [ocr] extra fails in exactly one place.

The engine is loaded lazily and held per process. A worker process is
short-lived by design, so its memory goes away with it rather than sitting in
the gateway for the whole uptime.
"""

from __future__ import annotations

import io
from pathlib import Path

__all__ = [
    "OcrUnavailable",
    "engine_available",
    "render_page",
    "transcribe_page",
]

# 200 dpi is the usual floor for reliable OCR of body text; below it small
# type starts dropping characters, above it the cost grows with no gain.
_DEFAULT_DPI = 200

_engine = None


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
                "local OCR needs the [ocr] extra: pip install durin-agent[ocr]"
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
