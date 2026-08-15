"""Shared document → markdown conversion (markitdown).

One conversion path in the codebase: the transient ``convert_to_markdown``
read tool and the durable ``memory_ingest`` path both go through
``convert_file_to_markdown`` so the supported-format set and the error
handling never drift apart.

Pure text/markdown formats are NOT handled here — callers read those
verbatim. This module owns the binary/office/PDF formats markitdown parses.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from durin.memory.ocr import OcrUnavailable, engine_available, transcribe_pages_detached
from durin.memory.pdf_coverage import (
    EMPTY_PAGE_CHARS,
    PdfCoverage,
    classify_counts,
    classify_coverage,
    coverage_note,
    page_char_counts,
    page_texts,
    page_texts_subset,
)

__all__ = [
    "SUPPORTED_SUFFIXES",
    "ConvertedDoc",
    "DocConvertError",
    "NeedsOcrJob",
    "convert_file_to_markdown",
    "is_convertible",
]

logger = logging.getLogger(__name__)

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
    """The result of converting one document to markdown.

    ``ocr_stub`` is True when ``markdown`` is a coverage note standing in for
    pages that need OCR but could not get it right now (the setting is off,
    or the engine is missing/failed) rather than a real transcription. A
    caller that persists this document should record the flag: it is what
    lets a later re-ingest tell "nothing more to do" apart from "OCR's
    situation may have changed, worth trying again" without re-running the
    conversion just to find out. Defaults False -- every other return path
    (a real transcription, or a document that never needed OCR at all) is
    final and never needs a second look.
    """

    markdown: str
    suffix: str
    coverage: PdfCoverage | None = None
    ocr_stub: bool = False


class NeedsOcrJob(DocConvertError):
    """Raised when a PDF needs more OCR than the inline budget allows.

    Carries what a caller needs to enqueue the work: which pages, and how big
    the document is.

    ``pages`` is a floor, not necessarily the whole set. Deciding that a
    document is over budget only takes confirming one page more than the budget
    allows, and confirming the rest of a four-hundred-page book is exactly the
    work this exception exists to defer — so the caller's worker widens the
    list itself before it starts. ``estimated_pages`` is how many pages are
    expected to need OCR: it sizes the job for the reader, and nothing decides
    anything from it.
    """

    def __init__(
        self, message: str, *, pages: list[int], total_pages: int,
        estimated_pages: int | None = None,
    ) -> None:
        super().__init__(message)
        self.pages = pages
        self.total_pages = total_pages
        self.estimated_pages = (
            len(pages) if estimated_pages is None else estimated_pages
        )


_converter = None


def _get_converter():
    global _converter
    if _converter is None:
        from markitdown import MarkItDown

        _converter = MarkItDown()
    return _converter


def _markitdown_text(path: Path) -> str:
    """markitdown's own extraction of the whole file, as a stripped string."""
    from markitdown import MarkItDownException

    try:
        result = _get_converter().convert(str(path))
    except MarkItDownException as exc:
        raise DocConvertError(f"conversion failed: {exc}") from exc
    return (result.text_content or "").strip()


def _confirm_empty_pages(path: Path, candidates: Sequence[int]) -> list[int]:
    """Which of *candidates* really have no text layer. One extraction pass.

    The probe says a page might be empty; only the accurate extractor says it
    is. The caller hands over just enough candidates to settle its question —
    one more than the inline budget allows — and this confirms them in a single
    call.

    Deliberately one call and no more. Confirmation is an attempt at a
    shortcut, not a search: if this batch does not settle it, the caller falls
    through to the full accurate pass, which answers exactly. Walking the rest
    of the flagged pages instead would re-open the PDF per batch, and opening
    is O(total pages) — on a long document whose pages all sit in the probe's
    slack that costs many times the pass it was avoiding.

    Sound in the one direction that matters. Every page in the result was
    measured exactly the way the classifier measures it, so the result is a
    subset of the pages a full classification would call empty, never a
    superset — and a subset that already exceeds a budget proves the whole set
    does. A page the extractor cannot see at all is not a page confirmed empty,
    so it does not count.

    Raises :class:`DocConvertError` if the extraction pass itself fails (a
    corrupt PDF, pdfplumber raising) — a raw exception here would otherwise
    escape past the taxonomy every caller of :func:`convert_file_to_markdown`
    already handles.
    """
    try:
        texts = page_texts_subset(path, candidates)
    except Exception as exc:  # noqa: BLE001
        raise DocConvertError(
            f"{path.name}: confirming flagged pages: {exc}"
        ) from exc
    confirmed: list[int] = []
    for page in candidates:
        text = texts.get(page)
        # len(text.strip()) is the measure EMPTY_PAGE_CHARS is calibrated
        # against, and the one classify_coverage applies to the same string.
        if text is not None and len(text.strip()) < EMPTY_PAGE_CHARS:
            confirmed.append(page)
    return confirmed


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

    coverage: PdfCoverage | None = None
    _markitdown_memo: str | None = None

    def markitdown_text() -> str:
        """markitdown's extraction, converted on first use and at most once.

        Deliberately lazy. Every path that transcribes pages returns the
        accurate per-page text joined instead, and the over-budget path returns
        nothing at all — for a scanned book, converting first means a full
        conversion thrown away unread.
        """
        nonlocal _markitdown_memo
        if _markitdown_memo is None:
            _markitdown_memo = _markitdown_text(path)
        return _markitdown_memo

    if suffix == ".pdf":
        ocr_cfg = documents_config.ocr
        cov: PdfCoverage
        texts: list[str] = []
        # Probe first, and let what it finds decide what else has to run. The
        # probe is a cheap per-page glyph count and a deliberate under-estimate
        # of what the classifier measures, so it can only err towards calling a
        # page empty. That is the only direction it is safe to trust: a page it
        # does NOT flag is a page both extractors agree has text, while a page
        # it flags has to be confirmed by the accurate extractor before
        # anything is decided from it.
        try:
            counts = page_char_counts(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pdf coverage probe failed for %s: %s", path.name, exc)
            # No probe, no shortcut: the order this branch had before the probe
            # existed. markitdown runs first because it is what turns a file no
            # library can read into a DocConvertError, rather than letting a
            # raw extractor exception escape to callers that catch neither.
            # That guard only helps when markitdown ALSO fails, though — when
            # it succeeds and this extraction independently raises, the raw
            # exception needs its own wrap.
            markitdown_text()
            try:
                texts = page_texts(path)
            except Exception as page_exc:  # noqa: BLE001
                raise DocConvertError(
                    f"{path.name}: extracting page text after the coverage "
                    f"probe failed: {page_exc}"
                ) from page_exc
            cov = classify_coverage(texts)
        else:
            # Through the classifier rather than against the threshold
            # directly, so what counts as an empty page is decided in exactly
            # one place whichever measurement it is applied to.
            probe_cov = classify_counts(counts)
            flagged = list(probe_cov.empty_pages)
            if not flagged:
                # The majority of PDFs: a real text layer on every page. The
                # probe alone settles it, and this is the only path that pays
                # for nothing but the conversion.
                cov = probe_cov
            else:
                if (
                    ocr_cfg.enabled
                    and engine_available()
                    and len(flagged) > ocr_cfg.inline_max_pages
                ):
                    # A scanned book on its way to a background job. Confirming
                    # one page more than the budget allows is the whole proof —
                    # the confirmed pages are a subset of the truly empty ones,
                    # so a subset over the budget puts the whole set over it —
                    # and it costs a handful of pages instead of the document.
                    # The worker derives the exhaustive list itself.
                    #
                    # One attempt only. If these do not settle it the code
                    # below answers exactly, for about four times what another
                    # confirmation batch would cost.
                    confirmed = _confirm_empty_pages(
                        path, flagged[: ocr_cfg.inline_max_pages + 1]
                    )
                    if len(confirmed) > ocr_cfg.inline_max_pages:
                        raise NeedsOcrJob(
                            f"{path.name}: at least {len(confirmed)} of "
                            f"{len(counts)} pages need OCR, over the inline "
                            f"limit of {ocr_cfg.inline_max_pages}. Ingest the "
                            "document to have it transcribed as a background job.",
                            pages=confirmed,
                            total_pages=len(counts),
                            estimated_pages=len(flagged),
                        )
                # Either OCR is not going to run, or confirmation could not
                # blow the budget. Both want the accurate, expensive extraction
                # and a classification from it: it is what labels the gaps in
                # the note, what the transcribed pages are merged into, and the
                # only measure allowed to decide which pages are empty — the
                # probe can miss one, on a font it decodes and pdfplumber does
                # not.
                try:
                    texts = page_texts(path)
                except Exception as exc:  # noqa: BLE001
                    raise DocConvertError(
                        f"{path.name}: extracting page text: {exc}"
                    ) from exc
                cov = classify_coverage(texts)

        if cov.empty_pages:
            # Two ways OCR does not happen, and they are not the same thing to
            # a reader: the setting is off, or the setting is on but the engine
            # is not installed (documents.ocr.enabled is a config key, the
            # engine is an install extra — a redeploy without it leaves exactly
            # this state). Both keep the document readable with a note; only
            # the wording differs.
            if not ocr_cfg.enabled or not engine_available():
                # Still return the document. What text exists is worth having,
                # and the note tells the reader what is missing and how to fix
                # it. ocr_stub=True: a caller that persists this must not treat
                # it as the last word -- OCR being turned on later is exactly
                # the situation change that makes this note stale.
                note = coverage_note(cov, texts, engine_missing=ocr_cfg.enabled)
                return ConvertedDoc(
                    markdown=note + markitdown_text(), suffix=suffix, coverage=cov,
                    ocr_stub=True,
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
            try:
                transcribed = transcribe_pages_detached(
                    path, pages, language=ocr_cfg.language
                )
                for page in pages:
                    texts[page - 1] = transcribed[page].text
            except (OcrUnavailable, ImportError) as exc:
                # engine_available() above only proves ``import rapidocr``
                # works; the subprocess's own imports can still fail
                # underneath it, it can be killed, and it can time out — the
                # transcription call raises the same OcrUnavailable for all of
                # them, and it is a RuntimeError no caller of this function
                # catches. Same outcome as finding the engine missing before
                # starting: the document, and a note saying why it has gaps.
                # The exception's own message is the only account of which
                # failure this was, and this log line is the only place it is
                # written down, so it goes in verbatim.
                logger.warning(
                    "%s: local OCR is enabled but its engine failed to produce "
                    "a transcription (%s); returning the document with a "
                    "coverage note", path.name, exc,
                )
                note = coverage_note(cov, texts, engine_missing=True)
                # Same ocr_stub=True as the engine-missing branch above: the
                # engine merely failing THIS run does not mean it always will.
                return ConvertedDoc(
                    markdown=note + markitdown_text(), suffix=suffix, coverage=cov,
                    ocr_stub=True,
                )
            markdown = "\n\n".join(t for t in texts if t.strip())
            if not markdown:
                # The det pass tells two invisible documents apart: pages of
                # blank paper, or print the engine detected but could not
                # read. Only the second message sends the reader anywhere
                # useful — at pages that LOOK blank in the output, "rescan
                # the document" is otherwise the natural, wrong conclusion.
                unreadable = sum(1 for p in transcribed.values() if p.det_boxes)
                if unreadable:
                    raise DocConvertError(
                        f"{path.name} yielded no extractable text even after "
                        f"OCR — the engine detected printed text on "
                        f"{unreadable} of the {len(transcribed)} transcribed "
                        "page(s) but could not read it; a script outside its "
                        "built-in models (they read Chinese, Japanese and "
                        "Latin-script languages) is the usual cause"
                    )
                raise DocConvertError(
                    f"{path.name} yielded no extractable text even after OCR — every "
                    "transcribed page came back blank"
                )
            return ConvertedDoc(markdown=markdown, suffix=suffix, coverage=cov)
        # No pages need OCR: fall through to the shared empty-extraction
        # guard below, same as any other format, just carrying coverage.
        coverage = cov

    markdown = markitdown_text()
    if not markdown:
        raise DocConvertError(
            f"{path.name} yielded no extractable text — scanned or image-only "
            "documents need OCR, which this converter does not do"
        )
    return ConvertedDoc(markdown=markdown, suffix=suffix, coverage=coverage)
