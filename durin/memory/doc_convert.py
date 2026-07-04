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

__all__ = [
    "SUPPORTED_SUFFIXES",
    "ConvertedDoc",
    "DocConvertError",
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


def convert_file_to_markdown(path: Path) -> ConvertedDoc:
    """Convert a supported document to clean markdown.

    Raises :class:`DocConvertError` for an unsupported format, a converter
    failure, or an empty extraction (e.g. a scanned, image-only PDF with no
    text layer). ``OSError`` from reading the file propagates to the caller.
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise DocConvertError(
            f"unsupported format: {suffix or 'no extension'} — "
            f"supported formats are {', '.join(SUPPORTED_SUFFIXES)}"
        )

    from markitdown import MarkItDownException

    try:
        result = _get_converter().convert(str(path))
    except MarkItDownException as exc:
        raise DocConvertError(f"conversion failed: {exc}") from exc

    markdown = (result.text_content or "").strip()
    if not markdown:
        raise DocConvertError(
            f"{path.name} yielded no extractable text — scanned or image-only "
            "documents need OCR, which this converter does not do"
        )
    return ConvertedDoc(markdown=markdown, suffix=suffix)
