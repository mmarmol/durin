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


def test_a_normal_document_ingests_with_no_job(tmp_path, registry):
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "plain.pdf"
    _write_text_pdf(pdf, ["Plenty of readable body text on this page"])
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True}})

    result = ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg, jobs=registry)

    assert result["job_id"] is None
    assert "readable body text" in result["content"]
