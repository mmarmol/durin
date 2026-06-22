"""Tests for the hot layer v2 rendering format.

Covers exact marker format, v2 entity fields rendered as prose, intro
sentences locked, and telemetry failure event wiring. Intentionally tight on
string assertions: the markers are verbatim and the prompts are LLM-facing —
drift needs to break loudly.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from durin.memory.entity_page import EntityPage
from durin.memory.hot_layer import (
    _CANONICAL_BUDGET_CHARS,
    _ENTITIES_BUDGET_CHARS,
    _FRAGMENTS_BUDGET_CHARS,
    _HEADLINES_BUDGET_CHARS,
    _IDENTITY_BUDGET_CHARS,
    _MAX_CANONICAL,
    _MAX_ENTITIES,
    _MAX_FRAGMENTS,
    _MAX_HEADLINES,
    read_hot_layer,
)
from durin.memory.store import store_memory
from durin.telemetry.logger import TelemetryLogger, bind_telemetry, reset_telemetry

# ---------------------------------------------------------------------------
# Budgets — spec values locked
# ---------------------------------------------------------------------------


def test_budget_constants_match_spec_8_2() -> None:
    """Locks the five budget chars + four caps."""
    assert _IDENTITY_BUDGET_CHARS == 800
    assert _CANONICAL_BUDGET_CHARS == 2400
    assert _FRAGMENTS_BUDGET_CHARS == 1200
    assert _HEADLINES_BUDGET_CHARS == 1200
    assert _ENTITIES_BUDGET_CHARS == 600
    assert _MAX_CANONICAL == 12
    assert _MAX_FRAGMENTS == 8
    assert _MAX_HEADLINES == 12
    assert _MAX_ENTITIES == 50


# ---------------------------------------------------------------------------
# Section rendering — H2 + intro sentences + markers
# ---------------------------------------------------------------------------


def _write_v1_page(workspace: Path, *, slug: str, name: str,
                   updated_at: str = "2026-05-20T10:00:00") -> Path:
    page = EntityPage(
        type="person",
        name=name,
        aliases=[],
        body="Body content for " + name + ".",
        updated_at=datetime.fromisoformat(updated_at),
    )
    target = workspace / "memory" / "entities" / "person" / f"{slug}.md"
    page.save(target)
    return target


def test_canonical_marker_format_includes_consolidated_ts(tmp_path: Path) -> None:
    """`=== CANONICAL: <uri> (consolidated <ts>) ===` literal marker format."""
    _write_v1_page(tmp_path, slug="marcelo", name="Marcelo",
                   updated_at="2026-05-20T10:00:00")
    rendered = read_hot_layer(tmp_path).render()
    assert "=== CANONICAL: person:marcelo (consolidated 2026-05-20T10:00:00) ===" in rendered


def test_canonical_intro_sentence_present(tmp_path: Path) -> None:
    """Intro line cues the LLM to treat canonical as truth."""
    _write_v1_page(tmp_path, slug="marcelo", name="Marcelo")
    rendered = read_hot_layer(tmp_path).render()
    assert "## Memory: Canonical pages" in rendered
    assert (
        "These are the authoritative records — fragments below "
        "amend them with newer information."
    ) in rendered


def test_fragment_marker_format_includes_path_and_ts(tmp_path: Path) -> None:
    """`=== FRAGMENT: <path> (ts <ts>) ===` literal marker format.

    The page anchor makes the fragment post-cursor (cursor None → all
    entries qualify).
    """
    _write_v1_page(tmp_path, slug="marcelo", name="Marcelo")
    store_memory(
        tmp_path,
        content="Marcelo bought a new bike yesterday.",
        headline="bike",
        valid_from=date(2026, 5, 26),
        entities=["person:marcelo"],
    )
    rendered = read_hot_layer(tmp_path).render()
    assert "=== FRAGMENT:" in rendered
    # Path is relative to workspace, starts with memory/episodic/
    assert "memory/episodic/" in rendered
    # ts is the entry's valid_from / iso timestamp
    assert "(ts 2026-05-26" in rendered


def test_fragment_intro_sentence_present(tmp_path: Path) -> None:
    """Intro line tells the LLM to reconcile fragments by timestamp."""
    _write_v1_page(tmp_path, slug="marcelo", name="Marcelo")
    store_memory(
        tmp_path,
        content="recent fragment",
        entities=["person:marcelo"],
    )
    rendered = read_hot_layer(tmp_path).render()
    assert "## Memory: Recent fragments" in rendered
    assert (
        "Recent episodic entries — raw memories that may carry newer "
        "info than the canonical above. Reconcile using the "
        "timestamps."
    ) in rendered


# ---------------------------------------------------------------------------
# Cursor logic — only post-cursor episodic/stable; corpus + pending out
# ---------------------------------------------------------------------------


def test_fragments_include_stable_class(tmp_path: Path) -> None:
    """Both ``episodic`` AND ``stable`` qualify as hot-layer fragments."""
    _write_v1_page(tmp_path, slug="marcelo", name="Marcelo")
    store_memory(
        tmp_path,
        class_name="stable",
        content="A stable fact entry about Marcelo.",
        headline="STABLE_HEADLINE_TOKEN",
        entities=["person:marcelo"],
    )
    layer = read_hot_layer(tmp_path)
    # Stable entries should appear as fragments
    fragments_text = "\n".join(layer.fragment_blocks)
    assert "STABLE_HEADLINE_TOKEN" in fragments_text or "stable fact" in fragments_text


def test_fragments_exclude_corpus_class(tmp_path: Path) -> None:
    """Corpus entries never surface as hot-layer fragments."""
    _write_v1_page(tmp_path, slug="marcelo", name="Marcelo")
    store_memory(
        tmp_path,
        class_name="corpus",
        content="A corpus document chunk about something.",
        headline="CORPUS_HEADLINE_TOKEN",
        entities=["person:marcelo"],
    )
    layer = read_hot_layer(tmp_path)
    fragments_text = "\n".join(layer.fragment_blocks)
    assert "CORPUS_HEADLINE_TOKEN" not in fragments_text


# ---------------------------------------------------------------------------
# Failure handling — telemetry + degraded prompt
# ---------------------------------------------------------------------------


def test_hot_layer_failure_emits_telemetry_and_continues(
    tmp_path: Path,
) -> None:
    """Disk error → telemetry event + degraded (empty) layer, no raise."""
    # Bind a fresh telemetry logger to a temp file so we can inspect events.
    log_path = tmp_path / "telemetry.jsonl"
    logger = TelemetryLogger(log_path)
    token = bind_telemetry(logger)
    try:
        # Force a failure in _read_canonical_blocks via patch.
        with patch(
            "durin.memory.hot_layer._read_canonical_blocks",
            side_effect=OSError("simulated disk failure"),
        ):
            layer = read_hot_layer(tmp_path)
        # Did NOT raise — degraded layer is fine.
        assert layer.canonical_blocks == []
        # The render should still produce something or nothing, but not crash.
        rendered = layer.render()
        assert isinstance(rendered, str)
    finally:
        reset_telemetry(token)

    # Inspect the telemetry log for the failure event.
    import json
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    failure_events = [e for e in events if e["type"] == "memory.hot_layer.failure"]
    assert failure_events, f"expected memory.hot_layer.failure event, got: {events}"
    event = failure_events[0]
    assert event["data"]["component"] == "canonical_blocks"
    assert "simulated disk failure" in event["data"]["error"]


def test_hot_layer_failure_per_page_isolated(tmp_path: Path) -> None:
    """A single un-parseable entity page does not break the whole layer.

    Writes one good page + one with malformed YAML; the good one still
    surfaces.
    """
    _write_v1_page(tmp_path, slug="marcelo", name="Marcelo")
    bad_path = tmp_path / "memory" / "entities" / "person" / "broken.md"
    bad_path.write_text(
        "---\nthis is :: not :: valid :: yaml :: :\n---\n\nbody\n",
        encoding="utf-8",
    )
    layer = read_hot_layer(tmp_path)
    rendered = layer.render()
    # Good page surfaced.
    assert "person:marcelo" in rendered
    # Bad page silently skipped — no exception, no traceback in output.
    assert "broken" not in rendered.lower() or "person:broken" not in rendered


# ---------------------------------------------------------------------------
# v2 entity-page rendering — attributes + relations as prose
# ---------------------------------------------------------------------------


def _write_v2_page(workspace: Path) -> Path:
    page = EntityPage(
        type="person",
        name="Marcelo",
        aliases=["Marcelo Marmol", "marcelo"],
        body="Body about Marcelo.",
        updated_at=datetime.fromisoformat("2026-05-25T10:00:00"),
        attributes={
            "email": "marcelo@mxhero.com",
            "current_residence": "Spain",
        },
        relations=[
            {"to": "person:susana", "type": "spouse", "since": 2010},
            {"to": "project:durin", "type": "maintains"},
        ],
    )
    target = workspace / "memory" / "entities" / "person" / "marcelo.md"
    page.save(target)
    return target


def test_v2_page_renders_aliases_as_prose(tmp_path: Path) -> None:
    _write_v2_page(tmp_path)
    rendered = read_hot_layer(tmp_path).render()
    assert "(aliases: Marcelo Marmol, marcelo)" in rendered


def test_v2_page_renders_attributes_as_prose(tmp_path: Path) -> None:
    _write_v2_page(tmp_path)
    rendered = read_hot_layer(tmp_path).render()
    # Prose form, not YAML dump.
    assert "Attributes:" in rendered
    assert "email is marcelo@mxhero.com" in rendered
    assert "current_residence is Spain" in rendered
    # No raw YAML hint:
    assert "attributes:\n" not in rendered
    assert "{'email'" not in rendered


def test_v2_page_renders_relations_as_prose(tmp_path: Path) -> None:
    _write_v2_page(tmp_path)
    rendered = read_hot_layer(tmp_path).render()
    assert "Relations:" in rendered
    assert "spouse of person:susana" in rendered
    assert "maintains project:durin" in rendered


def test_v2_page_empty_attributes_no_prose_line(tmp_path: Path) -> None:
    """When attributes is empty, no 'Attributes:' line is rendered."""
    page = EntityPage(
        type="person",
        name="Marcelo",
        body="body",
        updated_at=datetime.fromisoformat("2026-05-20T10:00:00"),
        relations=[{"to": "project:durin", "type": "maintains"}],
    )
    page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    rendered = read_hot_layer(tmp_path).render()
    assert "Attributes:" not in rendered
    assert "Relations:" in rendered


def test_v2_page_empty_relations_no_prose_line(tmp_path: Path) -> None:
    """When relations is empty, no 'Relations:' line is rendered."""
    page = EntityPage(
        type="person",
        name="Marcelo",
        body="body",
        updated_at=datetime.fromisoformat("2026-05-20T10:00:00"),
        attributes={"email": "x@y.com"},
    )
    page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    rendered = read_hot_layer(tmp_path).render()
    assert "Attributes:" in rendered
    assert "Relations:" not in rendered


def test_v1_page_still_renders_without_attributes_or_relations(
    tmp_path: Path,
) -> None:
    """A v1 page (no v2 fields) renders cleanly — no empty Attributes/Relations."""
    _write_v1_page(tmp_path, slug="marcelo", name="Marcelo")
    rendered = read_hot_layer(tmp_path).render()
    assert "person:marcelo" in rendered
    assert "Attributes:" not in rendered
    assert "Relations:" not in rendered


def test_v2_page_empty_aliases_no_aliases_prose(tmp_path: Path) -> None:
    """Aliases [] → no 'aliases: ' rendered."""
    page = EntityPage(
        type="person",
        name="Marcelo",
        body="body",
        updated_at=datetime.fromisoformat("2026-05-20T10:00:00"),
        attributes={"email": "x@y.com"},
    )
    page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    rendered = read_hot_layer(tmp_path).render()
    assert "aliases:" not in rendered


# ---------------------------------------------------------------------------
# Done criterion — exact snapshot of a v2 canonical block
# ---------------------------------------------------------------------------


def test_v2_canonical_block_exact_snapshot(tmp_path: Path) -> None:
    """Lock the rendered canonical block byte-for-byte.

    A regression here means the LLM-facing prose changed. Update only
    after deliberate spec review.
    """
    _write_v2_page(tmp_path)
    layer = read_hot_layer(tmp_path)
    assert len(layer.canonical_blocks) == 1
    block = layer.canonical_blocks[0]
    expected = (
        "=== CANONICAL: person:marcelo (consolidated 2026-05-25T10:00:00) ===\n"
        "Marcelo (aliases: Marcelo Marmol, marcelo).\n"
        "Attributes: email is marcelo@mxhero.com; current_residence is Spain.\n"
        "Relations: spouse of person:susana (since 2010); maintains project:durin.\n"
        "Body about Marcelo.\n"
        "=== END CANONICAL ==="
    )
    assert block == expected
