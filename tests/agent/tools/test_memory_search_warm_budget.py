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
    `_effective_summary`. `path` carries the on-disk path WITH its `.md`
    suffix, the shape `_resolve_meta` actually produces (`vh.get("path")`
    off the vector row, itself `str(rel_path)` of the `.md` file) — a bare
    `path=""` here would silently skip the `.md`-doubling bug `_enrich_body`
    had at cold level, since `_sectioned_to_result` would then fall back to
    `hit.uri` (which never carries `.md`) instead."""
    write_session_summary(
        workspace, session_key, text, last_active="2026-05-20T10:00:00Z",
    )
    key = sanitize_session_key(session_key)
    uri = f"memory/session_summary/{key}"
    return SectionedHit(
        uri=uri, type="session_summary",
        path=f"memory/session_summary/{key}.md", score=1.0,
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


def test_cold_level_session_turn_hit_resolves_its_md_path_without_doubling(
    tmp_path: Path,
) -> None:
    """The same `.md`-doubling class of bug fixed above for
    `session_summary` also reaches a raw `session` (turn) hit, via a
    different failure mode: its `path` is `sessions/<key>.md` — ONE path
    segment, unlike `memory/<class>/<id>.md`'s two — so the pre-fix
    `r.uri.split("/", 2)` raised `ValueError` (only 2 parts) and gave up
    before ever building a wrong path. `_enrich_body`'s `uri.endswith
    (".md")` branch resolves this shape directly instead of re-deriving
    one, so it fixes this case too.

    A real `sessions/<key>.md` transcript has no YAML frontmatter (see
    `session_md.render_session_md` — it opens with a bare `# Session
    <key>` heading), so `load_entry` could never parse one regardless of
    this fix — that gap is pre-existing and untouched here. This test
    writes a `save_entry`-shaped stand-in at that same single-segment
    path so it isolates the path-resolution fix from that unrelated
    format gap, exercised directly through `_sectioned_to_result` (the
    call site `_enrich_body` fires from) rather than the full `execute()`
    pipeline.
    """
    from durin.memory.schema import MemoryEntry
    from durin.memory.storage import save_entry

    key = "cli_turn-test"
    text = "the full raw turn text a cold-level drill should return"
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    save_entry(
        MemoryEntry(id=key, headline="turn", body=text),
        sessions_dir / f"{key}.md",
    )

    hit = SectionedHit(
        uri=f"sessions/{key}.md", type="session",
        path=f"sessions/{key}.md", score=1.0, ts="2026-05-20",
        snippet="turn snippet", body_length=len(text),
    )
    tool = MemorySearchTool(workspace=tmp_path)
    result = tool._sectioned_to_result(
        hit, level="cold", cache={}, warm_excerpt_chars=600,
    )

    assert result is not None
    assert result.body == text


# ---------------------------------------------------------------------------
# (c) reference hits also respect `warm_excerpt_chars` at warm level and
# show the whole chunk at cold — `_attach_reference_bodies` previously
# hardcoded a 600-char cut at every level, so cold never showed more than
# warm did for the Library, the same class of bug C1 fixed for
# session_summary.
# ---------------------------------------------------------------------------


def _make_reference_chunk_text(min_chars: int = 1200) -> str:
    """A single-chunk (under the 384-token structural cap) reference body
    with no leading heading/metadata line, so `strip_scraped_boilerplate`
    passes it through unchanged and the chunk's `text` field round-trips
    verbatim — verified empirically (`ingest_reference` + `reference_chunks`
    on this exact shape produced one chunk, `len(chunk["text"]) ==
    len(text)`) before writing the assertions below, not guessed."""
    sentence = "The uroabdomen protocol needs careful monitoring of vitals."
    text = sentence
    while len(text) < min_chars:
        text += " " + sentence
    return text


def _reference_hit(slug: str, idx: int) -> SectionedHit:
    """The pipeline-shaped hit a real search would produce for a reference
    chunk: fusion uri `reference:<slug>#<idx>` (no `memory/` prefix —
    `_sectioned_to_result` adds it), matching `_attach_reference_bodies`'s
    own prefix-stripping chain."""
    return SectionedHit(
        uri=f"reference:{slug}#{idx}", type="reference", path="",
        score=1.0, snippet="a reference chunk",
    )


def test_warm_level_cuts_reference_hit_to_warm_excerpt_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.config.schema import Config
    from durin.memory.reference import ingest_reference

    cfg = Config()
    cfg.memory.search.warm_excerpt_chars = 100
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)

    text = _make_reference_chunk_text()
    res = ingest_reference(tmp_path, "ref-warm-budget", text)
    slug = res.ref.split(":", 1)[1]
    hit = _reference_hit(slug, 0)
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="anything", scope="library", level="warm"),
    )
    rendered = out["sectioned_rendered"]

    assert text[:100] in rendered
    assert text[:101] not in rendered
    assert f"preview 100/{len(text)}" in rendered


def test_cold_level_shows_the_whole_reference_chunk_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from durin.memory.reference import ingest_reference

    text = _make_reference_chunk_text()
    res = ingest_reference(tmp_path, "ref-cold-budget", text)
    slug = res.ref.split(":", 1)[1]
    hit = _reference_hit(slug, 0)
    _stub_pipeline(monkeypatch, [hit])

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(
        tool.execute(query="anything", scope="library", level="cold"),
    )
    rendered = out["sectioned_rendered"]

    assert text in rendered
    assert "(complete)" in rendered


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
    # The size bound holds end-to-end through the tool, not just at the
    # renderer unit level (`test_sectioned_output.py`'s own
    # `test_twelve_hits_all_represented_under_tight_budget` covers that) —
    # the budget plus a generous per-hit allowance for the 13 hits that
    # could each degrade to a headline-pointer line.
    assert len(rendered) <= 4000 + 13 * 80

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
