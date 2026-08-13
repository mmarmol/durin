"""Coverage classification for PDFs with missing text layers."""

import pytest

from durin.memory.pdf_coverage import (
    PdfCoverage,
    classify_coverage,
    gap_ranges,
    page_texts,
)


def test_all_pages_with_text_is_kind_text():
    cov = classify_coverage(["lorem ipsum dolor sit amet"] * 10)
    assert cov.kind == "text"
    assert cov.empty_pages == ()
    assert cov.total_pages == 10


def test_page_under_twenty_chars_counts_as_empty():
    cov = classify_coverage(["x" * 19, "y" * 20])
    assert cov.empty_pages == (1,)


def test_whitespace_only_page_counts_as_empty():
    cov = classify_coverage(["   \n\t  ", "y" * 40])
    assert cov.empty_pages == (1,)


def test_one_empty_page_is_not_enough_to_flag():
    # MIN_EMPTY is 2: a single blank page in a real document is normal.
    cov = classify_coverage(["", *["body text here, plenty of it"] * 9])
    assert cov.kind == "text"


def test_empty_ratio_over_twenty_percent_is_partial():
    # 3 of 10 == 30% > MIN_RATIO
    cov = classify_coverage(["", "", "", *["body text here, plenty of it"] * 7])
    assert cov.kind == "partial"
    assert cov.empty_pages == (1, 2, 3)


def test_ten_empty_pages_is_partial_even_under_the_ratio():
    # 10 of 100 == 10% < MIN_RATIO, but ABSOLUTE_EMPTY is 10.
    cov = classify_coverage([*[""] * 10, *["body text here, plenty of it"] * 90])
    assert cov.kind == "partial"


def test_nine_empty_pages_under_the_ratio_is_not_flagged():
    cov = classify_coverage([*[""] * 9, *["body text here, plenty of it"] * 91])
    assert cov.kind == "text"


def test_empty_pages_are_listed_even_when_the_document_is_not_flagged():
    # The thresholds decide whether gaps are worth reporting to a reader. They
    # must NOT decide whether the pages can be transcribed: a caller with a
    # local engine reads every page that has no text layer.
    cov = classify_coverage([*[""] * 3, *["body text here, plenty of it"] * 97])
    assert cov.kind == "text"
    assert cov.empty_pages == (1, 2, 3)


def test_fully_scanned_document_is_kind_scanned():
    cov = classify_coverage([""] * 412)
    assert cov.kind == "scanned"
    assert cov.total_pages == 412
    assert len(cov.empty_pages) == 412


def test_ninety_percent_empty_is_scanned_not_partial():
    # SCANNED_RATIO is 0.9: a cover page with text does not make a book "partial".
    cov = classify_coverage([*["title page text goes here"] * 10, *[""] * 90])
    assert cov.kind == "scanned"


def test_single_page_document_is_never_flagged():
    # MIN_EMPTY is 2, so a one-page scan classifies as text; the caller's
    # empty-extraction path already handles it.
    cov = classify_coverage([""])
    assert cov.kind == "text"


def test_gap_ranges_groups_consecutive_pages():
    assert gap_ranges((3, 4, 5, 9, 12, 13)) == [(3, 5), (9, 9), (12, 13)]


def test_gap_ranges_of_nothing_is_empty():
    assert gap_ranges(()) == []


def test_coverage_is_frozen():
    cov = classify_coverage(["text " * 10])
    assert isinstance(cov, PdfCoverage)
    with pytest.raises(Exception):
        cov.kind = "scanned"  # type: ignore[misc]


def test_page_texts_reads_one_string_per_page(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "three.pdf"
    _write_text_pdf(pdf, ["Alpha page one", "Beta page two", "Gamma page three"])

    texts = page_texts(pdf)
    assert len(texts) == 3
    assert "Alpha" in texts[0]
    assert "Gamma" in texts[2]


def test_page_texts_returns_empty_string_for_a_page_without_text(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "gap.pdf"
    _write_text_pdf(pdf, ["Alpha page one", "", "Gamma page three"])

    texts = page_texts(pdf)
    assert len(texts) == 3
    assert texts[1].strip() == ""


def test_classify_over_a_real_pdf(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "mixed.pdf"
    _write_text_pdf(pdf, ["Chapter one opens with plenty of body text", "", "", ""])

    cov = classify_coverage(page_texts(pdf))
    assert cov.kind == "partial"
    assert cov.empty_pages == (2, 3, 4)


from durin.memory.pdf_coverage import coverage_note


def _texts(pattern: list[bool]) -> list[str]:
    """True == a page with text, False == a scanned page."""
    return ["body text long enough to count" if ok else "" for ok in pattern]


def test_no_note_for_a_text_document():
    texts = _texts([True] * 10)
    cov = classify_coverage(texts)
    assert coverage_note(cov, texts, ocr_enabled=True) == ""


def test_partial_note_lists_gap_ranges():
    texts = _texts([True, True, False, False, False, True, True, True, True, True])
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts, ocr_enabled=True)
    assert "3 of 10 pages" in note
    assert "pages 3-5" in note


def test_partial_note_labels_a_gap_with_the_text_before_it():
    texts = ["Chapter Four: Financial Statements", "", "", *_texts([True] * 7)]
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts, ocr_enabled=True)
    assert "Chapter Four: Financial Statements" in note


def test_scanned_note_does_not_list_every_page_as_a_gap():
    texts = _texts([False] * 412)
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts, ocr_enabled=True)
    assert "412" in note
    assert "pages 1-412" not in note


def test_note_with_ocr_disabled_says_how_to_enable_it():
    texts = _texts([False] * 50)
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts, ocr_enabled=False)
    assert "documents.ocr.enabled" in note


def test_note_with_ocr_enabled_does_not_tell_the_model_to_enable_it():
    texts = _texts([False] * 50)
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts, ocr_enabled=True)
    assert "documents.ocr.enabled" not in note
