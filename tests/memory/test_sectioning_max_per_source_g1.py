"""G1 (audit fourth pass, 2026-05-28): ship the
`memory.search.sectioning.max_per_source` config knob.

Doc 03 §16 row 8 has promised `Configurable via
memory.search.sectioning.max_per_source` since Phase 3 (commit
792f1c6) but the field never landed. `DEFAULT_MAX_PER_SOURCE = 3`
was hard-coded in `sectioned_output.py` and the 3 callsites
(`search_pipeline.py:182`, `memory_search.py:461`,
`memory_search.py:657`) all used the default. Operators with
PDF-heavy ingest corpora had no way to tune the cap without
patching code.

G1 wires:
- `MemorySearchSectioningConfig.max_per_source: int = 3`.
- `run_search_pipeline(..., max_per_source=...)` accepts an override.
- `memory_search.execute` reads the config and threads it through.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def test_config_field_exists_with_default_3() -> None:
    from durin.config.schema import MemorySearchSectioningConfig

    cfg = MemorySearchSectioningConfig()
    assert cfg.max_per_source == 3


def test_search_config_carries_sectioning_subsection() -> None:
    from durin.config.schema import MemorySearchConfig

    cfg = MemorySearchConfig()
    # Default sectioning sub-config present.
    assert hasattr(cfg, "sectioning")
    assert cfg.sectioning.max_per_source == 3


def test_run_search_pipeline_accepts_max_per_source(
    tmp_path: Path,
) -> None:
    """The pipeline signature gains `max_per_source` so callers can
    override the default. Smoke test: no exception on a real run."""
    from durin.memory.entity_page import EntityPage
    from durin.memory.indexer import rebuild_fts_index
    from durin.memory.search_pipeline import run_search_pipeline

    EntityPage(
        type="person", name="Marcelo", aliases=["marcelo"], body="b",
    ).save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(tmp_path)
    result = run_search_pipeline(
        tmp_path, "marcelo", max_per_source=5,
    )
    assert isinstance(result.hits, list)


def test_cap_override_changes_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed N corpus hits sharing an ingest_id; assert the cap is
    actually honoured at the override value, not the default.
    Uses `apply_per_source_cap` directly so the test stays
    deterministic without rebuilding indexes."""
    from durin.memory.sectioned_output import (
        SectionedHit,
        apply_per_source_cap,
    )

    hits = [
        SectionedHit(
            uri=f"corpus/doc-a/chunk-{i}",
            type="corpus",
            path=f"memory/corpus/doc-a/chunk-{i}.md",
            score=1.0 - i * 0.01,
            ingest_id="doc-a",
        )
        for i in range(10)
    ]

    capped_default = apply_per_source_cap(hits)
    assert len(capped_default) == 3

    capped_5 = apply_per_source_cap(hits, max_per_source=5)
    assert len(capped_5) == 5

    capped_1 = apply_per_source_cap(hits, max_per_source=1)
    assert len(capped_1) == 1


def test_memory_search_threads_config_to_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: when `cfg.memory.search.sectioning.max_per_source = 5`,
    `memory_search.execute` passes that value into the pipeline call."""
    from durin.config.schema import (
        Config,
        MemorySearchConfig,
        MemorySearchSectioningConfig,
    )

    cfg = Config()
    cfg.memory.search = MemorySearchConfig(
        sectioning=MemorySearchSectioningConfig(max_per_source=5),
    )

    # Capture the kwarg passed into run_search_pipeline.
    captured: dict = {}
    import durin.memory.search_pipeline as sp
    real_run = sp.run_search_pipeline

    def _spy(*args, **kwargs):
        captured["max_per_source"] = kwargs.get("max_per_source")
        return real_run(*args, **kwargs)

    monkeypatch.setattr(sp, "run_search_pipeline", _spy)

    from durin.memory.entity_page import EntityPage
    from durin.memory.indexer import rebuild_fts_index
    EntityPage(
        type="person", name="X", aliases=[], body="b",
    ).save(tmp_path / "memory" / "entities" / "person" / "x.md")
    rebuild_fts_index(tmp_path)

    from durin.agent.tools.memory_search import MemorySearchTool
    tool = MemorySearchTool(workspace=tmp_path, app_config=cfg)
    asyncio.run(tool.execute(query="X"))

    assert captured.get("max_per_source") == 5
