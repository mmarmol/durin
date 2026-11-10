"""`durin doctor` reports an embedding model whose files are not on disk.

The check only reads the instance home: it loads no model, downloads nothing
and installs nothing. fastembed is replaced by a recorder.
"""
from __future__ import annotations

import sys

import pytest

import durin.memory.embedding as embedding_module
from durin.cli import doctor
from durin.cli.doctor import check_embedding_model_cached
from durin.config.home import durin_home
from durin.config.schema import Config
from tests.memory.test_embedding_model_dir import install_recorder, store_model

_E5_SMALL = "intfloat/multilingual-e5-small"


@pytest.fixture
def recorder(monkeypatch):
    return install_recorder(monkeypatch)


def _config(*, enabled: bool = True, model: str = _E5_SMALL) -> Config:
    config = Config()
    config.memory.enabled = enabled
    config.memory.embedding.model = model
    return config


def test_doctor_reports_a_model_that_was_never_downloaded(recorder):
    result = check_embedding_model_cached(cfg=_config())
    assert result.status == "warn"
    assert _E5_SMALL in result.message
    assert str(durin_home() / "models" / "embed") in result.message
    assert result.fix


def test_doctor_reports_nothing_when_the_files_are_present(recorder):
    store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",))
    result = check_embedding_model_cached(cfg=_config())
    assert result.status == "ok"
    assert result.fix is None


def test_doctor_reports_a_snapshot_made_only_of_broken_links(recorder):
    root = store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",))
    for blob in (root / "blobs").iterdir():
        blob.unlink()
    result = check_embedding_model_cached(cfg=_config())
    assert result.status == "warn"
    assert result.fix


def test_doctor_reports_nothing_when_vector_memory_is_off(recorder):
    result = check_embedding_model_cached(cfg=_config(enabled=False))
    assert result.status == "ok"
    assert result.fix is None


def test_the_check_downloads_and_builds_nothing(recorder):
    """The suite's network guard fails this test on any outbound connection;
    the recorder shows no model was built, and the directory a download would
    create is still absent."""
    result = check_embedding_model_cached(cfg=_config())
    assert result.status == "warn"
    assert recorder.built == []
    assert not (durin_home() / "models" / "embed").exists()


def test_a_missing_memory_extra_is_left_to_its_own_row(monkeypatch):
    """Without fastembed nothing can say which files the model needs; the
    embedding-model row already reports the missing extra."""
    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setattr(embedding_module, "_CATALOG_CACHE", None)
    result = check_embedding_model_cached(cfg=_config())
    assert result.status == "ok"


def test_a_model_outside_the_catalog_is_left_to_its_own_row(recorder):
    result = check_embedding_model_cached(cfg=_config(model="no-such/model"))
    assert result.status == "ok"


def test_the_full_doctor_run_includes_the_row(recorder, monkeypatch):
    for name in ("check_embedding_model", "check_embedding_model_loads"):
        monkeypatch.setattr(
            doctor, name,
            lambda: doctor.CheckResult("stubbed", "ok", "stubbed in test", category="state"),
        )
    report = doctor.run_checks()
    rows = [r for r in report.results if r.name == "embedding model files"]
    assert [r.status for r in rows] == ["warn"]
