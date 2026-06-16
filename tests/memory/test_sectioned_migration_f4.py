"""F4 (audit third pass, 2026-05-28): complete Phase 3 sectioned
rendering migration.

Before F4 we shipped Phase 3 building blocks (`query_router`, `RRF`,
`sectioned_output`, `lexical_executor`) but kept the legacy
`Result.render_block` wiring in `memory_search`. Result: per-source
cap (doc 03 §12.4) never activated; section intros (doc 03 §12)
never reached the LLM; two parallel renderers (`Result.render_block`
in search.py vs `sectioned_output._render_block`) emitted different
formats.

F4 closes the gap:
- `sectioned_output._render_block` reaches feature parity with the
  legacy (END marker, summary > body > snippet preference, entities
  tail, `(canonical entity page)` hint when no ts).
- `SectionedHit` carries `summary` and `entities` so the renderer
  has the data the legacy used.
- `memory_search.execute` builds SectionedHit list, applies
  `apply_per_source_cap`, calls `render_sectioned`. Response
  exposes the single `sectioned_rendered` string; per-row `rendered`
  field dropped (WebUI doesn't consume it; LLM gets the sectioned
  string).
"""

from __future__ import annotations

import asyncio
from pathlib import Path


def test_render_block_has_end_marker() -> None:
    """Every block carries an `=== END KIND ===` close so the LLM can
    boundary-detect without relying on section intros.
    """
    from durin.memory.sectioned_output import (
        SectionedHit,
        _render_block,
    )

    hit = SectionedHit(
        uri="person:marcelo", type="entity", path="entities/person/marcelo.md",
        score=1.0, ts="", snippet="excerpt",
        summary="Marcelo Marmol — builder of memory systems.",
    )
    out = _render_block("canonical", hit)
    assert out.startswith(
        "=== CANONICAL: person:marcelo (canonical entity page) ==="
    )
    assert out.endswith("=== END CANONICAL ===")


def test_render_block_canonical_uses_ts_when_present() -> None:
    """When ``hit.ts`` is set, the canonical header carries
    `(consolidated <ts>)` instead of `(canonical entity page)`."""
    from durin.memory.sectioned_output import (
        SectionedHit,
        _render_block,
    )

    hit = SectionedHit(
        uri="person:marcelo", type="entity",
        path="entities/person/marcelo.md",
        score=1.0, ts="2026-05-23",
        summary="x",
    )
    out = _render_block("canonical", hit)
    assert "(consolidated 2026-05-23)" in out


def test_render_block_prefers_summary_over_snippet() -> None:
    """Body inside the block is `summary > body > snippet`."""
    from durin.memory.sectioned_output import (
        SectionedHit,
        _render_block,
    )

    hit = SectionedHit(
        uri="memory/episodic/e1", type="episodic",
        path="memory/episodic/e1.md",
        score=1.0, ts="2026-05-23",
        snippet="SNIPPET-tail",
        body="full body",
        summary="HEADLINE summary",
    )
    out = _render_block("fragment", hit)
    assert "HEADLINE summary" in out
    # The snippet must not leak into the rendered block when summary
    # exists — would duplicate context for the LLM.
    assert "SNIPPET-tail" not in out


def test_render_block_falls_back_to_body_then_snippet() -> None:
    from durin.memory.sectioned_output import (
        SectionedHit,
        _render_block,
    )

    no_summary = SectionedHit(
        uri="u", type="episodic", path="p.md",
        score=1.0, ts="t",
        body="body-here", snippet="snippet-here",
    )
    out = _render_block("fragment", no_summary)
    assert "body-here" in out
    assert "snippet-here" not in out

    only_snippet = SectionedHit(
        uri="u", type="episodic", path="p.md",
        score=1.0, ts="t", snippet="just-snippet",
    )
    out = _render_block("fragment", only_snippet)
    assert "just-snippet" in out


def test_render_block_entities_tail_for_fragments(
) -> None:
    """Fragments carry an `Entities: ...` tail so the LLM can drill
    to canonical pages. Canonical blocks skip it (the URI IS the
    entity ref)."""
    from durin.memory.sectioned_output import (
        SectionedHit,
        _render_block,
    )

    frag = SectionedHit(
        uri="memory/episodic/e1", type="episodic",
        path="memory/episodic/e1.md",
        score=1.0, ts="2026-05-23",
        summary="something",
        entities=("person:marcelo", "topic:pytest"),
    )
    out = _render_block("fragment", frag)
    assert "Entities: person:marcelo, topic:pytest" in out

    canon = SectionedHit(
        uri="person:marcelo", type="entity",
        path="entities/person/marcelo.md",
        score=1.0, ts="",
        summary="Marcelo",
        entities=("person:marcelo",),
    )
    out = _render_block("canonical", canon)
    # Canonical: no entities tail (URI is the entity).
    assert "Entities:" not in out


def test_memory_search_response_has_sectioned_rendered(
    tmp_path: Path,
) -> None:
    """`memory_search` post-F4 returns a single `sectioned_rendered`
    string with section intros + per-block markers + END closes.
    Per-row `rendered` field is gone (WebUI uses raw fields; LLM uses
    the sectioned string)."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.entity_page import EntityPage
    from durin.memory.indexer import rebuild_fts_index

    EntityPage(
        type="person", name="Marcelo",
        aliases=["marcelo"], body="b",
    ).save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(tmp_path)

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="Marcelo"))

    assert "sectioned_rendered" in out
    assert isinstance(out["sectioned_rendered"], str)
    assert out["sectioned_rendered"]  # non-empty
    # Section intros + markers + END close present.
    assert "=== CANONICAL: " in out["sectioned_rendered"]
    assert "=== END CANONICAL ===" in out["sectioned_rendered"]
    # Per-row `rendered` dropped.
    for r in out["results"]:
        assert "rendered" not in r


def test_sectioned_rendered_includes_section_intros(
    tmp_path: Path,
) -> None:
    """The sectioned renderer surfaces Phase 3 section intros
    ("Consolidated entity pages — the main memory; ..."). Pre-F4 the
    intros never reached the LLM because `memory_search` rendered
    per-row via `Result.render_block`."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.memory.entity_page import EntityPage
    from durin.memory.indexer import rebuild_fts_index

    EntityPage(
        type="person", name="Marcelo",
        aliases=["marcelo"], body="b",
    ).save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(tmp_path)

    tool = MemorySearchTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(query="Marcelo"))

    rendered = out["sectioned_rendered"]
    # Section header (markdown style) AND intro text from
    # _SECTION_INTRO map.
    assert "## Canonical" in rendered
    assert "Consolidated entity pages" in rendered
