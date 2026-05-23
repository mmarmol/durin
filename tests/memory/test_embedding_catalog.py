"""Coherence test: every embedding model durin advertises must exist
in fastembed's runtime catalog.

This is the guardrail against silent catalog drift between fastembed
minor releases (e5-small disappeared in 0.6, MiniLM-L12 changed pooling
0.5→0.6, etc.). Running it in CI prevents a future fastembed upgrade
from shipping a wizard that lists models that no longer exist.

Skipped automatically if fastembed is not installed (the ``[memory]``
extra is optional).
"""

from __future__ import annotations

import pytest

# Hard skip if the optional extra is missing.
fastembed = pytest.importorskip("fastembed")

from durin.cli.onboard_wizard import _EMBEDDING_CHOICES
from durin.config.schema import MemoryEmbeddingConfig
from durin.memory.embedding import FastembedProvider, list_supported_models


def _catalog_names() -> set[str]:
    return set(list_supported_models().keys())


def test_default_model_exists_in_fastembed_catalog() -> None:
    """The MemoryEmbeddingConfig default must be a real model."""
    default_model = MemoryEmbeddingConfig().model
    assert default_model in _catalog_names(), (
        f"Default model {default_model!r} is not in fastembed's catalog. "
        f"Either fix the default in durin/config/schema.py or revisit "
        f"the fastembed pin in pyproject.toml."
    )


def test_fastembedprovider_default_constant_matches_config_default() -> None:
    """The DEFAULT_MODEL constant and the config default must agree —
    otherwise a caller that instantiates FastembedProvider() and a
    caller that reads MemoryEmbeddingConfig().model see different
    models."""
    assert FastembedProvider.DEFAULT_MODEL == MemoryEmbeddingConfig().model


def test_every_wizard_choice_exists_in_fastembed_catalog() -> None:
    """Every model the wizard offers must be in fastembed's catalog."""
    catalog = _catalog_names()
    for label, provider, model_id, _size in _EMBEDDING_CHOICES:
        assert provider == "fastembed", (
            f"Wizard choice {label!r} declares provider {provider!r}; "
            f"only fastembed is supported in V1."
        )
        assert model_id in catalog, (
            f"Wizard choice {label!r} points to {model_id!r} which is "
            f"not in fastembed's catalog. Update _EMBEDDING_CHOICES in "
            f"durin/cli/onboard_wizard.py."
        )


def test_default_model_is_in_wizard_choices() -> None:
    """The default the user gets without touching the wizard must be
    one of the choices the wizard offers — otherwise the wizard UI
    misleads the user about what's currently active."""
    default_model = MemoryEmbeddingConfig().model
    wizard_models = {choice[2] for choice in _EMBEDDING_CHOICES}
    assert default_model in wizard_models, (
        f"Default model {default_model!r} is not in the wizard's choice "
        f"list — the wizard's 'current selection' line will be wrong."
    )


def test_wizard_choices_are_constructible() -> None:
    """Every wizard model id must construct a FastembedProvider without
    error — proves the catalog validation in __init__ accepts them."""
    for label, _provider, model_id, _size in _EMBEDDING_CHOICES:
        FastembedProvider(model_id)  # raises ValueError if catalog drift
