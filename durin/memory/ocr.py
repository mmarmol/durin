"""Local OCR for PDF pages that carry no text layer.

Rasterise with pypdfium2, transcribe with RapidOCR on CPU. Both stay inside
this module so the rest of the codebase never imports the engine directly and
an install without the [ocr] extra fails in exactly one place.

The engine is loaded lazily and held per process, so which process calls
``transcribe_page`` decides where that memory ends up. The OCR worker
(``durin/jobs/ocr_worker.py``) calls it directly: that worker is itself a
short-lived subprocess, so its engine goes away when it exits. The gateway is
not short-lived, so inline callers never call ``transcribe_page`` in this
process — they call ``transcribe_pages_detached``, which runs the engine in
its own short-lived child (``durin/memory/ocr_subproc.py``) and hands back
only the transcription results, keeping the engine's memory out of the
gateway either way.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from durin.config.home import durin_home

__all__ = [
    "OcrUnavailable",
    "TranscribedPage",
    "engine_available",
    "render_page",
    "score_str",
    "transcribe_page",
    "transcribe_pages_detached",
]

logger = logging.getLogger(__name__)

# 200 dpi is the usual floor for reliable OCR of body text; below it small
# type starts dropping characters, above it the cost grows with no gain.
_DEFAULT_DPI = 200

# Headroom added to the subprocess timeout when the selected language's
# recognition model is not on disk yet: the one-time download rides ahead of
# the first page, and the ordinary formula budgets nothing for the network.
# Sized for the worst first run — the ~8 MiB recognition model plus ~10.5 MiB
# of shared detection/classification models when no language was ever added
# before — on a ~0.5 Mbit/s line.
_MODEL_DOWNLOAD_TIMEOUT_S = 300

# The engine and the language it was built for, cached per process. The
# language is part of the cache key: rapidocr swaps the recognition model by
# construction parameter, so an engine built for one language served to a
# request for another would transcribe every page as plausible garbage with
# nothing flagging it.
_engine = None
_engine_language: str | None = None

# Measured (durin v0.6.0 audit): one OCR engine costs ~1.4 GB resident and
# ~4.8 cores for as long as it runs, wherever it runs -- the same budget the
# background lane's cap is built from (~2.1 GB peak over a whole book). Two or
# three of them together saturate a laptop and OOM small servers. That cap is
# enforced in the job registry and never sees this path, so inline
# transcription admits one child at a time here: callers reach this from
# ``asyncio.to_thread``'s executor, whose several worker threads would
# otherwise let N scanned attachments spawn N children at once. A threading
# semaphore, not an asyncio one, because that executor is what contends. No
# config knob, for the same reason the job cap has none: raising it changes
# the resource budget the number is built from, which is a code change, not a
# per-install preference.
_INLINE_OCR_SLOT = threading.Semaphore(1)


class OcrUnavailable(RuntimeError):
    """Raised when OCR is requested but the [ocr] extra is not installed."""


@dataclass(frozen=True)
class TranscribedPage:
    """One page's transcription, with the engine's own account of it.

    ``mean_score`` and ``min_score`` aggregate the engine's per-line
    recognition scores; both are None when no line survived recognition.
    ``det_boxes`` is None for a page that produced text — the question never
    came up — and an int only when the page came back empty: a detection-only
    second pass then counts regions of printed text, so 0 means genuinely
    blank paper while more means the page holds print this engine cannot
    read (a script outside its models, typically).
    """

    text: str
    mean_score: float | None
    min_score: float | None
    det_boxes: int | None


def engine_available() -> bool:
    """Whether the OCR engine can be imported in this interpreter."""
    try:
        import rapidocr  # noqa: F401

        return True
    except ImportError:
        return False


def score_str(score: float | None) -> str:
    """A recognition score for a log line: three decimals, or ``n/a``.

    None is representable end to end — the child's JSON allows any None mix
    and ``transcribe_page``'s own ``scores ... or ()`` defense builds
    score-less pages — so a format site that assumes a float turns a missing
    number into a TypeError. Formatting through this keeps an absent score a
    logging detail, never a page failure.
    """
    return "n/a" if score is None else f"{score:.3f}"


def _model_root() -> Path:
    """Where on-demand language models live: ``<durin home>/models/ocr``.

    The durin home, not rapidocr's own default (a ``models`` dir inside its
    site-packages install), because every reinstall or redeploy replaces
    site-packages — models cached there would be re-downloaded after each
    one. The stt and tts engines cache under the same ``models/`` parent for
    the same reason.
    """
    return durin_home() / "models" / "ocr"


def _language_model_present(language: str) -> bool:
    """Whether *language*'s one-time model download already happened.

    RapidOCR stores every model flat under the root, named after its
    download URL's basename (observed with rapidocr 3.9.2): the language's
    recognition model is ``<code>_PP-OCRv5_rec_mobile.onnx``, and the first
    added language drops the shared detection/classification models beside
    it. The recognition model is the per-language marker; when it is absent,
    a download is coming, whatever else the root holds.
    """
    return (_model_root() / f"{language}_PP-OCRv5_rec_mobile.onnx").is_file()


def _get_engine(language: str | None = None):
    """The process-cached engine for *language* (None = the built-in pack).

    A request for a different language than the cached engine's rebuilds it
    — same lifetime rules as before, the language is simply part of what
    identifies the engine.
    """
    global _engine, _engine_language
    if _engine is None or _engine_language != language:
        if not engine_available():
            raise OcrUnavailable(
                "local OCR needs the [ocr] extra: install it via the "
                "Settings > Documents toggle, or manually with `pipx inject "
                'durin-agent "durin-agent[ocr]"` / `uv tool install '
                '"durin-agent[ocr]"`'
            )
        from rapidocr import RapidOCR

        if language is None:
            # Exactly the construction the default path has always used: the
            # models bundled in the wheel, resolved inside it, fully offline.
            engine = RapidOCR()
        else:
            from rapidocr import ModelType, OCRVersion

            # PP-OCRv5 mobile is the line that publishes per-script
            # recognition models; only the recognizer is swapped, detection
            # and classification stay the defaults. Overriding the model
            # root moves every download (and lookup) to durin's own model
            # dir; rapidocr fetches whatever is missing there on first use
            # and reuses it afterwards.
            engine = RapidOCR(
                params={
                    "Rec.lang_type": language,
                    "Rec.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.model_type": ModelType.MOBILE,
                    "Global.model_root_dir": str(_model_root()),
                }
            )
        _engine, _engine_language = engine, language
    return _engine


def render_page(pdf_path: Path, page: int, *, dpi: int = _DEFAULT_DPI) -> bytes:
    """Render one 1-based PDF page to PNG bytes."""
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        if page < 1 or page > len(doc):
            raise ValueError(
                f"page {page} out of range for {pdf_path.name} ({len(doc)} pages)"
            )
        # pypdfium2 renders at a scale relative to 72 dpi.
        bitmap = doc[page - 1].render(scale=dpi / 72)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        return buf.getvalue()
    finally:
        doc.close()


def transcribe_page(
    pdf_path: Path,
    page: int,
    *,
    dpi: int = _DEFAULT_DPI,
    language: str | None = None,
) -> TranscribedPage:
    """Transcribe one 1-based PDF page, top to bottom.

    Empty ``text`` is a legitimate outcome, not a failure; the result's
    ``det_boxes`` is what says whether that emptiness is blank paper or
    printed text the engine cannot read. *language* selects a recognition
    model beyond the built-in pack (None); callers pass the already-validated
    ``documents.ocr.language`` value.
    """
    engine = _get_engine(language)
    image = render_page(pdf_path, page, dpi=dpi)
    # Every stage flag explicit on every call: rapidocr writes any non-None
    # flag back onto the engine instance, so the det-only pass below would
    # otherwise leave recognition switched off for every page transcribed
    # after it through this process-cached engine.
    result = engine(image, use_det=True, use_cls=True, use_rec=True)
    lines = getattr(result, "txts", None)
    if not lines:
        # Recognition read nothing. Detection alone, on the same rendered
        # image, is what tells blank paper (0 boxes) apart from print in a
        # script the models cannot read (some boxes, all of whose
        # recognitions were erased by the engine's own score filter).
        det = engine(image, use_det=True, use_cls=False, use_rec=False)
        boxes = getattr(det, "boxes", None)
        return TranscribedPage(
            text="",
            mean_score=None,
            min_score=None,
            det_boxes=0 if boxes is None else len(boxes),
        )
    scores = [float(score) for score in getattr(result, "scores", None) or ()]
    # The scores travel for logging and diagnosis only, never as an
    # accept/reject gate: measured on this engine, wrong-but-plausible
    # readings score 0.956-0.987 — inside the band of legitimate noisy
    # scans — so no threshold separates bad output from good.
    return TranscribedPage(
        text="\n".join(str(line) for line in lines).strip(),
        mean_score=sum(scores) / len(scores) if scores else None,
        min_score=min(scores) if scores else None,
        det_boxes=None,
    )


def _child_failure(stdout: str | None, stderr: str | None) -> str:
    """Why the OCR child exited non-zero, in its own words where it has any.

    The child catches its own failures, prints ``{"error": "<message>"}`` to
    stdout and exits 1 — that is the ordinary failure, and stderr is empty for
    it. Only a death it never got to report (a module-scope import error, an
    OOM kill) leaves the reason on stderr instead. Reading stdout first is
    what makes the ordinary case say anything at all; anything else on stdout
    is treated as no answer and falls through to stderr's tail, so a truncated
    or garbled line never shadows the real reason.
    """
    try:
        message = json.loads(stdout or "").get("error")
    except Exception:  # noqa: BLE001 — no usable JSON: the child never printed one
        message = None
    return str(message) if message else (stderr or "")[-500:]


def transcribe_pages_detached(
    pdf_path: Path,
    pages: Sequence[int],
    *,
    dpi: int = _DEFAULT_DPI,
    timeout_s: float | None = None,
    language: str | None = None,
) -> dict[int, TranscribedPage]:
    """Transcribe *pages* of *pdf_path* in a short-lived child process.

    Spawns ``python -m durin.memory.ocr_subproc`` and waits for it
    synchronously — one process per call, never pooled or cached, so the
    engine's memory is released the moment the child exits rather than
    living in the caller. The child renders its own pages from *pdf_path*;
    only the page numbers cross the process boundary, not image data.

    Raises :class:`OcrUnavailable` — the same exception a broken in-process
    engine raises — on a non-zero exit, a timeout, an unparseable result, or
    a result missing one of the requested pages. Every one of those is a
    failure of the whole call: there is no partial result to salvage, so
    existing callers degrade exactly as they already do for a missing
    engine, via the ``except (OcrUnavailable, ImportError)`` they already
    have. That reuse is deliberate — this adds no new exception type.

    Holds ``_INLINE_OCR_SLOT`` for the whole call, so concurrent callers
    queue instead of putting several engines on the machine at once.
    """
    with _INLINE_OCR_SLOT:
        # Selecting the language in config was the consent for this; the log
        # line is the disclosure — the source by name, and what of the user's
        # is (not) in the transfer. Checked before spawning because only the
        # parent knows to stretch the timeout for it.
        downloading = language is not None and not _language_model_present(language)
        if downloading:
            logger.info(
                "first use of OCR language %r: rapidocr downloads its "
                "recognition model (~8 MiB, plus ~11 MiB of shared detection "
                "models if none was ever added) from modelscope.cn into %s, "
                "once; document content is not uploaded",
                language, _model_root(),
            )

        if timeout_s is None:
            # 60s covers interpreter startup plus RapidOCR's model load; 10s
            # per page covers rendering and inference on CPU with slack for a
            # slow host. Measured from the child's own start, not from this
            # call's: waiting for the slot above spends none of it. Not
            # configurable: a hung child should fail loudly into the
            # coverage-note path, not wait on a knob nobody will tune
            # correctly. A pending model download is the one addition: it
            # happens inside the child, ahead of its first page.
            timeout_s = 60 + 10 * len(pages)
            if downloading:
                timeout_s += _MODEL_DOWNLOAD_TIMEOUT_S

        cmd = [
            sys.executable, "-m", "durin.memory.ocr_subproc",
            str(pdf_path), str(dpi), *(str(page) for page in pages),
        ]
        if language is not None:
            cmd += ["--lang", language]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            tail = (exc.stderr or "")[-500:]
            raise OcrUnavailable(
                f"OCR subprocess timed out after {timeout_s:.0f}s: {tail}"
            ) from exc

        if proc.returncode != 0:
            raise OcrUnavailable(
                f"OCR subprocess exited {proc.returncode}: "
                f"{_child_failure(proc.stdout, proc.stderr)}"
            )

        try:
            payload = json.loads(proc.stdout)
            result = {
                int(page): TranscribedPage(
                    text=str(obj["text"]),
                    mean_score=None if obj["mean_score"] is None else float(obj["mean_score"]),
                    min_score=None if obj["min_score"] is None else float(obj["min_score"]),
                    det_boxes=None if obj["det_boxes"] is None else int(obj["det_boxes"]),
                )
                for page, obj in payload["pages"].items()
            }
        except Exception as exc:  # noqa: BLE001 — any parse-shape surprise is OcrUnavailable too
            tail = (proc.stderr or "")[-500:]
            raise OcrUnavailable(
                f"OCR subprocess produced no parseable result: {tail}"
            ) from exc

        missing = [page for page in pages if page not in result]
        if missing:
            raise OcrUnavailable(
                f"OCR subprocess did not return page(s) {missing} of the "
                f"{len(pages)} requested"
            )
        # The child's stderr is captured and discarded on success, so this
        # summary is the only place its scores can reach a log at all. Each
        # aggregate is guarded on its own inputs: a page can carry any mix
        # of None scores, so "some mins exist" says nothing about the means
        # — one gate for both divided by zero on exactly that mix.
        mean_scores = [p.mean_score for p in result.values() if p.mean_score is not None]
        min_scores = [p.min_score for p in result.values() if p.min_score is not None]
        if mean_scores or min_scores:
            logger.info(
                "OCR subprocess transcribed %d page(s) of %s: mean score %s, min %s",
                len(result), pdf_path.name,
                score_str(sum(mean_scores) / len(mean_scores) if mean_scores else None),
                score_str(min(min_scores) if min_scores else None),
            )
        elif any(p.text for p in result.values()):
            # Text without a single score anywhere: an engine that does not
            # report them. "No text recognized" would be a lie here — the
            # text is the part that arrived fine.
            logger.info(
                "OCR subprocess transcribed %d page(s) of %s: "
                "no recognition scores reported",
                len(result), pdf_path.name,
            )
        else:
            logger.info(
                "OCR subprocess transcribed %d page(s) of %s: no text recognized on any",
                len(result), pdf_path.name,
            )
        return result
