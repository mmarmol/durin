import asyncio

from durin.agent.tools.context import RequestContext
from durin.agent.tools.memory_ingest import MemoryIngestTool


def test_ingest_result_emits_id_and_reference_before_content(tmp_path):
    # C1: `id` + `reference` must precede `content` so they survive the 16 KB
    # head-truncation of tool results on large documents.
    doc = tmp_path / "rabies.md"
    doc.write_text("# Rabies\n\nA viral disease.\n", encoding="utf-8")

    tool = MemoryIngestTool(workspace=str(tmp_path))
    out = asyncio.run(tool.execute(path=str(doc)))

    assert "error" not in out
    assert "reference" in out and out["reference"].startswith("reference:")
    keys = list(out.keys())
    assert keys.index("id") < keys.index("content")
    assert keys.index("reference") < keys.index("content")


def _fake_ingest_result(tmp_path, source, captured):
    def _fake(workspace, src, **kwargs):
        captured.update(kwargs)
        return {
            "id": "x", "source": str(src), "meta_path": str(tmp_path / "meta.json"),
            "size_bytes": 1, "content": "some text", "job_id": None,
        }
    return _fake


def test_execute_threads_the_session_key_from_context_into_ingest(tmp_path, monkeypatch):
    """A session-scoped ingest's background OCR job must be tagged with that
    session -- otherwise the job's session_key stays NULL in the registry and
    it never appears in anyone's tasks tray (JobRegistry.list_for_session
    filters with `WHERE session_key = ?`, which SQL never matches NULL)."""
    doc = tmp_path / "rabies.md"
    doc.write_text("# Rabies\n\nA viral disease.\n", encoding="utf-8")
    captured: dict = {}
    monkeypatch.setattr(
        "durin.agent.tools.memory_ingest.ingest_artifact",
        _fake_ingest_result(tmp_path, doc, captured),
    )

    tool = MemoryIngestTool(workspace=str(tmp_path))
    tool.set_context(RequestContext(channel="websocket", chat_id="c1"))
    asyncio.run(tool.execute(path=str(doc)))

    assert captured.get("session_key") == "websocket:c1"


def test_execute_prefers_the_explicit_session_key_over_channel_and_chat_id(tmp_path, monkeypatch):
    doc = tmp_path / "rabies.md"
    doc.write_text("# Rabies\n\nA viral disease.\n", encoding="utf-8")
    captured: dict = {}
    monkeypatch.setattr(
        "durin.agent.tools.memory_ingest.ingest_artifact",
        _fake_ingest_result(tmp_path, doc, captured),
    )

    tool = MemoryIngestTool(workspace=str(tmp_path))
    tool.set_context(RequestContext(channel="websocket", chat_id="c1", session_key="unified:main"))
    asyncio.run(tool.execute(path=str(doc)))

    assert captured.get("session_key") == "unified:main"


def test_execute_passes_no_session_key_without_a_request_context(tmp_path, monkeypatch):
    # No set_context call at all -- the tool must still work (session_key=None
    # is exactly what ingest_artifact already defaults to), not raise.
    doc = tmp_path / "rabies.md"
    doc.write_text("# Rabies\n\nA viral disease.\n", encoding="utf-8")
    captured: dict = {}
    monkeypatch.setattr(
        "durin.agent.tools.memory_ingest.ingest_artifact",
        _fake_ingest_result(tmp_path, doc, captured),
    )

    tool = MemoryIngestTool(workspace=str(tmp_path))
    asyncio.run(tool.execute(path=str(doc)))

    assert captured.get("session_key") is None


# ---------------------------------------------------------------------------
# A scanned document whose OCR is deferred to a background job
# ---------------------------------------------------------------------------


def _defer_ocr_to_a_job(tmp_path, monkeypatch):
    """Set up the deferred-OCR path end to end, with no real OCR engine, no
    embedder and no worker process: OCR on with a 5-page inline budget, the job
    registry redirected into tmp, and the worker launch stubbed out so the
    test drives ``run_job`` itself. Returns the job registry.

    The engine is *reported* as installed (it is not, in CI): an install
    without the [ocr] extra transcribes nothing and enqueues nothing, so a
    test about the deferred path has to say one exists. What it would
    transcribe is stubbed per test.

    ``memory.enabled`` is off so nothing tries to load an embedding model:
    that pins these tests to the lexical half of search, which is the half CI
    can run (no fastembed / lancedb there).
    """
    from durin.config.schema import Config
    from durin.jobs.registry import JobRegistry

    cfg = Config()
    cfg.documents.ocr.enabled = True
    cfg.documents.ocr.inline_max_pages = 5
    cfg.memory.enabled = False
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("durin.memory.doc_convert.engine_available", lambda: True)

    db = tmp_path / "jobs" / "jobs.db"
    monkeypatch.setattr("durin.config.paths.jobs_db_path", lambda: db)
    # Replace the `subprocess` name inside durin.jobs.spawn rather than
    # `subprocess.Popen` itself: the latter is the real, shared module
    # attribute, and stubbing it out globally breaks any module imported
    # afterwards that subscripts Popen in a type annotation.
    monkeypatch.setattr("durin.jobs.spawn.subprocess", _NoLaunch)
    return JobRegistry(db)


class _NoLaunch:
    """Stands in for the ``subprocess`` module inside ``durin.jobs.spawn``."""

    @staticmethod
    def Popen(*args, **kwargs):  # noqa: N802 - mirrors the name it replaces
        return None


def _scanned_book(tmp_path, pages: int = 8):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "zorpbook.pdf"
    _write_text_pdf(pdf, [""] * pages)          # no text layer: every page needs OCR
    return pdf


def test_a_scanned_book_becomes_searchable_once_its_transcription_lands(
    tmp_path, monkeypatch,
):
    """The whole promise of the feature, asserted as the user experiences it:
    hand durin a scanned book, let the background transcription finish, and a
    search for what the book says returns it.

    The search before the worker runs is not decoration -- without it this
    test would still pass if the search happened to match on the filename, or
    if the assertion were vacuous."""
    registry = _defer_ocr_to_a_job(tmp_path, monkeypatch)
    book = _scanned_book(tmp_path)
    ws = tmp_path / "ws"
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"Page {page}: the zorptastic protocol governs it all.",
    )

    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.jobs.ocr_worker import run_job

    out = asyncio.run(MemoryIngestTool(workspace=str(ws)).execute(path=str(book)))
    assert "error" not in out
    assert out["job_id"]

    search = MemorySearchTool(workspace=ws)     # no embedding model -> FTS + grep
    before = asyncio.run(search.execute(query="zorptastic", scope="library"))
    assert "zorptastic" not in before["sectioned_rendered"]

    run_job(out["job_id"], registry=registry)
    assert registry.get(out["job_id"]).status == "done"

    after = asyncio.run(search.execute(query="zorptastic", scope="library"))
    assert "zorptastic" in after["sectioned_rendered"]


def test_the_result_says_the_text_is_still_being_transcribed(tmp_path, monkeypatch):
    """The agent must not read this result as a finished ingest. There is no
    text and no reference to cite yet -- what there is is a job, its size, and
    a note saying what to tell the user."""
    _defer_ocr_to_a_job(tmp_path, monkeypatch)
    book = _scanned_book(tmp_path, pages=8)

    out = asyncio.run(
        MemoryIngestTool(workspace=str(tmp_path / "ws")).execute(path=str(book))
    )

    assert out["job_id"]
    assert out["pages_pending"] == 8
    assert "transcrib" in out["note"].lower()
    assert out["content"] == ""
    assert "reference" not in out          # nothing to cite until the text lands
