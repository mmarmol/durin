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
    "classify_counts",
    "classify_coverage",
    "coverage_note",
    "gap_ranges",
    "page_char_counts",
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
    return classify_counts([len(text.strip()) for text in page_texts])


def classify_counts(char_counts: list[int]) -> PdfCoverage:
    """Classify a document from its per-page character counts.

    The same classifier as :func:`classify_coverage` — that one measures the
    text it is handed and calls this. Split out so a caller that only needs
    the verdict can count characters the cheap way instead of paying for a
    full layout-aware extraction it would throw away.
    """
    total = len(char_counts)
    empty = tuple(
        i + 1 for i, count in enumerate(char_counts) if count < EMPTY_PAGE_CHARS
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


def page_char_counts(path: Path) -> list[int]:
    """How many characters each page carries, one count per page.

    A cheap stand-in for ``len(page_texts(path)[i].strip())``. pypdfium2 reads
    the page's text objects straight out; pdfplumber reconstructs reading order
    from per-character geometry, which is what makes it accurate and what makes
    it two orders of magnitude slower. Reading order does not change a
    character count, and a page with a text layer clears ``EMPTY_PAGE_CHARS``
    by orders of magnitude either way — so this is enough to decide *whether* a
    page needs a closer look, and nothing more. Anything that reads the text
    itself must call :func:`page_texts`.
    """
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(path))
    try:
        counts = []
        for page in doc:
            textpage = page.get_textpage()
            try:
                counts.append(len(textpage.get_text_range().strip()))
            finally:
                textpage.close()
        return counts
    finally:
        doc.close()


def page_texts(path: Path) -> list[str]:
    """Extracted text per page, one string per page in document order.

    A page that yields nothing comes back as an empty string rather than
    being skipped, so page numbers stay aligned with the list index.
    """
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return [(page.extract_text() or "") for page in pdf.pages]


# How much of the preceding page's text to quote when labelling a gap.
_GAP_LABEL_CHARS = 60


def _gap_label(texts: list[str], first_empty: int) -> str:
    """Quote the last text seen before a gap, so the reader knows which part
    of the document is missing rather than only which page numbers."""
    for idx in range(first_empty - 2, -1, -1):
        text = texts[idx].strip()
        if len(text) >= EMPTY_PAGE_CHARS:
            snippet = " ".join(text.split())[:_GAP_LABEL_CHARS]
            return f' — after "{snippet}" (p{idx + 1})'
    return ""


def coverage_note(
    cov: PdfCoverage, texts: list[str], *, ocr_enabled: bool,
    engine_missing: bool = False,
) -> str:
    """A note about what could not be read, for the model reading this document.

    Empty for a document whose text layer is intact. Callers prepend it to the
    extracted text rather than appending: a long extraction is paginated, and a
    footer would land on a page the reader may never fetch.

    ``engine_missing`` distinguishes the two ways OCR did not run: the setting
    is off (the reader is told to turn it on), or the setting is on and the
    engine is not installed (turning it on again would achieve nothing).
    """
    if cov.kind == "text":
        return ""

    if cov.kind == "scanned":
        head = (
            f"[SCANNED DOCUMENT: all {cov.total_pages} pages of this PDF are "
            "images with no text layer. Nothing below was extracted from them."
        )
    else:
        lines = []
        for first, last in gap_ranges(cov.empty_pages):
            span = f"page {first}" if first == last else f"pages {first}-{last}"
            count = last - first + 1
            plural = "" if count == 1 else "s"
            label = _gap_label(texts, first)
            lines.append(f"  {span} ({count} page{plural}){label}")
        gaps = "\n".join(lines)
        head = (
            f"[EXTRACTION COVERAGE WARNING: {len(cov.empty_pages)} of "
            f"{cov.total_pages} pages in this PDF yielded no text. Those pages "
            "are scanned images; their content is MISSING below, including "
            "where a section header appears with an empty body. Unreadable "
            f"ranges:\n{gaps}"
        )

    if ocr_enabled:
        tail = (
            " These pages can be transcribed with local OCR — ingest the "
            "document to have it done as a background job."
        )
    elif engine_missing:
        tail = (
            " Local OCR is turned on but its engine is not installed here, so "
            "nothing transcribed these pages. Installing durin's [ocr] extra "
            "is what fixes it; in the dashboard, switching local OCR off and "
            "on again under Settings > Documents offers to install it."
        )
    else:
        tail = (
            " Local OCR would transcribe these pages but is turned off. It is "
            "enabled in the dashboard under Settings > Documents, or by setting "
            "documents.ocr.enabled."
        )
    return head + tail + "]\n"
