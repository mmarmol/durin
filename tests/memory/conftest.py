"""Shared fixtures for memory tests."""
import sys
from pathlib import Path

import pytest

EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
_FAKES = Path(__file__).resolve().parent.parent / "fakes"


def _fastembed_modules() -> list[str]:
    return [name for name in sys.modules if name == "fastembed" or name.startswith("fastembed.")]


@pytest.fixture
def fastembed_stand_in(monkeypatch):
    """Embed with the stand-in in tests/fakes/fastembed instead of a real
    model, in this process and in every process it spawns (the embedding
    pool's workers import from this process's sys.path), so nothing loads or
    downloads a model. Any fastembed modules already imported are set aside
    for the test and put back after it."""
    import durin.memory.embedding as embedding_module

    saved = {name: sys.modules.pop(name) for name in _fastembed_modules()}
    monkeypatch.syspath_prepend(str(_FAKES))
    monkeypatch.setattr(embedding_module, "_CATALOG_CACHE", None)
    monkeypatch.setattr(embedding_module, "_REGISTERED_CUSTOM", set())
    import fastembed

    assert Path(fastembed.__file__).is_relative_to(_FAKES), fastembed.__file__
    try:
        yield fastembed
    finally:
        for name in _fastembed_modules():
            del sys.modules[name]
        sys.modules.update(saved)


@pytest.fixture
def embedding_model(fastembed_stand_in) -> str:
    """The E5 model id, embedded by the fastembed stand-in: the test exercises
    the vector index end to end without loading or downloading the model.
    Skips the requesting test when the vector index's own dependency
    (lancedb, from the [memory] extra) is not installed."""
    from durin.memory.vector_index import vector_index_available

    if not vector_index_available():
        pytest.skip("vector index unavailable in this environment")
    return EMBEDDING_MODEL
