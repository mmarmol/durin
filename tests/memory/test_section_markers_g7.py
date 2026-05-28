"""G7 (audit fourth pass, 2026-05-28): a single source of truth for the
`=== KIND: ... ===` marker strings shared by the hot layer renderer
and the sectioned_output renderer.

Two renderers stay intentionally separate (per audit G6 closing the
F4 unification question — different responsibilities, different
internal content). But they both wrap their output in the same
marker convention defined in `docs/memory/06_prompts_and_instructions.md`
§8.3. Pre-G7 each module built the marker strings independently —
two places to drift. G7 ships a small `section_markers` helper they
both call, eliminating the drift surface without forcing the two
renderers to merge their body logic.
"""

from __future__ import annotations


def test_canonical_marker_with_ts() -> None:
    from durin.memory.section_markers import canonical_marker

    assert canonical_marker(
        "person:marcelo", ts="2026-05-23",
    ) == "=== CANONICAL: person:marcelo (consolidated 2026-05-23) ==="


def test_canonical_marker_without_ts_uses_canonical_label() -> None:
    """When no timestamp is present (entity pages never carry
    `valid_from`), the marker swaps `(consolidated <ts>)` for the
    descriptive `(canonical entity page)` per audit F4."""
    from durin.memory.section_markers import canonical_marker

    assert canonical_marker(
        "person:marcelo", ts="",
    ) == "=== CANONICAL: person:marcelo (canonical entity page) ==="


def test_fragment_marker_with_ts() -> None:
    from durin.memory.section_markers import fragment_marker

    assert fragment_marker(
        "memory/episodic/abc123", ts="2026-05-23",
    ) == "=== FRAGMENT: memory/episodic/abc123 (ts 2026-05-23) ==="


def test_fragment_marker_without_ts() -> None:
    from durin.memory.section_markers import fragment_marker

    assert fragment_marker(
        "memory/stable/x", ts="",
    ) == "=== FRAGMENT: memory/stable/x ==="


def test_session_marker_with_and_without_ts() -> None:
    from durin.memory.section_markers import session_marker

    assert session_marker(
        "sessions/abc#turn-1", ts="2026-05-23",
    ) == "=== SESSION: sessions/abc#turn-1 (ts 2026-05-23) ==="
    assert session_marker(
        "sessions/abc#turn-1", ts="",
    ) == "=== SESSION: sessions/abc#turn-1 ==="


def test_ingested_marker_uses_ingest_id_prefix() -> None:
    from durin.memory.section_markers import ingested_marker

    assert ingested_marker(
        "doc-a", "chunk-2",
    ) == "=== INGESTED: doc-a/chunk-2 ==="


def test_ingested_marker_unknown_id_fallback() -> None:
    """Convention: when no ingest_id is available the marker falls
    back to `unknown` so the LLM still sees a marker structure."""
    from durin.memory.section_markers import ingested_marker

    assert ingested_marker(
        None, "chunk-2",
    ) == "=== INGESTED: unknown/chunk-2 ==="


def test_end_marker_uppercases_kind() -> None:
    from durin.memory.section_markers import end_marker

    assert end_marker("canonical") == "=== END CANONICAL ==="
    assert end_marker("fragment") == "=== END FRAGMENT ==="
    assert end_marker("session") == "=== END SESSION ==="
    assert end_marker("ingested") == "=== END INGESTED ==="


def test_sectioned_output_uses_shared_helper() -> None:
    """`sectioned_output._marker_for` must delegate to the shared
    helper so any future change to marker format flows through one
    code path."""
    import inspect

    from durin.memory import section_markers as sm
    from durin.memory.sectioned_output import _marker_for, SectionedHit

    src = inspect.getsource(_marker_for)
    assert (
        "section_markers" in src
        or "canonical_marker" in src
        or "fragment_marker" in src
    ), (
        "_marker_for should reference the shared section_markers "
        "helper to avoid drift; saw source: " + src[:200]
    )

    # Behavioural check via the public function still matches.
    hit = SectionedHit(
        uri="person:marcelo", type="entity",
        path="entities/person/marcelo.md", score=1.0, ts="",
        summary="x",
    )
    assert _marker_for("canonical", hit) == sm.canonical_marker(
        "person:marcelo", ts="",
    )


def test_hot_layer_canonical_block_uses_shared_helper() -> None:
    """`hot_layer._render_canonical_block` must produce the same
    `=== CANONICAL: ===` header the shared helper produces."""
    from durin.memory.entity_page import EntityPage
    from durin.memory.hot_layer import _render_canonical_block
    from durin.memory.section_markers import canonical_marker

    page = EntityPage(
        type="person", name="Marcelo", aliases=["m"], body="b",
    )
    out = _render_canonical_block(
        "person:marcelo", page,
        consolidated_ts="2026-05-23T18:00",
    )
    expected_header = canonical_marker(
        "person:marcelo", ts="2026-05-23T18:00",
    )
    assert out.splitlines()[0] == expected_header


def test_hot_layer_fragment_block_uses_shared_helper() -> None:
    """`hot_layer._render_fragment_block` must produce the same
    `=== FRAGMENT: ===` header the shared helper produces."""
    from datetime import date
    from types import SimpleNamespace
    from durin.memory.hot_layer import _render_fragment_block
    from durin.memory.section_markers import fragment_marker

    entry = SimpleNamespace(
        body="b", summary="", headline="h",
        valid_from=date(2026, 5, 23),
    )
    out = _render_fragment_block(entry, rel_path="memory/episodic/abc")
    expected_header = fragment_marker(
        "memory/episodic/abc", ts="2026-05-23",
    )
    assert out.splitlines()[0] == expected_header
