"""Ingesting a scanned book enqueues a job instead of blocking."""

import pytest

from durin.config.schema import DocumentsConfig
from durin.jobs.registry import JobRegistry
from durin.memory.ingestion import ingest_artifact


@pytest.fixture(autouse=True)
def _engine_present(monkeypatch):
    """Stand in for an installed OCR engine.

    The [ocr] extra is absent in CI, and an install without it transcribes
    nothing and enqueues nothing — so these tests, which are about the job
    hand-off rather than the engine, have to say an engine exists. The test
    that is *about* a missing engine overrides this.
    """
    monkeypatch.setattr("durin.memory.doc_convert.engine_available", lambda: True)


@pytest.fixture()
def registry(tmp_path):
    return JobRegistry(tmp_path / "jobs.db")


@pytest.fixture()
def book(tmp_path):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "book.pdf"
    _write_text_pdf(pdf, [""] * 40)
    return pdf


def test_ingesting_a_scanned_book_returns_immediately_with_a_job(tmp_path, book, registry):
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    result = ingest_artifact(
        tmp_path / "ws", book, documents_config=cfg, jobs=registry, session_key="chat:1"
    )

    assert result["job_id"] is not None
    job = registry.get(result["job_id"])
    assert job.kind == "ocr"
    assert job.units_total == 40
    assert job.status == "queued"


def test_the_original_is_stored_even_though_the_text_is_pending(tmp_path, book, registry):
    from pathlib import Path

    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})
    result = ingest_artifact(
        tmp_path / "ws", book, documents_config=cfg, jobs=registry, session_key="chat:1"
    )
    assert Path(result["source"]).exists()


def test_a_session_scoped_job_appears_in_that_sessions_tray(tmp_path, book, registry):
    """The chain this task's findings depend on, proven rather than assumed:
    a job carrying a session key must actually come back out of
    collect_tasks for that exact session. If session_key never reached the
    enqueued row, or collect_tasks never filtered by it, this fails --
    passing here is what a correctly wired tasks tray requires."""
    from durin.agent.background_tasks import collect_tasks

    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})
    ingest_artifact(
        tmp_path / "ws", book, documents_config=cfg, jobs=registry, session_key="chat:1"
    )

    tasks = collect_tasks(tmp_path / "ws", jobs=registry, session_key="chat:1")

    assert [t["kind"] for t in tasks] == ["job"]
    # The label is the normalized ingested-entry filename ("source.<ext>"),
    # not the original name -- ingest_artifact copies to entry_dir/source.pdf
    # before spawn_ocr_job ever sees a path, and its label is the path's name.
    assert tasks[0]["label"] == "source.pdf"
    assert tasks[0]["units_total"] == 40
    # A different session must not see it -- the filter has to actually filter.
    assert collect_tasks(tmp_path / "ws", jobs=registry, session_key="chat:other") == []


def test_ingest_survives_ocr_enabled_without_the_extra(tmp_path, book, registry, monkeypatch):
    """OcrUnavailable is a RuntimeError: neither IngestError nor OSError, so an
    ingest that let it escape would raise straight through every caller. It has
    to be handled where the conversion happens, not left to each caller to
    catch a type none of them mention."""
    monkeypatch.setattr("durin.memory.doc_convert.engine_available", lambda: False)
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    result = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    assert result["job_id"] is None
    assert "[ocr]" in result["content"]


def test_a_normal_document_ingests_with_no_job(tmp_path, registry):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "plain.pdf"
    _write_text_pdf(pdf, ["Plenty of readable body text on this page"])
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True}})

    result = ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg, jobs=registry)

    assert result["job_id"] is None
    assert "readable body text" in result["content"]


def test_the_result_carries_how_many_pages_are_pending(tmp_path, book, registry):
    """What the agent needs to tell the user how big the wait is."""
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    result = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    assert result["job_pages"] == 40


def test_the_entry_is_complete_before_its_job_is_spawned(tmp_path, book, registry, monkeypatch):
    """The worker learns which document it is transcribing from the entry's
    meta.json, and it can be reading it the moment spawn_ocr_job returns. So
    the entry has to be finished before that call, not after it."""
    seen = {}

    def _probe(*, registry, pdf_path, pages, session_key, sidecar_dir=None):
        from pathlib import Path

        seen["meta"] = (Path(sidecar_dir) / "meta.json").is_file()
        return registry.enqueue(
            kind="ocr", label=pdf_path.name, payload={}, session_key=session_key,
            units_total=len(pages),
        )

    monkeypatch.setattr("durin.jobs.spawn.spawn_ocr_job", _probe)
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    assert seen["meta"] is True


class _RecordingVectorIndex:
    def __init__(self):
        self.upserts = []

    def upsert_reference_chunk(self, *, ref, idx, text, path, breadcrumb=""):
        self.upserts.append(idx)


def test_indexing_an_entry_embeds_its_chunks_with_the_configured_model(tmp_path, monkeypatch):
    """The vector half only ever runs in the product -- CI has neither
    fastembed nor lancedb -- so the wiring that builds the index from the
    active config is what this covers, with both stood in for."""
    import json

    from durin.config.schema import Config
    from durin.memory.ingestion import index_ingested_entry

    entry_dir = tmp_path / "ws" / "ingested" / "e1"
    entry_dir.mkdir(parents=True)
    (entry_dir / "source.md").write_text("# Book\n\nthe zorptastic protocol\n", encoding="utf-8")
    (entry_dir / "meta.json").write_text(
        json.dumps({"derived": {"source_path": "/somewhere/zorpbook.pdf"}}), encoding="utf-8")

    cfg = Config()
    cfg.memory.enabled = True
    vi = _RecordingVectorIndex()
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("durin.memory.vector_index.vector_index_available", lambda: True)
    monkeypatch.setattr(
        "durin.memory.embedding.provider_from_config",
        lambda cfg, model=None: ("provider", model))
    monkeypatch.setattr(
        "durin.memory.vector_index.VectorIndex", lambda workspace, provider: vi)

    ref = index_ingested_entry(entry_dir)

    assert ref == "reference:zorpbook"
    assert vi.upserts == [0]


def test_indexing_an_entry_skips_the_vector_half_when_memory_is_off(tmp_path, monkeypatch):
    import json

    from durin.config.schema import Config
    from durin.memory.ingestion import index_ingested_entry

    entry_dir = tmp_path / "ws" / "ingested" / "e1"
    entry_dir.mkdir(parents=True)
    (entry_dir / "source.md").write_text("# Book\n\nthe zorptastic protocol\n", encoding="utf-8")
    (entry_dir / "meta.json").write_text(
        json.dumps({"derived": {"source_path": "/somewhere/zorpbook.pdf"}}), encoding="utf-8")

    cfg = Config()
    cfg.memory.enabled = False
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)

    def _never(*a, **kw):
        raise AssertionError("built a vector index with memory disabled")

    monkeypatch.setattr("durin.memory.vector_index.VectorIndex", _never)

    assert index_ingested_entry(entry_dir) == "reference:zorpbook"
