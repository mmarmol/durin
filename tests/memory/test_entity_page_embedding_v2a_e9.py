"""E9 (audit second pass, 2026-05-28): v2.a embedding text for
entity pages now includes `rendered_frontmatter` between aliases
and body.

Concrete benefit: queries like "Marcelo's email" / "who is
Marcelo's spouse" hit the entity page centroide via the rendered
attributes/relations, instead of depending on the body prose
mentioning the attribute key.

v2.b (entities_with_aliases for memory entries) is NOT shipped —
the entity-aware ranker (audit A1) already covers alias matching
at query time without inflating the embedding text.
"""

from __future__ import annotations

from pathlib import Path

from durin.memory.entity_page import EntityPage
from durin.memory.vector_index import VectorIndex


def _make_page() -> EntityPage:
    return EntityPage(
        type="person",
        name="Marcelo",
        aliases=["Marcelo Marmol", "马塞洛"],
        attributes={
            "email": "marcelo@mxhero.com",
            "current_residence": "Spain",
        },
        relations=[
            {"to": "person:susana", "type": "spouse", "since": "2010"},
        ],
        body="Marcelo builds memory systems.",
    )


def test_compose_includes_rendered_attributes() -> None:
    page = _make_page()
    text = VectorIndex._compose_entity_page_text(
        name=page.name,
        aliases=list(page.aliases),
        attributes=dict(page.attributes),
        relations=list(page.relations),
        body=page.body,
    )
    assert "Email: marcelo@mxhero.com" in text
    assert "Current Residence: Spain" in text


def test_compose_includes_rendered_relations() -> None:
    page = _make_page()
    text = VectorIndex._compose_entity_page_text(
        name=page.name,
        aliases=list(page.aliases),
        attributes=dict(page.attributes),
        relations=list(page.relations),
        body=page.body,
    )
    # Relation type capitalised; target carried as URI when name not resolved.
    assert "Spouse:" in text
    assert "person:susana" in text or "susana" in text.lower()
    assert "since 2010" in text


def test_compose_preserves_v1_order_name_aliases_first() -> None:
    """Rendered frontmatter goes BETWEEN aliases and body — most
    distilled signal (name + aliases) still leads."""
    page = _make_page()
    text = VectorIndex._compose_entity_page_text(
        name=page.name,
        aliases=list(page.aliases),
        attributes=dict(page.attributes),
        relations=list(page.relations),
        body=page.body,
    )
    name_pos = text.index("Marcelo")
    aliases_pos = text.index("Aliases:")
    email_pos = text.index("Email:")
    body_pos = text.index("memory systems")
    assert name_pos < aliases_pos < email_pos < body_pos


def test_compose_empty_attributes_skips_frontmatter_section() -> None:
    """A page with no attributes/relations composes the same as v1
    (no empty section noise in the embedding)."""
    text = VectorIndex._compose_entity_page_text(
        name="Marcelo",
        aliases=["Marmol"],
        attributes={},
        relations=[],
        body="Builder.",
    )
    # Must not contain dangling labels or empty lines.
    assert "Email:" not in text
    assert "Spouse:" not in text
    assert text == "Marcelo\n\nAliases: Marmol\n\nBuilder."


def test_compose_skips_provenance_and_timestamps() -> None:
    """`provenance`, `dream_processed_through`, `created_at`,
    `updated_at` are internal metadata; never rendered (doc 02
    §4.2 v2 spec)."""
    text = VectorIndex._compose_entity_page_text(
        name="Marcelo",
        aliases=[],
        attributes={
            "email": "x@y.com",
            "created_at": "2024-01-01",
            "updated_at": "2024-12-01",
            "provenance": {"source": "absorb"},
            "dream_processed_through": "2024-12-01T00:00:00Z",
        },
        relations=[],
        body="b",
    )
    assert "Email: x@y.com" in text
    assert "created_at" not in text.lower() or "Created At:" not in text
    assert "provenance" not in text.lower() or "Provenance:" not in text


def test_compose_stateful_attribute_uses_current_only() -> None:
    """Stateful attributes have `{current: X, history: [...]}` —
    historical values must NOT enter the centroid (avoid drift toward
    defunct facts)."""
    text = VectorIndex._compose_entity_page_text(
        name="Marcelo",
        aliases=[],
        attributes={
            "role": {
                "current": "founder",
                "history": ["engineer", "manager"],
            },
        },
        relations=[],
        body="b",
    )
    assert "Role: founder" in text
    assert "engineer" not in text
    assert "manager" not in text


def test_rebuild_includes_entity_pages(tmp_path: Path) -> None:
    """E9: pre-existing gap — `rebuild_from_workspace` only walked
    `memory/<class>/*.md` entries, NOT `memory/entities/`. After
    the forced rebuild from schema_version bump, entity pages would
    disappear from the vector index until the next Dream/absorb.

    Fix: rebuild now walks entity pages and re-upserts each via
    `upsert_entity_page` so the index is complete post-rebuild.
    """
    from durin.memory.embedding import EmbeddingProvider

    class _FakeProvider(EmbeddingProvider):
        def embed(self, texts):
            return [[float(len(t))] * 4 for t in texts]

        def dimensions(self) -> int:
            return 4

        def model_name(self) -> str:
            return "fake-test-model"

    page = _make_page()
    page.save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")

    vi = VectorIndex(tmp_path, _FakeProvider())
    count = vi.rebuild_from_workspace()

    # No entries under memory/<class>; only one entity page.
    assert count == 1
    # Confirm the table has the entity_page row.
    rows = vi.search("Marcelo", top_k=10)
    types = {r.get("class_name") for r in rows}
    assert "entity_page" in types
