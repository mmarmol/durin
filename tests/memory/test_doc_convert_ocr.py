"""Conversion with OCR: the inline budget, the coverage note, the job hand-off."""

import pytest

from durin.config.schema import DocumentsConfig
from durin.memory.doc_convert import (
    NeedsOcrJob,
    convert_file_to_markdown,
)


def _cfg(*, enabled=True, inline_max_pages=5):
    return DocumentsConfig.model_validate(
        {"ocr": {"enabled": enabled, "inline_max_pages": inline_max_pages}}
    )


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
