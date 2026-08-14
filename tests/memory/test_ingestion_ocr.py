"""Ingesting a scanned book enqueues a job instead of blocking."""

import pytest

from durin.config.schema import DocumentsConfig
from durin.jobs.registry import JobRegistry
from durin.memory.ingestion import ingest_artifact


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


def test_a_normal_document_ingests_with_no_job(tmp_path, registry):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "plain.pdf"
    _write_text_pdf(pdf, ["Plenty of readable body text on this page"])
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True}})

    result = ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg, jobs=registry)

    assert result["job_id"] is None
    assert "readable body text" in result["content"]
