"""Warm ``memory_search`` output is bounded per hit and per response.

Two knobs (``MemorySearchConfig.warm_excerpt_chars`` /
``warm_max_chars``) cap what used to be unbounded: at warm level a
non-entity hit's ``summary`` is whatever the pipeline materialised —
for a ``session_summary`` entry that is its whole accumulated text (up
to 16 000 chars, the compactor's own budget), rendered in full because
``_sectioned_to_result`` previously read the bare (short) snippet
instead. Cold level had the mirror bug: the short snippet shadowed the
real full body in ``_render_block``'s ``summary > body > snippet``
preference, so cold never actually showed more than warm did.

Most tests here stub ``run_search_pipeline`` with a hand-built
``SectionedHit`` — the same seam ``test_recall_event_payload_e1.py``
uses — so the warm/cold rendering and budget logic can be exercised
deterministically without lancedb/fastembed. The field values used
(``summary`` carrying the full materialised text, ``body_length`` the
true full length, ``snippet`` a short headline) were verified against
a real vector-backed run before writing these tests, not guessed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from durin.agent.tools.memory_search import MemorySearchTool
from durin.memory.search_pipeline import SearchPipelineResult
from durin.memory.sectioned_output import SectionedHit
from durin.memory.session_summary_store import (
    sanitize_session_key,
    write_session_summary,
)


def _capture_recall(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.agent.tools.memory_search.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    return events


def _stub_pipeline(
    monkeypatch: pytest.MonkeyPatch, hits: list[SectionedHit],
) -> None:
    """`run_search_pipeline` is lazy-imported inside `execute()` — patch
    at source module so the import binds the stub (same idiom as
    `test_recall_event_payload_e1.py`)."""
    monkeypatch.setattr(
        "durin.memory.search_pipeline.run_search_pipeline",
        lambda *a, **kw: SearchPipelineResult(
            hits=hits, vector_count=len(hits), lexical_count=0,
        ),
    )


def _session_summary_hit(workspace: Path, session_key: str, text: str) -> SectionedHit:
    """Write a real session_summary .md (so cold-tier disk enrichment has
    something faithful to read) and return the pipeline-shaped hit a real
    vector-backed search would produce for it: `summary` carries the whole
    materialised text, `body_length` the true full length, `snippet` a
    short headline — mirrors `VectorIndex._record_with_vector` /
    `_effective_summary`."""
    write_session_summary(
        workspace, session_key, text, last_active="2026-05-20T10:00:00Z",
    )
    key = sanitize_session_key(session_key)
    uri = f"memory/session_summary/{key}"
    return SectionedHit(
        uri=uri, type="session_summary", path="", score=1.0,
        ts="2026-05-20", snippet=text[:49],
        summary=text, body_length=len(text),
    )


# ---------------------------------------------------------------------------
# (a) a session_summary entry's warm excerpt is bounded; cold shows it whole.
# ---------------------------------------------------------------------------


def _make_long_session_summary_text(min_chars: int = 5000) -> str:
    """A session-summary body of at least *min_chars*, built from whole
    sentences and ending in a unique marker with no leading/trailing
    whitespace — `write_session_summary` strips its input, so a fixture
    with boundary whitespace would silently round-trip shorter than
    written and break an exact `text in rendered` check."""
    sentence = "Marcelo discussed the memory ranking pipeline in depth."
    marker = "UNIQUE_TAIL_MARKER_END"
    body = sentence
    while len(body) < min_chars:
        body += " " + sentence
    return body + " " + marker


def test_warm_level_cuts_session_summary_to_excerpt_with_preview_qualifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _make_long_session_summary_text()
    hit = _session_summary_hit(tmp_path, "cli:budget-warm", text)
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="anything", scope="dreamed", level="warm"),
    )
    rendered = out["sectioned_rendered"]

    assert text[:600] in rendered
    assert "UNIQUE_TAIL_MARKER_END" not in rendered
    assert f"preview 600/{len(text)}" in rendered


def test_cold_level_shows_the_whole_session_summary_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _make_long_session_summary_text()
    hit = _session_summary_hit(tmp_path, "cli:budget-cold", text)
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="anything", scope="dreamed", level="cold"),
    )
    rendered = out["sectioned_rendered"]

    assert text in rendered
    assert ", complete)" in rendered


# ---------------------------------------------------------------------------
# Config wiring: `load_config().memory.search` with schema defaults as the
# fallback (mirrors `_get_vector_index`'s own `load_config()` use in this
# file) — so callers that construct the tool directly (graph_api, webui
# search, tier2_judge — none of them pass `app_config`) still honour the
# operator's configured budget, not just the agent's own tool-call path.
# ---------------------------------------------------------------------------


def test_default_warm_excerpt_chars_applied_without_app_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No app_config, no monkeypatched config — the schema default (600)
    still bounds a large hit's warm summary via the real `load_config()`
    fallback path (a fresh DURIN_HOME has no config.json; pydantic
    defaults apply)."""
    hit = SectionedHit(
        uri="memory/episodic/e1", type="episodic", path="",
        score=1.0, snippet="short headline", summary="z" * 2000,
        body_length=2000,
    )
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="anything", level="warm"))
    rendered = out["sectioned_rendered"]

    assert ("z" * 600) in rendered
    assert ("z" * 601) not in rendered


def test_config_override_changes_warm_excerpt_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.config.schema import Config

    cfg = Config()
    cfg.memory.search.warm_excerpt_chars = 100
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)

    hit = SectionedHit(
        uri="memory/episodic/e1", type="episodic", path="",
        score=1.0, snippet="short headline", summary="z" * 2000,
        body_length=2000,
    )
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="anything", level="warm"))
    rendered = out["sectioned_rendered"]

    assert ("z" * 100) in rendered
    assert ("z" * 101) not in rendered


# ---------------------------------------------------------------------------
# (b) twelve ~1000-char hits with warm_max_chars=4000: every hit still
# appears, section order preserved, memory.recall carries rendered_chars.
# ---------------------------------------------------------------------------


def test_warm_response_budget_headline_fallback_when_twelve_hits_exceed_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.config.schema import Config

    cfg = Config()
    cfg.memory.search.warm_max_chars = 4000
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)

    hits = [
        SectionedHit(
            uri=f"memory/episodic/e{i}", type="episodic", path="",
            score=1.0 - i * 0.01, ts="2026-05-20",
            snippet=f"headline {i}", summary="y" * 1000, body_length=1000,
        )
        for i in range(12)
    ]
    canonical = SectionedHit(
        uri="person:marcelo", type="entity", path="",
        score=2.0, ts="", snippet="marcelo", summary="Marcelo Marmol.",
        body_length=15,
    )
    events = _capture_recall(monkeypatch)
    _stub_pipeline(monkeypatch, [canonical, *hits])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="anything", level="warm", limit=13),
    )
    rendered = out["sectioned_rendered"]

    for i in range(12):
        assert f"memory/episodic/e{i}" in rendered
    assert "person:marcelo" in rendered
    assert "drill for the body" in rendered
    # Section order preserved: canonical still leads fragment even
    # though both sections carry hits past the budget.
    assert rendered.index("=== CANONICAL:") < rendered.index("## Fragment")

    payload = [p for t, p in events if t == "memory.recall"][0]
    assert payload["rendered_chars"] == len(rendered)


def test_recall_event_carries_rendered_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = SectionedHit(
        uri="memory/episodic/e1", type="episodic", path="",
        score=1.0, snippet="short", summary="a short body",
        body_length=12,
    )
    events = _capture_recall(monkeypatch)
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="anything", level="warm"))

    payload = [p for t, p in events if t == "memory.recall"][0]
    assert payload["rendered_chars"] == len(out["sectioned_rendered"])
