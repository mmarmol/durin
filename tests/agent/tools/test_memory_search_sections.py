"""How `memory_search` buckets and tags a hit in its rendered output.

Two shaping steps that live in the tool, not in the renderer: which section
a hit's class maps to, and which entity refs ride the block's ``Entities:``
tail. Both are pure functions of one pipeline hit, so the tests stub
``run_search_pipeline`` with hand-built ``SectionedHit`` rows — the same seam
the warm-budget tests use — rather than standing up lancedb/fastembed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.search_pipeline import SearchPipelineResult
from durin.memory.sectioned_output import SectionedHit


def _stub_pipeline(
    monkeypatch: pytest.MonkeyPatch, hits: list[SectionedHit],
) -> None:
    """``run_search_pipeline`` is lazy-imported inside ``execute()`` — patch
    it at its source module so the import binds the stub."""
    monkeypatch.setattr(
        "durin.memory.search_pipeline.run_search_pipeline",
        lambda *a, **kw: SearchPipelineResult(
            hits=hits, vector_count=len(hits), lexical_count=0,
        ),
    )


def test_session_summary_hit_renders_its_entities_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A summary carries the entity refs the archive prompt extracted; the
    block must show them so the agent can drill to the canonical pages."""
    summary = "Marcelo reviewed the ranking pipeline."
    hit = SectionedHit(
        uri="memory/session_summary/cli_test", type="session_summary",
        path="memory/session_summary/cli_test.md", score=1.0,
        ts="2026-05-20", snippet="Marcelo reviewed the ranking",
        summary=summary, body_length=len(summary),
        entities=("person:marcelo", "project:durin"),
    )
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="ranking", level="warm"))

    assert "Entities: person:marcelo, project:durin" in out["sectioned_rendered"]


def test_raw_session_turn_hit_renders_in_the_session_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw turn is session material, not a fragment: its class must reach
    the renderer as `session` or the block lands under the wrong heading with
    the wrong marker."""
    hit = SectionedHit(
        uri="sessions/cli_test.md#turn-3", type="session",
        path="sessions/cli_test.md", score=1.0, ts="2026-05-20",
        snippet="the turn that matched", summary="the turn that matched",
    )
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="anything", level="warm"))
    rendered = out["sectioned_rendered"]

    assert "=== SESSION:" in rendered
    assert "## Fragment" not in rendered
    assert "=== FRAGMENT:" not in rendered
