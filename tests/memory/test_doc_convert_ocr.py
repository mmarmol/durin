"""Conversion with OCR: the inline budget, the coverage note, the job hand-off."""

import pytest

from durin.config.schema import DocumentsConfig
from durin.memory.doc_convert import (
    NeedsOcrJob,
    convert_file_to_markdown,
)
from durin.memory.ocr import OcrUnavailable


def _cfg(*, enabled=True, inline_max_pages=5):
    return DocumentsConfig.model_validate(
        {"ocr": {"enabled": enabled, "inline_max_pages": inline_max_pages}}
    )


@pytest.fixture(autouse=True)
def _engine_present(monkeypatch):
    """Stand in for an installed OCR engine.

    The [ocr] extra is absent in CI, and every test here that transcribes
    fakes ``transcribe_page`` anyway — so it must fake the availability probe
    too, or it exercises the missing-engine path instead of the one it means
    to. The tests that are *about* a missing engine override this.
    """
    monkeypatch.setattr("durin.memory.doc_convert.engine_available", lambda: True)


@pytest.fixture()
def two_page_scan(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "invoice.pdf"
    _write_text_pdf(pdf, ["", ""])
    return pdf


@pytest.fixture()
def big_scan(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "book.pdf"
    _write_text_pdf(pdf, [""] * 40)
    return pdf


def test_an_ordinary_pdf_never_runs_the_expensive_extractor(tmp_path, monkeypatch):
    # The common case — every page has a text layer — must not pay for a second
    # full text extraction on top of the conversion it already did. The cheap
    # per-page probe answers "does any page need attention" on its own.
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "ordinary.pdf"
    _write_text_pdf(pdf, ["A page with plenty of ordinary body text on it"] * 8)

    calls = []
    monkeypatch.setattr(
        "durin.memory.doc_convert.page_texts",
        lambda path: calls.append(path) or [],
    )
    out = convert_file_to_markdown(pdf, documents_config=_cfg())
    assert calls == []
    assert out.coverage is not None
    assert out.coverage.total_pages == 8
    assert out.coverage.empty_pages == ()


def test_a_pdf_with_gaps_still_gets_the_accurate_extraction(tmp_path):
    # The note labels each gap with the text that precedes it, and that label
    # comes from the accurate extractor — the probe counts characters only.
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "gapped.pdf"
    _write_text_pdf(pdf, ["Chapter Four: Financial Statements", "", "", ""])

    out = convert_file_to_markdown(pdf, documents_config=_cfg(enabled=False))
    assert "Chapter Four: Financial Statements" in out.markdown
    assert out.coverage is not None
    assert out.coverage.empty_pages == (2, 3, 4)


# A corner stamp on an otherwise image-only page: an exhibit block, a Bates
# number, an archive slug. 15 glyphs over 4 lines — under EMPTY_PAGE_CHARS, but
# only if the probe measures the page the way the classifier's threshold is
# calibrated for.
_STAMP = "EX-14\nB-317\nARC\np7"


def test_a_scan_whose_pages_carry_a_stamp_still_gets_its_coverage_note(tmp_path):
    """What the reader loses if the cheap probe over-counts a sparse page: the
    document reads as though the stamps were its content, with no note saying
    six pages of it could not be read."""
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "stamped_scan.pdf"
    _write_text_pdf(pdf, [_STAMP] * 6)

    out = convert_file_to_markdown(pdf, documents_config=_cfg(enabled=False))

    assert "SCANNED DOCUMENT" in out.markdown
    assert out.coverage is not None
    assert out.coverage.kind == "scanned"


def test_stamped_scanned_inserts_are_transcribed_like_blank_ones(tmp_path, monkeypatch):
    """The same page, with OCR on: a stamped insert needs transcribing exactly
    as much as a page with nothing on it at all. A probe that reads the stamp
    as a text layer skips the whole OCR branch and the insert is lost."""
    from tests.tools.test_read_enhancements import _write_text_pdf

    texts = ["Body text long enough to count as a real page"] * 8
    for i in (3, 4, 5):
        texts[i] = _STAMP
    pdf = tmp_path / "stamped_report.pdf"
    _write_text_pdf(pdf, texts)

    transcribed = []
    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_page",
        lambda path, page, **kw: transcribed.append(page) or f"insert {page}",
    )

    out = convert_file_to_markdown(pdf, documents_config=_cfg(inline_max_pages=5))

    assert transcribed == [4, 5, 6]
    assert "insert 5" in out.markdown


def test_text_pdf_is_unchanged_by_the_ocr_path(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "plain.pdf"
    _write_text_pdf(pdf, ["A page with plenty of ordinary body text on it"])

    out = convert_file_to_markdown(pdf, documents_config=_cfg())
    assert "ordinary body text" in out.markdown
    assert "COVERAGE WARNING" not in out.markdown


def test_small_scan_is_transcribed_inline(two_page_scan, monkeypatch):
    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_page",
        lambda path, page, **kw: f"transcribed page {page}",
    )
    out = convert_file_to_markdown(two_page_scan, documents_config=_cfg())
    assert "transcribed page 1" in out.markdown
    assert "transcribed page 2" in out.markdown


def test_scan_over_the_budget_raises_needs_ocr_job(big_scan):
    with pytest.raises(NeedsOcrJob) as excinfo:
        convert_file_to_markdown(big_scan, documents_config=_cfg(inline_max_pages=5))
    assert excinfo.value.total_pages == 40
    assert excinfo.value.pages == list(range(1, 41))


def test_exactly_the_budget_stays_inline_one_more_goes_to_a_job(tmp_path, monkeypatch):
    # The check is `>`, not `>=`: a document needing exactly inline_max_pages
    # pages of OCR must still be handled inline, and one more page over that
    # must be the one that tips it into a background job.
    from tests.tools.test_read_enhancements import _write_text_pdf

    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_page",
        lambda path, page, **kw: f"transcribed page {page}",
    )

    at_budget = tmp_path / "at_budget.pdf"
    _write_text_pdf(at_budget, [""] * 5)
    out = convert_file_to_markdown(at_budget, documents_config=_cfg(inline_max_pages=5))
    assert "transcribed page 5" in out.markdown

    over_budget = tmp_path / "over_budget.pdf"
    _write_text_pdf(over_budget, [""] * 6)
    with pytest.raises(NeedsOcrJob):
        convert_file_to_markdown(over_budget, documents_config=_cfg(inline_max_pages=5))


def test_budget_counts_pages_needing_ocr_not_document_pages(tmp_path, monkeypatch):
    # A long report with three scanned inserts stays inline.
    from tests.tools.test_read_enhancements import _write_text_pdf

    texts = ["Body text long enough to count as a real page"] * 100
    for i in (10, 11, 12):
        texts[i] = ""
    pdf = tmp_path / "report.pdf"
    _write_text_pdf(pdf, texts)

    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_page",
        lambda path, page, **kw: f"insert {page}",
    )
    out = convert_file_to_markdown(pdf, documents_config=_cfg(inline_max_pages=5))
    assert "insert 11" in out.markdown


def test_ocr_disabled_keeps_the_document_readable_with_a_note(big_scan):
    # Not an exception: the caller still gets whatever text exists, plus the
    # note saying what is missing and how to turn OCR on.
    out = convert_file_to_markdown(big_scan, documents_config=_cfg(enabled=False))
    assert "documents.ocr.enabled" in out.markdown


def test_ocr_enabled_without_the_engine_reads_like_ocr_being_off(big_scan, monkeypatch):
    # documents.ocr.enabled is a config key; the engine is an install extra.
    # Turning the key on without installing the extra must not break every PDF
    # conversion in the process — the document still comes back, with a note
    # that names the real problem.
    monkeypatch.setattr("durin.memory.doc_convert.engine_available", lambda: False)

    out = convert_file_to_markdown(big_scan, documents_config=_cfg())

    assert "[ocr]" in out.markdown
    assert out.coverage is not None


def test_no_job_is_enqueued_for_a_book_no_engine_can_transcribe(big_scan, monkeypatch):
    # big_scan is 40 pages over a budget of 5, so this is the NeedsOcrJob path.
    # A background job would fail on page 1 and leave the document with no
    # sidecar and no Library entry; the note is the useful answer instead.
    monkeypatch.setattr("durin.memory.doc_convert.engine_available", lambda: False)

    out = convert_file_to_markdown(big_scan, documents_config=_cfg(inline_max_pages=5))

    assert "[ocr]" in out.markdown


@pytest.mark.parametrize(
    "boom",
    [
        pytest.param(OcrUnavailable("no extra"), id="ocr-unavailable"),
        # engine_available() only proves `import rapidocr` works; its own
        # imports can still fail underneath.
        pytest.param(ImportError("onnxruntime is broken"), id="broken-lazy-import"),
    ],
)
def test_an_engine_that_fails_at_transcribe_time_is_handled_the_same_way(
    two_page_scan, monkeypatch, boom
):
    def _raise(path, page, **kw):
        raise boom

    monkeypatch.setattr("durin.memory.doc_convert.transcribe_page", _raise)

    out = convert_file_to_markdown(two_page_scan, documents_config=_cfg())

    assert "[ocr]" in out.markdown


def test_partial_scan_only_transcribes_the_empty_pages(tmp_path, monkeypatch):
    from tests.tools.test_read_enhancements import _write_text_pdf

    texts = ["Real body text on this page, plenty of it"] * 10
    texts[3] = ""
    texts[4] = ""
    pdf = tmp_path / "mixed.pdf"
    _write_text_pdf(pdf, texts)

    transcribed = []
    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_page",
        lambda path, page, **kw: transcribed.append(page) or f"ocr {page}",
    )
    convert_file_to_markdown(pdf, documents_config=_cfg())
    assert transcribed == [4, 5]


def test_coverage_is_reported_on_the_result(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "plain.pdf"
    _write_text_pdf(pdf, ["Ordinary text on the only page here"])

    out = convert_file_to_markdown(pdf, documents_config=_cfg())
    assert out.coverage is not None
    assert out.coverage.kind == "text"


def test_non_pdf_documents_never_touch_the_ocr_path(tmp_path):
    # Only PDFs are in scope. A DOCX with no text must not be rasterised.
    out_path = tmp_path / "note.csv"
    out_path.write_text("a,b\n1,2\n")
    out = convert_file_to_markdown(out_path, documents_config=_cfg())
    assert out.coverage is None
