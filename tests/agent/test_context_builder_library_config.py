"""The stable layer honours ``memory.library`` when it builds the pinned block.

``ContextBuilder._build_pinned_memory`` is the only place that reads the config
for the Library awareness catalog, so this drives the real prompt build with a
stubbed config and proves the cap reaches ``build_library_awareness``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from durin.agent.context import ContextBuilder
from durin.memory.reference import ingest_reference


def test_stable_layer_caps_the_library_catalog_from_config(tmp_path: Path, monkeypatch) -> None:
    cfg = SimpleNamespace(
        memory=SimpleNamespace(
            owner=None,
            library=SimpleNamespace(awareness_max_docs=1, awareness_abstracts=False),
        ),
    )
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **kw: cfg)

    ingest_reference(tmp_path, "A Book", "# a\n\nx.\n")
    ingest_reference(tmp_path, "B Book", "# b\n\ny.\n")

    stable = ContextBuilder(workspace=tmp_path)._build_stable_layer(channel=None)

    assert "- A Book" in stable
    assert "- B Book" not in stable
    assert "…and 1 more" in stable
