"""The local OCR engine wrapper.

The [ocr] extra is not installed in CI, so anything touching the engine skips
there. The availability and error-path tests run everywhere.
"""

import json
import logging
import resource
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from durin.memory.ocr import (
    OcrUnavailable,
    engine_available,
    render_page,
    transcribe_page,
    transcribe_pages_detached,
)


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
    monkeypatch), rather than the whole test body, for two reasons. First, a
    test that never constructs an engine keeps its logging untouched, and the
    handler lookup runs where ``import rapidocr`` has certainly happened — the
    handler does not exist before that import. Note what the scoping does not
    buy: ``monkeypatch.setattr(handler, "level", ...)`` is undone at fixture
    teardown, not when the wrapped call returns, so from the first construction
    onward the suppression lasts the rest of that test — RapidOCR's own WARNING
    for a page with no detected text included. Second, and more importantly:
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


# --- transcribe_pages_detached: the parent-side call to the OCR subprocess ---
#
# These fake ``subprocess.run`` rather than the engine, so they run everywhere
# (no [ocr] extra needed) and prove the contract between
# ``transcribe_pages_detached`` and the child it spawns: how it is invoked,
# and how each outcome the child can produce (clean JSON, a noisy failure, a
# hang, an incomplete result) turns into either a parsed dict or the single
# ``OcrUnavailable`` every existing caller already degrades on.


def _fake_run(stdout="", stderr="", returncode=0):
    def run(cmd, *, capture_output, text, timeout):
        assert capture_output is True and text is True  # streams must stay separate and str
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return run


def test_transcribe_pages_detached_parses_stdout_into_a_page_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "durin.memory.ocr.subprocess.run",
        _fake_run(stdout='{"pages": {"1": "hello", "2": "world"}}'),
    )

    result = transcribe_pages_detached(tmp_path / "doc.pdf", [1, 2])

    assert result == {1: "hello", 2: "world"}


def test_transcribe_pages_detached_invokes_the_subprocess_module_by_argv_contract(
    monkeypatch, tmp_path
):
    """argv is <pdf_path> <dpi> <page> [<page>...], run as ``python -m
    durin.memory.ocr_subproc`` so it resolves via sys.executable regardless of
    the caller's cwd — the same pattern ``durin.jobs.spawn`` already uses for
    the OCR worker."""
    seen = {}

    def run(cmd, *, capture_output, text, timeout):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"pages": {"3": "x", "7": "y"}}', stderr=""
        )

    monkeypatch.setattr("durin.memory.ocr.subprocess.run", run)

    transcribe_pages_detached(tmp_path / "doc.pdf", [3, 7], dpi=150)

    assert seen["cmd"] == [
        sys.executable, "-m", "durin.memory.ocr_subproc",
        str(tmp_path / "doc.pdf"), "150", "3", "7",
    ]


def test_transcribe_pages_detached_default_timeout_scales_with_page_count(monkeypatch, tmp_path):
    # Stated as a comment at the call site rather than a config knob: 60s for
    # interpreter startup + model load, 10s per page for render + inference.
    seen = {}

    def run(cmd, *, capture_output, text, timeout):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"pages": {"1": "a", "2": "b", "3": "c"}}', stderr=""
        )

    monkeypatch.setattr("durin.memory.ocr.subprocess.run", run)

    transcribe_pages_detached(tmp_path / "doc.pdf", [1, 2, 3])

    assert seen["timeout"] == 60 + 10 * 3


def test_transcribe_pages_detached_raises_on_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "durin.memory.ocr.subprocess.run",
        _fake_run(
            stdout="", stderr="Traceback (most recent call last):\nRuntimeError: boom",
            returncode=1,
        ),
    )

    with pytest.raises(OcrUnavailable) as excinfo:
        transcribe_pages_detached(tmp_path / "doc.pdf", [1])

    assert "boom" in str(excinfo.value)


def test_transcribe_pages_detached_reports_the_childs_own_error_on_a_clean_failure(
    monkeypatch, tmp_path
):
    """``ocr_subproc`` catches its own failures, prints ``{"error": ...}`` to
    stdout and exits 1 — the ordinary failure shape, not the exceptional one.
    Reading stderr alone made every one of those raise "exited 1: " with
    nothing after the colon, and that message is what the coverage-note path
    and the gateway log both quote."""
    monkeypatch.setattr(
        "durin.memory.ocr.subprocess.run",
        _fake_run(
            stdout=json.dumps({"error": "OcrUnavailable: local OCR needs the [ocr] extra"}),
            stderr="",
            returncode=1,
        ),
    )

    with pytest.raises(OcrUnavailable) as excinfo:
        transcribe_pages_detached(tmp_path / "doc.pdf", [1])

    assert "local OCR needs the [ocr] extra" in str(excinfo.value)


def test_transcribe_pages_detached_falls_back_to_stderr_when_stdout_says_nothing(
    monkeypatch, tmp_path
):
    """A child killed before it could print its JSON (OOM, a failed import at
    module scope) leaves stdout empty and the reason on stderr. Garbage on
    stdout must not shadow it either."""
    monkeypatch.setattr(
        "durin.memory.ocr.subprocess.run",
        _fake_run(stdout="not json at all", stderr="Killed: 9", returncode=137),
    )

    with pytest.raises(OcrUnavailable) as excinfo:
        transcribe_pages_detached(tmp_path / "doc.pdf", [1])

    assert "Killed: 9" in str(excinfo.value)


def test_transcribe_pages_detached_raises_on_timeout(monkeypatch, tmp_path):
    def run(cmd, *, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout, output="", stderr="stuck loading model")

    monkeypatch.setattr("durin.memory.ocr.subprocess.run", run)

    with pytest.raises(OcrUnavailable) as excinfo:
        transcribe_pages_detached(tmp_path / "doc.pdf", [1])

    assert "stuck loading model" in str(excinfo.value)


def test_transcribe_pages_detached_raises_when_a_page_is_missing_from_the_result(
    monkeypatch, tmp_path
):
    # The child transcribing fewer pages than asked should not happen, but the
    # contract has to say something rather than merge a silent partial: the
    # whole call fails the same way a broken engine does.
    monkeypatch.setattr(
        "durin.memory.ocr.subprocess.run",
        _fake_run(stdout='{"pages": {"1": "hello"}}'),
    )

    with pytest.raises(OcrUnavailable):
        transcribe_pages_detached(tmp_path / "doc.pdf", [1, 2])


def test_transcribe_pages_detached_parse_reads_stdout_only(monkeypatch, tmp_path):
    """RapidOCR logs INFO per model load; capture_output keeps that on stderr,
    separate from stdout's JSON. Stuffing stderr with lines that are not valid
    JSON and are not even mentioned proves the parse never looks at it for the
    result — only stdout decides the outcome on the happy path."""
    noisy_stderr = "\n".join(
        f"2026-08-14 12:00:0{i} [INFO] rapidocr.main: loading detection model onnx session"
        for i in range(5)
    )
    monkeypatch.setattr(
        "durin.memory.ocr.subprocess.run",
        _fake_run(stdout='{"pages": {"1": "clean text"}}', stderr=noisy_stderr),
    )

    result = transcribe_pages_detached(tmp_path / "doc.pdf", [1])

    assert result == {1: "clean text"}


def test_transcribe_pages_detached_runs_one_child_at_a_time(monkeypatch, tmp_path):
    """Two inline transcriptions arriving together must not put two OCR
    children on the machine at once.

    Inline callers reach this through ``asyncio.to_thread``'s executor, which
    has more than one worker thread — so nothing in the event loop serializes
    them any more. The engine costs about the same per child however it is
    started, which is precisely the budget the background lane's
    ``MAX_CONCURRENT_OCR_JOBS`` is built from; that cap lives in the registry
    and never sees this path.

    The fake child records when it starts and when it finishes, so the
    assertion is about *overlap* rather than about wall-clock timing: with the
    slot held, one child's start/finish pair always closes before the other's
    opens, in whichever order the threads happen to win it.
    """
    events: list[str] = []
    events_lock = threading.Lock()
    both_started = threading.Barrier(2, timeout=10)

    def run(cmd, *, capture_output, text, timeout):
        page = cmd[-1]
        with events_lock:
            events.append(f"start {page}")
        # Sleep inside the child so a second caller that was NOT held out has
        # a real window to overlap in — without it, a serial interleave and a
        # concurrent one look identical.
        time.sleep(0.05)
        with events_lock:
            events.append(f"finish {page}")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"pages": {page: "text"}}), stderr="",
        )

    monkeypatch.setattr("durin.memory.ocr.subprocess.run", run)

    def call(page: int):
        # Both threads are inside transcribe_pages_detached's contention
        # window before either is allowed to proceed, so the test exercises a
        # genuine race rather than two calls that merely happened to be late.
        both_started.wait()
        transcribe_pages_detached(tmp_path / "doc.pdf", [page])

    threads = [threading.Thread(target=call, args=(page,)) for page in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive(), "an inline OCR caller never returned"

    assert events in (
        ["start 1", "finish 1", "start 2", "finish 2"],
        ["start 2", "finish 2", "start 1", "finish 1"],
    ), f"two OCR children overlapped: {events}"


def _rss_bytes() -> int:
    """Best-effort *current* process RSS, in bytes.

    Prefers ``psutil`` (a live reading) when installed. Falling back to
    ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` trades that away on two
    counts: it is a high-water mark rather than a live reading (it can only
    stay flat or grow, never shrink), and its unit is platform-dependent —
    kilobytes on Linux, bytes on macOS/BSD — normalized here.
    """
    try:
        import psutil

        return psutil.Process().memory_info().rss
    except ImportError:
        pass
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return maxrss if sys.platform == "darwin" else maxrss * 1024


@pytest.mark.skipif(not engine_available(), reason="[ocr] extra not installed")
def test_transcribe_pages_detached_keeps_the_engine_out_of_this_process(tmp_path):
    """The point of this task: transcribing through the real subprocess must
    not grow *this* process's memory by the engine's footprint (~1.6 GB
    resident in-process, per the audit this branch fixes). 300 MB is generous
    headroom above ordinary subprocess/JSON overhead while still being far too
    small for the in-process engine to hide under.

    What this bound proves: the engine's memory did not leak into this
    process. What it does NOT prove: anything about the child's own memory
    (deliberately unmeasured here) or an exact residency number for either
    process — a looser bound would still catch the regression this test
    exists to catch.
    """
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "two_pages.pdf"
    _write_text_pdf(pdf, ["FIRST PAGE OCR CONTENT", "SECOND PAGE OCR CONTENT"])

    before = _rss_bytes()
    result = transcribe_pages_detached(pdf, [1, 2])
    after = _rss_bytes()

    assert "FIRST" in result[1].upper()
    assert "SECOND" in result[2].upper()

    delta_mb = (after - before) / (1024 * 1024)
    assert delta_mb < 300, (
        f"parent process RSS grew {delta_mb:.1f} MB during transcription — "
        "the OCR engine leaked into this process"
    )
