"""The fastembed E5 mean-pooling UserWarning must not reach operators.

fastembed >=0.6 warns on every load that catalog E5 models switched
from CLS to mean pooling. Mean pooling is the desired E5 behaviour
(durin's own e5-small registration declares MEAN), and model switches
force a vector rebuild, so the warning is pure noise in onboard/doctor
output. ``FastembedProvider._load`` filters exactly that message.
"""

from __future__ import annotations

import warnings
from unittest.mock import MagicMock, patch

import pytest

from durin.memory import embedding as embedding_mod

_MODEL = "intfloat/multilingual-e5-large"


def _catalog_entry(model: str) -> dict:
    return {"model": model, "dim": 1024, "size_in_GB": 2.24,
            "description": "test entry", "license": "mit"}


class _WarningTextEmbedding:
    """Stands in for fastembed.TextEmbedding: warns like >=0.6 does."""

    def __init__(self, model_name: str) -> None:
        warnings.warn(
            f"The model {model_name} now uses mean pooling instead of "
            "CLS embedding. In order to preserve the previous behaviour, "
            "consider either pinning fastembed version to 0.5.1 or using "
            "`add_custom_model` functionality.",
            UserWarning,
            stacklevel=2,
        )
        self.model_name = model_name

    @staticmethod
    def list_supported_models() -> list[dict]:
        return [_catalog_entry(_MODEL)]


class _OtherWarningTextEmbedding:
    def __init__(self, model_name: str) -> None:
        warnings.warn("something else is wrong", UserWarning, stacklevel=2)
        self.model_name = model_name

    @staticmethod
    def list_supported_models() -> list[dict]:
        return [_catalog_entry(_MODEL)]


@pytest.fixture
def fake_fastembed(monkeypatch):
    """Install a fake fastembed module and isolate module-global caches."""
    fastembed = MagicMock()
    fastembed.TextEmbedding = _WarningTextEmbedding
    monkeypatch.setattr(embedding_mod, "_register_custom_models", lambda: None)
    monkeypatch.setattr(embedding_mod, "_CATALOG_CACHE", None)
    with patch.dict("sys.modules", {"fastembed": fastembed}):
        yield fastembed
    embedding_mod._CATALOG_CACHE = None


def test_mean_pooling_warning_is_suppressed_on_load(fake_fastembed):
    provider = embedding_mod.FastembedProvider(model=_MODEL)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        provider._load()
    assert provider._model is not None
    pooling_warnings = [
        w for w in caught if "mean pooling" in str(w.message)
    ]
    assert pooling_warnings == []


def test_other_warnings_still_propagate(fake_fastembed):
    """The filter is surgical — unrelated warnings must survive."""
    fake_fastembed.TextEmbedding = _OtherWarningTextEmbedding

    provider = embedding_mod.FastembedProvider(model=_MODEL)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        provider._load()
    assert any("something else" in str(w.message) for w in caught)
