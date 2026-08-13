"""Shared document → markdown conversion (markitdown).

One conversion path in the codebase: the transient ``convert_to_markdown``
read tool and the durable ``memory_ingest`` path both go through
``convert_file_to_markdown`` so the supported-format set and the error
handling never drift apart.

Pure text/markdown formats are NOT handled here — callers read those
verbatim. This module owns the binary/office/PDF formats markitdown parses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from durin.memory.ocr import transcribe_page
from durin.memory.pdf_coverage import (
    PdfCoverage,
    classify_coverage,
    coverage_note,
    page_texts,
)

__all__ = [
    "SUPPORTED_SUFFIXES",
    "ConvertedDoc",
    "DocConvertError",
    "NeedsOcrJob",
    "convert_file_to_markdown",
    "is_convertible",
]

SUPPORTED_SUFFIXES = (
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".epub",
    ".html",
    ".htm",
    ".csv",
    ".json",
    ".xml",
    ".ipynb",
    ".zip",
)


class DocConvertError(ValueError):
    """Raised when a document cannot be converted to markdown."""


@dataclass(frozen=True)
class ConvertedDoc:
    markdown: str
    suffix: str
    coverage: PdfCoverage | None = None


class NeedsOcrJob(DocConvertError):
    """Raised when a PDF needs more OCR than the inline budget allows.

    Carries what a caller needs to enqueue the work: which pages, and how big
    the document is.
    """

    def __init__(self, message: str, *, pages: list[int], total_pages: int) -> None:
        super().__init__(message)
        self.pages = pages
        self.total_pages = total_pages


_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        from markitdown import MarkItDown

        _converter = MarkItDown()
    return _converter


def is_convertible(suffix: str) -> bool:
    """True when ``suffix`` (with leading dot) is a format markitdown parses."""
    return suffix.lower() in SUPPORTED_SUFFIXES


def convert_file_to_markdown(path: Path, *, documents_config=None) -> ConvertedDoc:
    """Convert a supported document to clean markdown.

    Raises :class:`DocConvertError` for an unsupported format, a converter
    failure, or an empty extraction (e.g. a scanned, image-only PDF with no
    text layer). ``OSError`` from reading the file propagates to the caller.

    For a PDF, pages with no text layer are transcribed with local OCR
    (subject to ``documents_config.ocr``). Raises :class:`NeedsOcrJob` when
    transcribing them inline would exceed the configured page budget — the
    caller enqueues the work as a background job instead. ``documents_config``
    is the ``DocumentsConfig`` to use; ``None`` loads the active config.
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DocConvertError(
            f"unsupported format: {suffix or 'no extension'} — "
            f"supported formats are {', '.join(SUPPORTED_SUFFIXES)}"
        )

    if documents_config is None:
        from durin.config.loader import load_config

        documents_config = load_config().documents

    from markitdown import MarkItDownException

    try:
        result = _get_converter().convert(str(path))
    except MarkItDownException as exc:
        raise DocConvertError(f"conversion failed: {exc}") from exc

    markdown = (result.text_content or "").strip()
    coverage: PdfCoverage | None = None

    if suffix == ".pdf":
        texts = page_texts(path)
        cov = classify_coverage(texts)
        if cov.empty_pages:
            ocr_cfg = documents_config.ocr
            if not ocr_cfg.enabled:
                # Still return the document. What text exists is worth having,
                # and the note tells the reader what is missing and how to fix it.
                note = coverage_note(cov, texts, ocr_enabled=False)
                return ConvertedDoc(
                    markdown=note + markdown, suffix=suffix, coverage=cov
                )
            pages = list(cov.empty_pages)
            if len(pages) > ocr_cfg.inline_max_pages:
                raise NeedsOcrJob(
                    f"{path.name}: {len(pages)} of {cov.total_pages} pages need "
                    f"OCR, over the inline limit of {ocr_cfg.inline_max_pages}. "
                    "Ingest the document to have it transcribed as a background job.",
                    pages=pages,
                    total_pages=cov.total_pages,
                )
            for page in pages:
                texts[page - 1] = transcribe_page(path, page)
            markdown = "\n\n".join(t for t in texts if t.strip())
            return ConvertedDoc(markdown=markdown, suffix=suffix, coverage=cov)
        # No pages need OCR: fall through to the shared empty-extraction
        # guard below, same as any other format, just carrying coverage.
        coverage = cov

    if not markdown:
        raise DocConvertError(
            f"{path.name} yielded no extractable text — scanned or image-only "
            "documents need OCR, which this converter does not do"
        )
    return ConvertedDoc(markdown=markdown, suffix=suffix, coverage=coverage)
