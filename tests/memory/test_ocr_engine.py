"""The local OCR engine wrapper.

The [ocr] extra is not installed in CI, so anything touching the engine skips
there. The availability and error-path tests run everywhere.
"""

import logging
from pathlib import Path

import pytest

from durin.memory.ocr import OcrUnavailable, engine_available, render_page, transcribe_page


@pytest.fixture(autouse=True)
def _quiet_rapidocr_console():
    """Silence RapidOCR's own stderr handler for the duration of each test.

    RapidOCR resets its shared "RapidOCR" logger's level back to its
    configured default (INFO) on every ``RapidOCR()`` construction, so
    lowering the *logger's* level beforehand does not stick — the engine's own
    __init__ overwrites it right back. The logger's handler is created once at
    first import and never touched by that reset, so raising the *handler's*
    level survives across constructions instead. This only trims routine
    INFO/WARNING chatter (model loads, blank-page notices); ERROR/CRITICAL
    output is untouched, and a real test failure (wrong text, a raised
    exception) fails exactly as loudly as it would without this fixture.
    """
    if not engine_available():
        yield
        return
    from rapidocr import RapidOCR  # noqa: F401 — forces the logger's handler to exist

    handlers = logging.getLogger("RapidOCR").handlers
    originals = [handler.level for handler in handlers]
    for handler in handlers:
        handler.setLevel(logging.ERROR)
    try:
        yield
    finally:
        for handler, level in zip(handlers, originals):
            handler.setLevel(level)


def test_engine_available_reports_a_bool():
    assert isinstance(engine_available(), bool)


def test_transcribe_raises_ocr_unavailable_without_the_extra(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.memory.ocr.engine_available", lambda: False)
    monkeypatch.setattr("durin.memory.ocr._engine", None, raising=False)
    with pytest.raises(OcrUnavailable):
        transcribe_page(tmp_path / "nope.pdf", 1)


def test_render_page_produces_png_bytes(tmp_path):
    # pypdfium2 arrives with the base install, so this runs in CI.
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "one.pdf"
    _write_text_pdf(pdf, ["Rendered page content"])

    data = render_page(pdf, 1)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_page_rejects_a_page_out_of_range(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "one.pdf"
    _write_text_pdf(pdf, ["only page"])

    with pytest.raises(ValueError):
        render_page(pdf, 2)


@pytest.mark.skipif(not engine_available(), reason="[ocr] extra not installed")
def test_transcribe_reads_text_off_a_rendered_page(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "hello.pdf"
    _write_text_pdf(pdf, ["HELLO OCR"])

    text = transcribe_page(pdf, 1)
    assert "HELLO" in text.upper()
