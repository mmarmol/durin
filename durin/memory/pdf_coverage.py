"""How much of a PDF actually has a text layer.

A scanned page carries no extractable text: the PDF holds an image of the
page and nothing else. Measuring this per page separates three documents
that need different handling — an ordinary PDF, one with a few scanned
inserts, and a book that was scanned cover to cover.

Measurement only. Deciding what to do about the gaps belongs to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ABSOLUTE_EMPTY",
    "EMPTY_PAGE_CHARS",
    "MIN_EMPTY",
    "MIN_RATIO",
    "SCANNED_RATIO",
    "PdfCoverage",
    "classify_coverage",
    "gap_ranges",
    "page_texts",
]

# A page holding fewer than this many non-whitespace characters is treated as
# having no text layer. Page numbers and running headers land under it.
EMPTY_PAGE_CHARS = 20

# Below this many empty pages nothing is reported: one or two blank pages are
# ordinary in a real document.
MIN_EMPTY = 2

# Report when empty pages exceed this share of the document...
MIN_RATIO = 0.2

# ...or when this many pages are empty regardless of the share, because a
# hundred missing pages matter even in a thousand-page document.
ABSOLUTE_EMPTY = 10

# At or above this share the document is a scan, not a document with gaps:
# there is nothing to pick, so listing the gaps would be noise.
SCANNED_RATIO = 0.9


@dataclass(frozen=True)
class PdfCoverage:
    """Per-page text coverage of one PDF.

    ``empty_pages`` holds 1-based page numbers. ``kind`` is ``"text"`` (nothing
    to report), ``"partial"`` (scanned ranges inside a text document) or
    ``"scanned"`` (the whole document is images).
    """

    total_pages: int
    empty_pages: tuple[int, ...]
    kind: str


def classify_coverage(page_texts: list[str]) -> PdfCoverage:
    """Classify a document from its per-page extracted text."""
    total = len(page_texts)
    empty = tuple(
        i + 1
        for i, text in enumerate(page_texts)
        if len(text.strip()) < EMPTY_PAGE_CHARS
    )
    # ``empty_pages`` always lists every page without a text layer, whatever
    # ``kind`` comes out as. The thresholds decide whether the gaps are worth
    # telling a reader about; they do not decide whether the pages can be
    # transcribed. A caller with a cheap local engine transcribes all of them.
    if total == 0 or len(empty) < MIN_EMPTY:
        return PdfCoverage(total_pages=total, empty_pages=empty, kind="text")

    ratio = len(empty) / total
    if ratio >= SCANNED_RATIO:
        kind = "scanned"
    elif ratio >= MIN_RATIO or len(empty) >= ABSOLUTE_EMPTY:
        kind = "partial"
    else:
        kind = "text"
    return PdfCoverage(total_pages=total, empty_pages=empty, kind=kind)


def gap_ranges(empty_pages: tuple[int, ...]) -> list[tuple[int, int]]:
    """Group consecutive page numbers into inclusive ``(first, last)`` ranges."""
    ranges: list[tuple[int, int]] = []
    for page in empty_pages:
        if ranges and page == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], page)
        else:
            ranges.append((page, page))
    return ranges


def page_texts(path: Path) -> list[str]:
    """Extracted text per page, one string per page in document order.

    A page that yields nothing comes back as an empty string rather than
    being skipped, so page numbers stay aligned with the list index.
    """
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return [(page.extract_text() or "") for page in pdf.pages]
