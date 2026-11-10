"""The embedding model's files live in the instance home.

Every fastembed model durin builds is given ``<durin home>/models/embed`` as
its cache directory, so the files sit next to the other local models instead
of in the operating system's temp directory, which the system may empty.
``FASTEMBED_CACHE_PATH``, fastembed's own way to choose the directory, is
honoured when set.

fastembed is replaced by a recorder: no model is loaded and nothing is
downloaded.
"""
from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

import durin.memory.embedding as embedding_module
from durin.config.home import durin_home

_E5_SMALL = "intfloat/multilingual-e5-small"
_E5_LARGE = "intfloat/multilingual-e5-large"


def _catalog_entry(model: str, *, hf: str, model_file: str, dim: int,
                   additional_files: tuple[str, ...] = ()) -> dict:
    """One model as fastembed's ``list_supported_models`` describes it."""
    return {
        "model": model,
        "sources": {"hf": hf, "url": None, "_deprecated_tar_struct": False},
        "model_file": model_file,
        "description": "",
        "license": "mit",
        "size_in_GB": 0.45,
        "additional_files": list(additional_files),
        "dim": dim,
        "tasks": {},
    }


_CATALOG = [
    _catalog_entry(_E5_SMALL, hf="intfloat/multilingual-e5-small",
                   model_file="onnx/model.onnx", dim=384),
    # The repository the files come from is not the model id.
    _catalog_entry(_E5_LARGE, hf="Qdrant/multilingual-e5-large-onnx",
                   model_file="model.onnx", dim=1024,
                   additional_files=("model.onnx_data",)),
]


class _RecordingTextEmbedding:
    """Stands in for ``fastembed.TextEmbedding``: records how it was built."""

    built: list[dict] = []

    @staticmethod
    def list_supported_models() -> list[dict]:
        return [dict(entry) for entry in _CATALOG]

    @staticmethod
    def add_custom_model(**_kwargs) -> None:
        pass

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5",
                 cache_dir: str | None = None, **kwargs) -> None:
        self.model_name = model_name
        type(self).built.append({"model_name": model_name, "cache_dir": cache_dir, **kwargs})

    def embed(self, documents, batch_size: int = 256, **kwargs) -> Iterator[list[float]]:
        for _ in documents:
            yield [0.0] * 384


def install_recorder(monkeypatch) -> type[_RecordingTextEmbedding]:
    """Replace fastembed by the recorder until ``monkeypatch`` undoes it."""
    fastembed = types.ModuleType("fastembed")
    fastembed.TextEmbedding = _RecordingTextEmbedding  # type: ignore[attr-defined]
    description = types.ModuleType("fastembed.common.model_description")
    description.PoolingType = types.SimpleNamespace(MEAN="MEAN", CLS="CLS")  # type: ignore[attr-defined]
    description.ModelSource = lambda **kwargs: kwargs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.common", types.ModuleType("fastembed.common"))
    monkeypatch.setitem(sys.modules, "fastembed.common.model_description", description)
    monkeypatch.setattr(embedding_module, "_CATALOG_CACHE", None)
    monkeypatch.setattr(embedding_module, "_REGISTERED_CUSTOM", set())
    monkeypatch.setattr(_RecordingTextEmbedding, "built", [])
    # A value from the shell running the suite would move the directory.
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    return _RecordingTextEmbedding


@pytest.fixture
def recorder(monkeypatch) -> type[_RecordingTextEmbedding]:
    return install_recorder(monkeypatch)


def _in_home() -> str:
    return str(durin_home() / "models" / "embed")


@pytest.fixture(params=["unset", "empty", "set"])
def model_dir(request, recorder, monkeypatch, tmp_path) -> str:
    """The directory a model must be loaded from, for each state of
    ``FASTEMBED_CACHE_PATH``: the instance home unless the variable names
    another directory."""
    if request.param == "unset":
        return _in_home()
    if request.param == "empty":
        monkeypatch.setenv("FASTEMBED_CACHE_PATH", "")
        return _in_home()
    chosen = tmp_path / "chosen-by-the-user"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(chosen))
    return str(chosen)


# -- where the directory is ---------------------------------------------------


def test_the_model_directory_is_inside_the_instance_home(monkeypatch):
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    assert embedding_module.embedding_cache_dir() == durin_home() / "models" / "embed"


def test_the_model_directory_follows_durin_home(monkeypatch, tmp_path):
    other = tmp_path / "another-instance"
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.setenv("DURIN_HOME", str(other))
    assert embedding_module.embedding_cache_dir() == other / "models" / "embed"


def test_the_model_directory_is_the_one_the_fastembed_variable_names(model_dir):
    assert str(embedding_module.embedding_cache_dir()) == model_dir


# -- every construction uses it -------------------------------------------------


def test_the_provider_loads_its_model_from_the_model_directory(recorder, model_dir):
    provider = embedding_module.FastembedProvider(model=_E5_SMALL)
    provider.embed(["hello"])
    assert recorder.built == [{"model_name": _E5_SMALL, "cache_dir": model_dir}]


def test_the_pool_worker_loads_its_model_from_the_model_directory(
    recorder, model_dir, monkeypatch,
):
    from durin.memory import embedding_worker

    monkeypatch.setattr(embedding_worker, "_MODEL", None)
    embedding_worker.init_worker(_E5_SMALL)
    assert recorder.built == [{"model_name": _E5_SMALL, "cache_dir": model_dir}]


def test_the_onboarding_download_fills_the_model_directory(recorder, model_dir, capsys):
    from durin.cli.onboard_wizard import _maybe_warm_embedding_model

    _maybe_warm_embedding_model(_E5_LARGE, "2.24 GB")
    assert recorder.built == [{"model_name": _E5_LARGE, "cache_dir": model_dir}]
    assert "warmup failed" not in capsys.readouterr().out


def test_the_embed_server_loads_its_model_from_the_model_directory(
    recorder, model_dir, monkeypatch,
):
    import uvicorn

    from durin.memory import embed_server

    class _ServerThatDoesNotServe:
        def __init__(self, config) -> None:
            pass

        def run(self, sockets) -> None:
            for sock in sockets:
                sock.close()

    monkeypatch.setattr(uvicorn, "Server", _ServerThatDoesNotServe)
    embed_server.run_embed_server(model=_E5_SMALL)
    assert recorder.built == [{"model_name": _E5_SMALL, "cache_dir": model_dir}]


# -- whether the files are there ------------------------------------------------


def store_model(repo: str, files: tuple[str, ...], *, into: str | None = None) -> Path:
    """Lay ``repo`` out the way fastembed stores a downloaded model: the
    content under ``blobs/``, and a snapshot whose entries are links to it.
    In the instance home's model directory unless ``into`` names another."""
    root = Path(into or _in_home()) / ("models--" + repo.replace("/", "--"))
    blobs = root / "blobs"
    snapshot = root / "snapshots" / "0a1b2c3d"
    blobs.mkdir(parents=True)
    for number, name in enumerate(files):
        blob = blobs / f"blob{number}"
        blob.write_bytes(b"weights")
        link = snapshot / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(blob)
    return root


def test_a_model_never_downloaded_is_not_on_disk(recorder):
    assert embedding_module.embedding_model_on_disk(_E5_SMALL) is False


def test_a_downloaded_model_is_on_disk(recorder):
    store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",))
    assert embedding_module.embedding_model_on_disk(_E5_SMALL) is True


def test_a_snapshot_of_broken_links_is_not_on_disk(recorder):
    root = store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",))
    for blob in (root / "blobs").iterdir():
        blob.unlink()
    assert (root / "snapshots" / "0a1b2c3d" / "onnx" / "model.onnx").is_symlink()
    assert embedding_module.embedding_model_on_disk(_E5_SMALL) is False


def test_the_files_are_looked_up_under_the_repository_they_come_from(recorder):
    store_model("Qdrant/multilingual-e5-large-onnx", ("model.onnx", "model.onnx_data"))
    assert embedding_module.embedding_model_on_disk(_E5_LARGE) is True


def test_a_model_missing_one_of_its_files_is_not_on_disk(recorder):
    store_model("Qdrant/multilingual-e5-large-onnx", ("model.onnx",))
    assert embedding_module.embedding_model_on_disk(_E5_LARGE) is False


def test_the_files_are_looked_up_in_the_directory_the_fastembed_variable_names(
    recorder, monkeypatch, tmp_path,
):
    chosen = tmp_path / "chosen-by-the-user"
    store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",))
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(chosen))
    assert embedding_module.embedding_model_on_disk(_E5_SMALL) is False
    store_model("intfloat/multilingual-e5-small", ("onnx/model.onnx",), into=str(chosen))
    assert embedding_module.embedding_model_on_disk(_E5_SMALL) is True


def test_looking_for_the_files_builds_no_model_and_creates_nothing(recorder):
    assert embedding_module.embedding_model_on_disk(_E5_SMALL) is False
    assert recorder.built == []
    assert not Path(_in_home()).exists()
