"""The local OCR engine wrapper.

The [ocr] extra is not installed in CI, so anything touching the engine skips
there. The availability and error-path tests run everywhere.
"""

import logging
from pathlib import Path

import pytest

from durin.memory.ocr import OcrUnavailable, engine_available, render_page, transcribe_page


@pytest.fixture(autouse=True)
def _quiet_rapidocr_model_load(monkeypatch):
    """Silence RapidOCR's own stderr handler for exactly the ``RapidOCR()``
    construction call inside ``_get_engine`` — not the rest of the test.

    RapidOCR resets its shared "RapidOCR" logger's level back to its
    configured default (INFO) on every ``RapidOCR()`` construction (rapidocr's
    main.py calls ``logger.setLevel(cfg.Global.log_level.upper())`` in
    __init__), so lowering the *logger's* level beforehand does not stick.
    Raising the *handler's* level survives that reset instead.

    The window is scoped to wrap only ``durin.memory.ocr._get_engine`` (via
    monkeypatch), rather than the whole test body, for two reasons. First, so
    any logging *after* construction — e.g. RapidOCR's own WARNING when a page
    has no detected text — is never touched. Second, and more importantly:
    pytest attaches its own diagnostic capture handler(s) to any
    non-propagating logger it discovers (RapidOCR sets ``propagate = False``
    on "RapidOCR"), and once that has happened for one test in a session, it
    stays attached for the rest of it. An earlier version of this fixture
    wrapped the whole test body and raised the level of *every* handler
    currently on the logger — which also caught pytest's own handler from the
    second relevant test onward in a session, silencing a genuine failure's
    log trail along with the routine noise. Filtering to
    ``type(h) is logging.StreamHandler`` — RapidOCR's own handler type
    exactly, not a subclass — leaves pytest's handler alone regardless of test
    order, so a failure elsewhere in the test (wrong text, a raised exception)
    keeps its diagnostic trail exactly as if this fixture were not here.

    Asserts the handler was actually found rather than silently doing
    nothing: if a future rapidocr release renames the logger or changes its
    handler's type, this must fail loudly, not quietly stop suppressing and
    let the noise back in unnoticed.
    """
    if not engine_available():
        yield
        return

    import durin.memory.ocr as ocr_module

    real_get_engine = ocr_module._get_engine

    def quiet_get_engine():
        from rapidocr import RapidOCR  # noqa: F401 — ensures the logger's handler exists

        handlers = [
            handler
            for handler in logging.getLogger("RapidOCR").handlers
            if type(handler) is logging.StreamHandler
        ]
        assert handlers, (
            'expected to find RapidOCR\'s own StreamHandler on the "RapidOCR" '
            "logger to silence it around construction; found none. Either "
            "rapidocr stopped attaching one, or changed its type — this "
            "filter needs updating, not silently skipping."
        )
        for handler in handlers:
            monkeypatch.setattr(handler, "level", logging.ERROR)
        return real_get_engine()

    monkeypatch.setattr(ocr_module, "_get_engine", quiet_get_engine)
    yield


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
