"""`durin doctor` reports an embedding model whose files are not on disk.

The files check only reads the model's directory, and the load probe leaves a
model that is not on disk alone, so a doctor run downloads nothing. fastembed
is replaced by a recorder, and the embedding worker pool by one that records
each attempt to start a worker.
"""
from __future__ import annotations

import json
import sys

import httpx
import pytest

import durin.memory.embedding as embedding_module
from durin.cli import doctor
from durin.cli.doctor import check_embedding_model_cached
from durin.config.home import durin_home
from durin.config.loader import get_config_path
from durin.config.schema import Config
from durin.memory import embed_server
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


def test_doctor_looks_in_the_directory_the_fastembed_variable_names(
    recorder, monkeypatch, tmp_path,
):
    chosen = tmp_path / "chosen-by-the-user"
    store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(chosen))
    missing = check_embedding_model_cached(cfg=_config())
    assert missing.status == "warn"
    assert str(chosen) in missing.message

    store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",), into=str(chosen))
    present = check_embedding_model_cached(cfg=_config())
    assert present.status == "ok"
    assert str(chosen) in present.message


@pytest.mark.parametrize("value", [None, ""])
def test_doctor_looks_in_the_instance_home_without_the_fastembed_variable(
    recorder, monkeypatch, value,
):
    if value is not None:
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", value)
    store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",))
    result = check_embedding_model_cached(cfg=_config())
    assert result.status == "ok"
    assert str(durin_home() / "models" / "embed") in result.message


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


# -- the load probe never downloads ---------------------------------------------


class _RecordingPool:
    """Stands in for the embedding worker pool: records each attempt to start
    a worker and refuses it, the way a host that cannot spawn one does. A real
    worker is another process, which would load the real fastembed."""

    started: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        type(self).started.append(kwargs)
        raise OSError("worker processes are not started in tests")


@pytest.fixture
def pools(monkeypatch) -> type[_RecordingPool]:
    monkeypatch.setattr(_RecordingPool, "started", [])
    monkeypatch.setattr(embedding_module, "ProcessPoolExecutor", _RecordingPool)
    return _RecordingPool


def _write_config(isolation: str) -> None:
    data = Config().model_dump(mode="json", by_alias=True)
    data["memory"]["embedding"]["isolation"] = isolation
    # The gateway and dashboard rows are not what these tests are about.
    data.setdefault("gateway", {}).update({"daemon": False, "webuiEnabled": False})
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _row(report: doctor.DoctorReport, name: str) -> doctor.CheckResult:
    (row,) = [r for r in report.results if r.name == name]
    return row


@pytest.mark.parametrize("isolation", ["service", "process", "inline"])
def test_a_doctor_run_builds_no_model_when_the_files_are_missing(recorder, pools, isolation):
    _write_config(isolation)
    report = doctor.run_checks()
    assert recorder.built == []
    assert pools.started == []
    assert _row(report, "embedding model files").status == "warn"
    load = _row(report, "embedding model load")
    assert load.status == "ok"
    assert "not attempted" in load.message
    assert "embedding model files" in load.message


def _served_embedding(url: str, **_kwargs) -> httpx.Response:
    """What the embedding server answers for one text."""
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": _E5_SMALL,
            "data": [{"object": "embedding", "index": 0, "embedding": [0.0] * 384}],
        },
        request=httpx.Request("POST", url),
    )


def test_with_the_files_missing_the_probe_asks_a_running_server(recorder, pools, monkeypatch):
    _write_config("service")
    embed_server.write_discovery(port=4321, token="t0ken", model=_E5_SMALL)
    asked: list[str] = []

    def post(url: str, **kwargs) -> httpx.Response:
        asked.append(url)
        return _served_embedding(url, **kwargs)

    monkeypatch.setattr(httpx, "post", post)
    result = doctor.check_embedding_model_loads()
    assert result.status == "ok"
    assert "384-dim" in result.message
    assert asked == ["http://127.0.0.1:4321/v1/embeddings"]
    assert recorder.built == []
    assert pools.started == []


def test_with_the_files_missing_a_server_that_does_not_answer_is_no_load(
    recorder, pools, monkeypatch,
):
    _write_config("service")
    embed_server.write_discovery(port=4321, token="t0ken", model=_E5_SMALL)

    def post(url: str, **_kwargs) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    result = doctor.check_embedding_model_loads()
    assert result.status == "ok"
    assert "not attempted" in result.message
    assert recorder.built == []
    assert pools.started == []


def test_with_the_files_on_disk_the_probe_loads_the_model(recorder, pools):
    _write_config("inline")
    store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",))
    result = doctor.check_embedding_model_loads()
    assert result.status == "ok"
    assert "384-dim" in result.message
    assert recorder.built == [
        {"model_name": _E5_SMALL, "cache_dir": str(durin_home() / "models" / "embed")},
    ]
