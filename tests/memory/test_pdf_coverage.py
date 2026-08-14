"""Coverage classification for PDFs with missing text layers."""

import pytest

from durin.memory.pdf_coverage import (
    PdfCoverage,
    classify_counts,
    classify_coverage,
    gap_ranges,
    page_char_counts,
    page_texts,
    page_texts_subset,
)

# A corner stamp on an otherwise image-only page: 15 glyphs over 4 lines. Under
# EMPTY_PAGE_CHARS however it is measured (18 chars with "\n" separators), but
# 21 if the separators are counted as "\r\n" — which is what a page with no
# text layer must never be mistaken for.
_STAMP = "EX-14\nB-317\nARC\np7"


def _stamped_report() -> list[str]:
    """An 8-page report with three scanned inserts carrying only a stamp."""
    pages = ["Body text long enough to count as a real page"] * 8
    for i in (3, 4, 5):
        pages[i] = _STAMP
    return pages


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


def test_page_texts_subset_reads_only_the_pages_it_was_asked_for(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "three.pdf"
    _write_text_pdf(pdf, ["Alpha page one", "Beta page two", "Gamma page three"])

    subset = page_texts_subset(pdf, [1, 3])

    assert sorted(subset) == [1, 3]
    assert "Alpha" in subset[1]
    assert "Gamma" in subset[3]


def test_page_texts_subset_says_exactly_what_the_full_extraction_says(tmp_path):
    # The whole point of the subset extractor is to stand in for page_texts on
    # the pages a caller cares about. If a page's text differs between the two,
    # a page confirmed empty from the subset is not the same fact as a page
    # classified empty from the full pass, and the classification stops being
    # comparable. Same extractor, same measure, 1-based both.
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "mixed.pdf"
    _write_text_pdf(
        pdf,
        ["Chapter one opens with plenty of body text", "", _STAMP,
         "Real body text here\nspread over three lines\nof a genuine page", ""],
    )

    full = page_texts(pdf)
    subset = page_texts_subset(pdf, range(1, len(full) + 1))

    assert subset == {i + 1: text for i, text in enumerate(full)}


def test_page_texts_subset_leaves_out_a_page_the_document_does_not_have(tmp_path):
    # Absent, not empty. "pdfplumber cannot see this page" and "this page has
    # no text layer" are different answers, and a caller confirming which pages
    # are empty must never read the first as the second — that would confirm a
    # page it never actually looked at.
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "two.pdf"
    _write_text_pdf(pdf, ["Alpha page one", "Beta page two"])

    subset = page_texts_subset(pdf, [2, 9])

    assert sorted(subset) == [2]


def test_page_char_counts_counts_glyphs_not_whitespace(tmp_path):
    # Glyphs only, no whitespace of any kind: the two extractors disagree about
    # whitespace (line separators most of all), and a count that includes it
    # cannot be compared against pdfplumber's.
    from tests.tools.test_read_enhancements import _write_text_pdf

    page = "Alpha page one and some more text"
    pdf = tmp_path / "gap.pdf"
    _write_text_pdf(pdf, [page, "", "Gamma page three"])

    counts = page_char_counts(pdf)
    assert len(counts) == 3
    assert counts[0] == len("".join(page.split()))  # 27, not the string's 33
    assert counts[1] == 0


def test_the_cheap_probe_classifies_a_real_pdf_the_same_way(tmp_path):
    # The probe exists only to skip the accurate extractor when no page needs
    # attention. It earns that only if it reaches the same verdict.
    #
    # The multi-line cases are not decoration. The two extractors separate
    # lines differently (pdfium "\r\n", pdfplumber "\n"), so a page's character
    # count depends on how many lines its text is split across — and a
    # single-line fixture cannot show that at all.
    from tests.tools.test_read_enhancements import _write_text_pdf

    for name, pages in (
        ("gapped", ["Chapter one opens with plenty of body text", "", "", ""]),
        ("all-text", ["Body text long enough to count as a real page"] * 6),
        ("all-blank", [""] * 5),
        ("single-page", ["Only page here, with plenty of body text on it"]),
        ("stamped-scan", [_STAMP] * 6),
        ("stamped-inserts", _stamped_report()),
        ("multiline-body", ["Real body text here\nspread over three lines\nof a genuine page"] * 4),
    ):
        pdf = tmp_path / f"probe-{name}.pdf"
        _write_text_pdf(pdf, pages)
        assert classify_counts(page_char_counts(pdf)) == classify_coverage(page_texts(pdf)), name


def test_a_multi_line_stamp_does_not_read_as_a_page_with_a_text_layer(tmp_path):
    """The shape this whole feature exists for: a scanned page whose only text
    is a short stamp laid out over several lines — an exhibit block, a Bates
    number, an archive slug. Its glyphs fall under the threshold, but counting
    the line separators too can push it over, and then the page reads as
    genuine text and its content is silently dropped."""
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "stamped.pdf"
    _write_text_pdf(pdf, [_STAMP] * 6)

    accurate = classify_coverage(page_texts(pdf))
    assert accurate.empty_pages == (1, 2, 3, 4, 5, 6)
    assert accurate.kind == "scanned"
    assert classify_counts(page_char_counts(pdf)) == accurate


def test_classify_counts_and_classify_coverage_are_the_same_classifier():
    # The page with internal newlines is the one that discriminates: it fails
    # if either side stops measuring `len(text.strip())` (whitespace included)
    # — the measure the EMPTY_PAGE_CHARS threshold is calibrated against.
    texts = ["", "x" * 19, "y" * 20, "a\nb\nc\nd\ne\nf\ng\nh\ni\nj", "body text long enough"]
    assert classify_counts([len(t.strip()) for t in texts]) == classify_coverage(texts)


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
    assert coverage_note(cov, texts) == ""


def test_partial_note_lists_gap_ranges():
    texts = _texts([True, True, False, False, False, True, True, True, True, True])
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts)
    assert "3 of 10 pages" in note
    assert "pages 3-5" in note


def test_partial_note_labels_a_gap_with_the_text_before_it():
    texts = ["Chapter Four: Financial Statements", "", "", *_texts([True] * 7)]
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts)
    assert "Chapter Four: Financial Statements" in note


def test_scanned_note_does_not_list_every_page_as_a_gap():
    texts = _texts([False] * 412)
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts)
    assert "412" in note
    assert "pages 1-412" not in note


def test_note_with_ocr_disabled_says_how_to_enable_it():
    texts = _texts([False] * 50)
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts)
    assert "documents.ocr.enabled" in note


def test_note_with_engine_missing_says_the_engine_is_the_reason():
    texts = _texts([False] * 50)
    cov = classify_coverage(texts)
    note = coverage_note(cov, texts, engine_missing=True)
    assert "documents.ocr.enabled" not in note
    assert "[ocr] extra" in note
