"""Ingesting a scanned book enqueues a job instead of blocking."""

import pytest

from durin.config.schema import DocumentsConfig
from durin.jobs.registry import JobRegistry
from durin.memory.ingestion import IngestError, ingest_artifact


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
    # The name the user handed over, not the normalized copy the entry holds:
    # ingest_artifact copies to entry_dir/source.pdf before spawn_ocr_job sees
    # a path, so every job in the tray would otherwise read "source.pdf".
    assert tasks[0]["label"] == "book.pdf"
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


def test_ingest_raises_when_ocr_transcribes_every_flagged_page_blank(
    tmp_path, registry, monkeypatch
):
    """convert_file_to_markdown's empty-after-OCR guard must reach this
    caller as IngestError -- the same taxonomy ingest_artifact already
    converts every other DocConvertError into (see the except clause right
    below the convert_file_to_markdown call in ingest_artifact)."""
    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "blank.pdf"
    _write_text_pdf(pdf, ["", ""])
    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_pages_detached",
        lambda path, pages: {p: "" for p in pages},
    )
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    with pytest.raises(IngestError, match="even after OCR"):
        ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg, jobs=registry)


def test_the_result_carries_how_many_pages_are_pending(tmp_path, book, registry):
    """What the agent needs to tell the user how big the wait is."""
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    result = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    assert result["job_pages"] == 40


def test_the_job_is_sized_for_the_whole_book_not_the_pages_it_starts_from(
    tmp_path, book, registry,
):
    """Deciding a forty-page scan is over budget only takes confirming six
    pages, so the payload the job starts from is six pages long. What the user
    is told is waiting must still be the book — the worker widens the payload
    to the whole of it before transcribing anything."""
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    result = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    job = registry.get(result["job_id"])
    assert job.payload["pages"] == [1, 2, 3, 4, 5, 6]
    assert job.units_total == 40


def test_the_entry_is_complete_before_its_job_is_spawned(tmp_path, book, registry, monkeypatch):
    """The worker learns which document it is transcribing from the entry's
    meta.json, and it can be reading it the moment spawn_ocr_job returns. So
    the entry has to be finished before that call, not after it."""
    seen = {}

    def _probe(
        *, registry, pdf_path, pages, session_key, label, sidecar_dir=None,
        units_total=None,
    ):
        from pathlib import Path

        seen["meta"] = (Path(sidecar_dir) / "meta.json").is_file()
        return registry.enqueue(
            kind="ocr", label=label, payload={}, session_key=session_key,
            units_total=len(pages),
        )

    monkeypatch.setattr("durin.jobs.spawn.spawn_ocr_job", _probe)
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    assert seen["meta"] is True


class _CountingPopen:
    """Stands in for the ``subprocess`` module inside ``durin.jobs.spawn``, so
    a test can assert how many OCR workers were actually launched instead of
    only observing job rows. A fresh instance per test avoids any counter
    leaking between them (see ``_NoLaunch`` in test_memory_ingest.py for the
    sibling convention this mirrors)."""

    def __init__(self):
        self.calls = 0

    def Popen(self, *args, **kwargs):
        self.calls += 1
        return None


def test_a_pending_re_ingest_returns_the_same_job_not_a_second_one(
    tmp_path, book, registry, monkeypatch
):
    """Reproduced pre-fix: two ingest_artifact calls for the same still-queued
    book each converted independently and each spawned, leaving two queued
    rows for one document. Re-ingesting while pending must instead return the
    SAME job untouched."""
    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    first = ingest_artifact(
        tmp_path / "ws", book, documents_config=cfg, jobs=registry, session_key="chat:1"
    )
    second = ingest_artifact(
        tmp_path / "ws", book, documents_config=cfg, jobs=registry, session_key="chat:1"
    )

    assert second["job_id"] == first["job_id"]
    assert second["job_pages"] == 40
    assert second["content"] == ""
    assert launcher.calls == 1
    assert len(registry.list_for_session("chat:1")) == 1


def test_re_ingest_after_done_is_a_true_no_op(tmp_path, book, registry, monkeypatch):
    """Reproduced pre-fix: a third ingest_artifact call for a document whose
    OCR job had already finished re-ran the whole conversion and spawned a
    THIRD job, even though the finished source.md was already on disk.
    Re-ingesting after done must skip conversion entirely."""
    from durin.config.schema import Config
    from durin.jobs.ocr_worker import run_job

    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    monkeypatch.setattr(
        "durin.jobs.ocr_worker.transcribe_page",
        lambda path, page, **kw: f"Page {page}: settled text.",
    )
    # memory.enabled=False keeps this test off the vector/embedding half --
    # index_ingested_entry (called by run_job) still runs its lexical half.
    mem_cfg = Config()
    mem_cfg.memory.enabled = False
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: mem_cfg)
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    first = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)
    run_job(first["job_id"], registry=registry)
    assert registry.get(first["job_id"]).status == "done"

    def _must_not_convert(*a, **kw):
        raise AssertionError("convert_file_to_markdown must not run for a finished entry")

    monkeypatch.setattr("durin.memory.doc_convert.convert_file_to_markdown", _must_not_convert)

    second = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    assert second["job_id"] is None
    assert "settled text" in second["content"]
    assert launcher.calls == 1  # no second job spawned


def test_re_ingest_after_a_failed_job_spawns_a_fresh_one(
    tmp_path, book, registry, monkeypatch
):
    """A failed job is not a pending one: re-ingesting must retry -- a NEW
    job spawned, marker overwritten to name it, the old failed row left
    exactly as it was."""
    import json
    from pathlib import Path

    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    first = ingest_artifact(
        tmp_path / "ws", book, documents_config=cfg, jobs=registry, session_key="chat:1"
    )
    old_job_id = first["job_id"]
    assert registry.claim(old_job_id, pid=4242)
    assert registry.finish(old_job_id, pid=4242, error="boom")

    second = ingest_artifact(
        tmp_path / "ws", book, documents_config=cfg, jobs=registry, session_key="chat:1"
    )

    assert second["job_id"] is not None
    assert second["job_id"] != old_job_id
    old_job = registry.get(old_job_id)
    assert old_job.status == "failed"
    assert old_job.error == "boom"
    marker = json.loads((Path(second["source"]).parent / "ocr_job.json").read_text())
    assert marker["job_id"] == second["job_id"]
    assert launcher.calls == 2
    assert len(registry.list_for_session("chat:1")) == 2


def test_a_marker_naming_a_vanished_job_behaves_as_a_retry(
    tmp_path, book, registry, monkeypatch
):
    """A marker can outlive the row it names (a pruned terminal job); this
    must read as a retry, not a crash."""
    import json
    from pathlib import Path

    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 5}})

    first = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)
    marker_path = Path(first["source"]).parent / "ocr_job.json"
    marker_path.write_text(json.dumps({"job_id": "vanished-000"}), encoding="utf-8")

    second = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    assert second["job_id"] is not None
    assert second["job_id"] != "vanished-000"
    assert registry.get(second["job_id"]) is not None
    assert launcher.calls == 2


def test_an_ordinary_markdown_document_re_ingest_also_short_circuits(tmp_path, registry):
    """The no-op promise finally holds for the common, non-OCR case too: a
    second ingest of an unchanged markdown file must not rewrite meta.json --
    the old code rewrote it (fresh ingested_at) on every call regardless of
    whether anything had changed."""
    from pathlib import Path

    doc = tmp_path / "notes.md"
    doc.write_text("# Notes\n\nFirst version.\n", encoding="utf-8")

    first = ingest_artifact(tmp_path / "ws", doc, jobs=registry)
    assert first["content"] == "# Notes\n\nFirst version.\n"
    assert first["job_id"] is None
    meta_before = Path(first["meta_path"]).read_text(encoding="utf-8")

    second = ingest_artifact(tmp_path / "ws", doc, jobs=registry)

    assert second["id"] == first["id"]
    assert second["content"] == first["content"]
    assert second["job_id"] is None
    assert Path(second["meta_path"]).read_text(encoding="utf-8") == meta_before


def test_a_re_ingest_upgrades_an_ocr_off_stub_once_ocr_is_enabled(
    tmp_path, book, registry, monkeypatch
):
    """The coverage note left when OCR is off must not freeze the entry
    forever: once OCR is turned on, the next re-ingest re-checks instead of
    replaying the stale note -- and the upgrade has to land on disk, not
    just in this call's return value, or the entry is still stuck the turn
    after that."""
    import json
    from pathlib import Path

    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    cfg_off = DocumentsConfig.model_validate(
        {"ocr": {"enabled": False, "inline_max_pages": 50}}
    )

    first = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_off, jobs=registry)
    assert first["job_id"] is None
    assert "turned off" in first["content"]
    entry_dir = Path(first["source"]).parent
    meta_before = json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta_before["derived"]["ocr_stub"] is True

    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_pages_detached",
        lambda path, pages: {p: f"Page {p}: fresh text." for p in pages},
    )
    cfg_on = DocumentsConfig.model_validate(
        {"ocr": {"enabled": True, "inline_max_pages": 50}}
    )

    second = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_on, jobs=registry)

    assert second["job_id"] is None
    assert "fresh text" in second["content"]
    assert "turned off" not in second["content"]
    # Persisted, not just returned in this call:
    assert "fresh text" in (entry_dir / "source.md").read_text(encoding="utf-8")
    meta_after = json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta_after["derived"]["ocr_stub"] is False
    assert launcher.calls == 0


def test_a_stub_upgrade_that_is_now_over_budget_spawns_a_job(
    tmp_path, book, registry, monkeypatch
):
    """Same upgrade, but this time OCR being enabled reveals the book is
    over the inline budget: the upgrade check must fall through to the
    ordinary over-budget spawn path, not get stuck on the stub because a
    job wasn't spawned the first time either."""
    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    cfg_off = DocumentsConfig.model_validate(
        {"ocr": {"enabled": False, "inline_max_pages": 5}}
    )

    first = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_off, jobs=registry)
    assert first["job_id"] is None

    cfg_on = DocumentsConfig.model_validate(
        {"ocr": {"enabled": True, "inline_max_pages": 5}}
    )
    second = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_on, jobs=registry)

    assert second["job_id"] is not None
    assert second["job_pages"] == 40
    assert launcher.calls == 1


def test_a_stub_re_ingested_with_ocr_still_off_returns_it_honestly(
    tmp_path, book, registry, monkeypatch
):
    """A stub is not upgradeable just because it exists. While OCR is still
    off, returning the same note again is the honest, current answer, and
    conversion must not re-run just to reconfirm nothing changed."""
    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    cfg_off = DocumentsConfig.model_validate(
        {"ocr": {"enabled": False, "inline_max_pages": 50}}
    )

    first = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_off, jobs=registry)

    def _must_not_convert(*a, **kw):
        raise AssertionError(
            "convert_file_to_markdown must not run while OCR is still unavailable"
        )

    monkeypatch.setattr("durin.memory.doc_convert.convert_file_to_markdown", _must_not_convert)

    second = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_off, jobs=registry)

    assert second["job_id"] is None
    assert second["content"] == first["content"]
    assert launcher.calls == 0


def test_a_half_state_missing_meta_rebuilds_instead_of_short_circuiting(
    tmp_path, book, registry, monkeypatch
):
    """The sidecar write and the meta.json write are each individually
    atomic but not one transaction. A crash between them (simulated here by
    deleting meta.json after a normal ingest) must not become a permanent
    trap: the next ingest has to rebuild both rather than trusting a
    sidecar it cannot verify."""
    from pathlib import Path

    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_pages_detached",
        lambda path, pages: {p: f"Page {p}: real text." for p in pages},
    )
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True, "inline_max_pages": 50}})

    first = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)
    entry_dir = Path(first["source"]).parent
    (entry_dir / "meta.json").unlink()

    second = ingest_artifact(tmp_path / "ws", book, documents_config=cfg, jobs=registry)

    assert second["content"] == first["content"]
    assert (entry_dir / "meta.json").exists()


def _delete_ocr_stub_key(meta_path):
    """Rewrite ``meta.json`` without its ``derived.ocr_stub`` key.

    Exactly the shape every flag-less durin version left behind — same
    payload, same formatting, no verdict. The legacy tests below all start
    from an entry doctored this way after a normal ingest."""
    import json

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["derived"]["ocr_stub"]
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def test_a_legacy_stub_meta_predating_the_flag_still_upgrades(
    tmp_path, book, registry, monkeypatch
):
    """Reproduced pre-fix: a meta.json with no ``ocr_stub`` key at all — what
    every flag-less durin version wrote — read as ``bool(None) == False``,
    i.e. "real transcription, never redone". The stale coverage note came
    back on every re-ingest forever, even after the user did exactly what the
    note says and turned OCR on. An absent key must instead be resolved from
    the sidecar itself, and the stub then upgraded like any other."""
    import json
    from pathlib import Path

    launcher = _CountingPopen()
    monkeypatch.setattr("durin.jobs.spawn.subprocess", launcher)
    cfg_off = DocumentsConfig.model_validate(
        {"ocr": {"enabled": False, "inline_max_pages": 50}}
    )

    first = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_off, jobs=registry)
    assert "turned off" in first["content"]
    entry_dir = Path(first["source"]).parent
    _delete_ocr_stub_key(entry_dir / "meta.json")

    monkeypatch.setattr(
        "durin.memory.doc_convert.transcribe_pages_detached",
        lambda path, pages: {p: f"Page {p}: fresh text." for p in pages},
    )
    cfg_on = DocumentsConfig.model_validate(
        {"ocr": {"enabled": True, "inline_max_pages": 50}}
    )

    second = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_on, jobs=registry)

    assert "fresh text" in second["content"]
    assert "turned off" not in second["content"]
    # Persisted, not just returned: the upgrade landed on disk...
    assert "fresh text" in (entry_dir / "source.md").read_text(encoding="utf-8")
    # ...and meta.json carries the new conversion's own verdict.
    meta_after = json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta_after["derived"]["ocr_stub"] is False
    assert launcher.calls == 0


def test_a_legacy_stub_kept_while_ocr_is_off_persists_its_verdict_surgically(
    tmp_path, book, registry, monkeypatch
):
    """While OCR stays off, a legacy stub is still the honest current answer:
    returned as-is, no conversion. But the classification must be written
    back (``ocr_stub: true``) so the sniff never re-runs — and that write
    must be surgical: a ``derived.summary`` added meanwhile (a dream pass
    distilling the entry) survives untouched."""
    import json
    from pathlib import Path

    cfg_off = DocumentsConfig.model_validate(
        {"ocr": {"enabled": False, "inline_max_pages": 50}}
    )
    first = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_off, jobs=registry)
    meta_path = Path(first["meta_path"])
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    del meta["derived"]["ocr_stub"]
    meta["derived"]["summary"] = "a dream pass distilled this"
    meta_path.write_text(
        json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )

    def _must_not_convert(*a, **kw):
        raise AssertionError("classifying a legacy sidecar must not re-convert it")

    monkeypatch.setattr(
        "durin.memory.doc_convert.convert_file_to_markdown", _must_not_convert
    )

    second = ingest_artifact(tmp_path / "ws", book, documents_config=cfg_off, jobs=registry)

    assert second["content"] == first["content"]
    assert second["job_id"] is None
    meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta_after["derived"]["ocr_stub"] is True
    assert meta_after["derived"]["summary"] == "a dream pass distilled this"


def test_a_legacy_real_transcription_is_sniffed_once_and_never_again(
    tmp_path, registry, monkeypatch
):
    """A flag-less meta.json over an ordinary conversion must be classified
    real from the sidecar itself (no note head) without converting anything,
    and the verdict persisted — after which a further re-ingest must not
    touch meta.json at all. Sniff once, ever."""
    import json
    from pathlib import Path

    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "plain.pdf"
    _write_text_pdf(pdf, ["Plenty of readable body text on this page"])
    cfg = DocumentsConfig.model_validate({"ocr": {"enabled": True}})

    first = ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg, jobs=registry)
    meta_path = Path(first["meta_path"])
    _delete_ocr_stub_key(meta_path)

    def _must_not_convert(*a, **kw):
        raise AssertionError("a legacy real transcription must not be re-converted")

    monkeypatch.setattr(
        "durin.memory.doc_convert.convert_file_to_markdown", _must_not_convert
    )

    second = ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg, jobs=registry)

    assert "readable body text" in second["content"]
    meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta_after["derived"]["ocr_stub"] is False

    stat_before = meta_path.stat()
    third = ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg, jobs=registry)
    assert third["content"] == second["content"]
    stat_after = meta_path.stat()
    assert (stat_after.st_ino, stat_after.st_mtime_ns) == (
        stat_before.st_ino,
        stat_before.st_mtime_ns,
    )


def test_a_legacy_partial_coverage_stub_is_recognized_by_its_warning_head(
    tmp_path, registry, monkeypatch
):
    """Flag-less stubs open with one of two heads: an all-scanned document
    with "[SCANNED DOCUMENT: " and a partially scanned one with
    "[EXTRACTION COVERAGE WARNING: ". The classifier must catch the second
    kind too — a partial stub misread as "real" would freeze half a document
    forever: the same trap, behind the other head."""
    import json
    from pathlib import Path

    from tests.tools.test_read_enhancements import _write_text_pdf

    pdf = tmp_path / "half.pdf"
    _write_text_pdf(
        pdf,
        [
            "Plenty of readable body text right here on this page",
            "",
            "More readable body text continues over on this page",
            "",
        ],
    )
    cfg_off = DocumentsConfig.model_validate(
        {"ocr": {"enabled": False, "inline_max_pages": 50}}
    )

    first = ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg_off, jobs=registry)
    assert first["content"].startswith("[EXTRACTION COVERAGE WARNING: ")
    meta_path = Path(first["meta_path"])
    _delete_ocr_stub_key(meta_path)

    def _must_not_convert(*a, **kw):
        raise AssertionError("classifying a legacy sidecar must not re-convert it")

    monkeypatch.setattr(
        "durin.memory.doc_convert.convert_file_to_markdown", _must_not_convert
    )

    second = ingest_artifact(tmp_path / "ws", pdf, documents_config=cfg_off, jobs=registry)

    assert second["content"] == first["content"]
    meta_after = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta_after["derived"]["ocr_stub"] is True


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
