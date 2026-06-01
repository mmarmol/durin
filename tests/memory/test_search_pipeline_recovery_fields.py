"""`recovered_from` + `recovery_duration_ms` surface in pipeline (P5.2).

When a safe wrapper (vector / lexical / grep) caught an exception
during the pipeline, the resulting `SearchPipelineResult` records
which source failed and how long the failure took. `MemorySearchTool`
exposes these in the response dict so the agent can see degraded
runs without parsing telemetry.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.indexer import rebuild_fts_index
from durin.memory.search_pipeline import (
    SearchPipelineResult,
    run_search_pipeline,
)


def _seed(workspace: Path) -> None:
    EntityPage(
        type="person", name="Marcelo", aliases=["m"],
        body="content",
    ).save(workspace / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(workspace)


def test_clean_run_has_empty_recovery_fields(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = run_search_pipeline(tmp_path, "Marcelo")
    assert isinstance(result, SearchPipelineResult)
    assert result.recovered_from == ()
    assert result.recovery_duration_ms == 0.0


def test_vector_failure_recorded(tmp_path: Path) -> None:
    """When the vector index raises, the pipeline records the
    failure source + duration. Search still completes via the
    remaining sources."""
    _seed(tmp_path)

    class _Broken:
        def search(self, *_a, **_kw):
            raise RuntimeError("simulated lance crash")

    result = run_search_pipeline(
        tmp_path, "Marcelo", vector_index=_Broken(),
    )
    assert "vector" in result.recovered_from
    assert result.recovery_duration_ms > 0.0
    # And the search still produced something (via lexical/grep).
    assert isinstance(result.hits, list)


def test_memory_search_tool_surfaces_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dict the tool returns includes the recovery fields when
    set; omits them when empty (don't pollute clean-run responses)."""
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)

    # Patch run_search_pipeline so we don't need a real broken vector.
    def fake_pipeline(*_a, **_kw):
        return SearchPipelineResult(
            hits=[],
            vector_count=0,
            lexical_count=0,
            recovered_from=("vector",),
            recovery_duration_ms=42.0,
        )

    monkeypatch.setattr(
        "durin.memory.search_pipeline.run_search_pipeline",
        fake_pipeline,
    )
    out = asyncio.run(tool.execute(query="anything"))
    assert out["recovered_from"] == ["vector"]
    assert out["recovery_duration_ms"] == 42.0


def test_memory_search_tool_omits_fields_when_clean(
    tmp_path: Path,
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="Marcelo"))
    # Clean run → no recovery fields in the response dict.
    assert "recovered_from" not in out
    assert "recovery_duration_ms" not in out
