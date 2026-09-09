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
from typing import Any

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


def test_raw_session_turn_cold_level_never_shows_less_than_warm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Important 2: a raw session turn's uri (`sessions/<key>.md`) addresses
    a rendered transcript with no YAML frontmatter — the real file on disk
    reproduces that shape here, so `_enrich_body`'s `load_entry` raises
    `FrontmatterError` and the result comes back unchanged, leaving `body`
    empty. Before the fix, `summary` was cleared unconditionally for cold,
    so the block fell through to the 160-char `snippet` — smaller than what
    warm rendered for the exact same hit. Cold must render at least the
    warm excerpt."""
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "cli_test.md").write_text(
        "## Turn 1\n\nUser: what's the ranking pipeline change?\n"
        "Assistant: widened the recall window.\n",
        encoding="utf-8",
    )
    summary = (
        "Marcelo reviewed the ranking pipeline and decided to widen the "
        "recall window for lexical matches before merging the change to "
        "the search pipeline configuration and its documentation."
    )
    assert len(summary) > 160
    hit = SectionedHit(
        uri="sessions/cli_test.md", type="session",
        path="sessions/cli_test.md", score=1.0, ts="2026-05-20",
        snippet=summary[:160], summary=summary,
    )

    _stub_pipeline(monkeypatch, [hit])
    tool = MemorySearchTool(workspace=tmp_path)
    warm = asyncio.run(
        tool.execute(query="ranking", level="warm"),
    )["sectioned_rendered"]

    _stub_pipeline(monkeypatch, [hit])
    cold = asyncio.run(
        tool.execute(query="ranking", level="cold"),
    )["sectioned_rendered"]

    assert summary in warm
    assert summary in cold


def _stub_pipeline_capturing_scope(
    monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any],
) -> None:
    """Patch ``run_search_pipeline`` at its source module (same seam as
    ``_stub_pipeline``) and record the ``scope`` kwarg the tool passed
    through, so the test can assert on the predicate itself rather than
    on post-filtered hits."""
    def fake_pipeline(*args: Any, **kw: Any) -> SearchPipelineResult:
        seen["scope"] = kw.get("scope")
        return SearchPipelineResult(hits=[], vector_count=0, lexical_count=0)

    monkeypatch.setattr(
        "durin.memory.search_pipeline.run_search_pipeline", fake_pipeline,
    )


def test_kinds_skill_reaches_the_indexes_as_a_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`kinds="skill"` must reach the pipeline as a scope predicate — the
    indexes filter `class_name` before the top-k; the tool no longer
    post-filters hits in Python."""
    seen: dict[str, Any] = {}
    _stub_pipeline_capturing_scope(monkeypatch, seen)

    tool = MemorySearchTool(workspace=tmp_path)
    asyncio.run(tool.execute(query="axe", kinds="skill"))

    assert seen["scope"].vector_where == "class_name = 'skill'"


def test_library_scope_is_a_predicate_not_a_post_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`scope="library"` must reach the pipeline as a scope predicate too —
    the reference class is what the indexes filter on, not a post-hoc
    Python filter over the pipeline's hits."""
    seen: dict[str, Any] = {}
    _stub_pipeline_capturing_scope(monkeypatch, seen)

    tool = MemorySearchTool(workspace=tmp_path)
    asyncio.run(tool.execute(query="axe", scope="library"))

    assert seen["scope"].fts_include == ("reference", "corpus")
    assert seen["scope"].vector_where == "class_name IN ('reference', 'corpus')"
