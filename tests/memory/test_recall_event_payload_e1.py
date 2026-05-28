"""E1 (audit second pass, 2026-05-28): `memory.recall` payload must
expose the diagnostic fields documented in `docs/memory/07` §4.1.

Pre-E1, the event only carried `query`, `scope`, `level`,
`result_count` — useful for counts but blind to: which path produced
results (`strategy`), wall-clock cost (`duration_ms`), whether the
LLM passed a keyword hint (`keywords`), whether the pipeline degraded
(`recovered_from` + `recovery_duration_ms`), and what the pre-limit
candidate population looked like (`total_candidates`).

A8 principle (telemetry is first-class observability infra): all of
these are already computed at the emission site in
`memory_search.py` — including them is a payload change, not new
instrumentation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durin.memory.entity_page import EntityPage
from durin.memory.indexer import rebuild_fts_index
from durin.memory.search_pipeline import SearchPipelineResult


def _seed(workspace: Path) -> None:
    EntityPage(
        type="person", name="Marcelo", aliases=["m"],
        body="content",
    ).save(workspace / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(workspace)


def _capture_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.agent.tools.memory_search.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    return events


def test_recall_payload_includes_strategy_and_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`strategy` + `duration_ms` are non-optional diagnostics that
    every recall call should emit. `strategy` discriminates between
    `vector`/`lexical`/`hybrid`/`grep` paths; `duration_ms` enables
    p50/p95 latency dashboards."""
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    events = _capture_recall(monkeypatch)
    tool = MemorySearchTool(workspace=tmp_path)

    asyncio.run(tool.execute(query="Marcelo"))

    recall = [p for t, p in events if t == "memory.recall"]
    assert len(recall) == 1
    payload = recall[0]
    assert "strategy" in payload
    assert payload["strategy"] in ("vector", "lexical", "hybrid", "grep")
    assert "duration_ms" in payload
    assert isinstance(payload["duration_ms"], float)
    assert payload["duration_ms"] >= 0.0


def test_recall_payload_includes_total_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`total_candidates` reflects pre-limit candidate count so
    dashboards can distinguish 'pipeline retrieved 0' from
    'pipeline retrieved 50 but limit cut to 10'."""
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    events = _capture_recall(monkeypatch)
    tool = MemorySearchTool(workspace=tmp_path)

    asyncio.run(tool.execute(query="Marcelo"))

    payload = [p for t, p in events if t == "memory.recall"][0]
    assert "total_candidates" in payload
    assert isinstance(payload["total_candidates"], int)
    assert payload["total_candidates"] >= 0


def test_recall_payload_includes_keywords_field_when_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the LLM supplies a `keywords` hint, the event records
    the literal string. Lets dashboards measure how often the hint
    parameter is actually used by the agent."""
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    events = _capture_recall(monkeypatch)
    tool = MemorySearchTool(workspace=tmp_path)

    asyncio.run(tool.execute(query="Marcelo", keywords="kw1 kw2"))

    payload = [p for t, p in events if t == "memory.recall"][0]
    assert payload.get("keywords") == "kw1 kw2"


def test_recall_payload_keywords_is_none_when_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    events = _capture_recall(monkeypatch)
    tool = MemorySearchTool(workspace=tmp_path)

    asyncio.run(tool.execute(query="Marcelo"))

    payload = [p for t, p in events if t == "memory.recall"][0]
    assert payload.get("keywords") is None


def test_recall_payload_surfaces_recovery_fields_on_degraded_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the pipeline records a recovered source, the event
    payload exposes `recovered_from` + `recovery_duration_ms` so
    dashboards can alert on degradation. Omitted on clean runs."""
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    events = _capture_recall(monkeypatch)
    tool = MemorySearchTool(workspace=tmp_path)

    def fake_pipeline(*_a, **_kw):
        return SearchPipelineResult(
            hits=[],
            vector_count=0,
            lexical_count=0,
            recovered_from=("vector",),
            recovery_duration_ms=123.5,
        )

    # `run_search_pipeline` is lazy-imported inside `execute()` —
    # patch at source module so the import binds the stub.
    monkeypatch.setattr(
        "durin.memory.search_pipeline.run_search_pipeline",
        fake_pipeline,
    )
    asyncio.run(tool.execute(query="anything"))

    payload = [p for t, p in events if t == "memory.recall"][0]
    assert payload.get("recovered_from") == ["vector"]
    assert payload.get("recovery_duration_ms") == 123.5


def test_recall_payload_omits_recovery_fields_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.agent.tools.memory_search import MemorySearchTool

    _seed(tmp_path)
    events = _capture_recall(monkeypatch)
    tool = MemorySearchTool(workspace=tmp_path)

    asyncio.run(tool.execute(query="Marcelo"))

    payload = [p for t, p in events if t == "memory.recall"][0]
    assert "recovered_from" not in payload
    assert "recovery_duration_ms" not in payload
