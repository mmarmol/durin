"""Tests for `durin.memory.absorption` — entity merge + archive."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.absorption import (
    AbsorptionError,
    EntityAbsorption,
)
from durin.memory.aliases_index import AliasIndex
from durin.memory.embedding import EmbeddingProvider
from durin.memory.entity_page import EntityPage
from durin.memory.vector_index import VectorIndex, vector_index_available


def _write_page(
    workspace: Path,
    type_: str,
    slug: str,
    *,
    name: str | None = None,
    aliases: list[str] | None = None,
    body: str = "",
    extra: dict | None = None,
) -> Path:
    page = EntityPage(
        type=type_,
        name=name or slug,
        aliases=aliases or [],
        body=body,
        extra=extra or {},
    )
    path = workspace / "memory" / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


# ---------------------------------------------------------------------------
# find_candidates
# ---------------------------------------------------------------------------


class TestFindCandidates:
    def test_no_collisions_returns_empty(self, tmp_path: Path) -> None:
        _write_page(tmp_path, "person", "marcelo", aliases=["Marcelo"])
        _write_page(tmp_path, "project", "durin", aliases=["Durin"])
        absorber = EntityAbsorption(tmp_path)
        # Build the alias index it consults
        absorber._get_alias_index().build()
        assert absorber.find_candidates() == []

    def test_overlapping_aliases_yield_candidate(self, tmp_path: Path) -> None:
        _write_page(
            tmp_path, "person", "marcelo_marmol",
            name="Marcelo Marmol", aliases=["Marcelo", "marcelo"],
        )
        _write_page(
            tmp_path, "person", "marcelo_diaz",
            name="Marcelo Diaz", aliases=["Marcelo", "marcelo"],
        )
        absorber = EntityAbsorption(tmp_path)
        absorber._get_alias_index().build()
        candidates = absorber.find_candidates()
        assert len(candidates) == 1
        c = candidates[0]
        assert set(c.refs) == {"person:marcelo_marmol", "person:marcelo_diaz"}
        # "marcelo" is the lowercase-folded key in the index
        assert "marcelo" in c.shared_aliases

    def test_more_overlap_ranks_first(self, tmp_path: Path) -> None:
        """Stronger signal (more shared aliases) sorts first."""
        # Pair A — share 2 aliases
        _write_page(
            tmp_path, "person", "a_full",
            name="A Full", aliases=["A", "A.F"],
        )
        _write_page(
            tmp_path, "person", "a_other",
            name="A Other", aliases=["A", "A.F"],
        )
        # Pair B — share 1 alias
        _write_page(
            tmp_path, "person", "b_full",
            name="B Full", aliases=["Bz"],
        )
        _write_page(
            tmp_path, "person", "b_other",
            name="B Other", aliases=["Bz"],
        )
        absorber = EntityAbsorption(tmp_path)
        absorber._get_alias_index().build()
        cs = absorber.find_candidates()
        assert len(cs) == 2
        # First candidate has more shared aliases
        assert len(cs[0].shared_aliases) >= len(cs[1].shared_aliases)


# ---------------------------------------------------------------------------
# absorb
# ---------------------------------------------------------------------------


class TestAbsorb:
    def test_absorbs_aliases_and_body(self, tmp_path: Path) -> None:
        canonical_path = _write_page(
            tmp_path, "person", "marcelo",
            name="Marcelo Marmol",
            aliases=["Marcelo"],
            body="## Background\nFounder of mxhero.\n",
            extra={"identifiers": ["mmarmol@mxhero.com"]},
        )
        absorbed_path = _write_page(
            tmp_path, "person", "marcelo_m",
            name="Marcelo M.",
            aliases=["Marcelo"],  # overlap
            body="## Notes\nObserved in slack as Marcelo M.\n",
            extra={"identifiers": ["UM7TCSZRN"]},
        )

        absorber = EntityAbsorption(tmp_path)
        sha = absorber.absorb(
            "person:marcelo",
            "person:marcelo_m",
            reason="alias overlap confirmed",
        )
        assert sha is not None and len(sha) == 40

        # Canonical updated: new aliases + body section
        merged = EntityPage.from_file(canonical_path)
        assert merged is not None
        assert "Marcelo" in merged.aliases
        # Absorbed's display name becomes an alias of the canonical
        assert "Marcelo M." in merged.aliases
        assert "Absorbed from person:marcelo_m" in merged.body
        assert "Observed in slack" in merged.body

        # Identifiers union
        identifiers = merged.extra.get("identifiers", [])
        assert "mmarmol@mxhero.com" in identifiers
        assert "UM7TCSZRN" in identifiers

        # Absorbed file: gone from original location, present in top-level
        # archive (Phase 0 deliverable 5 — `memory/archive/entities/<type>/<slug>.md`).
        assert not absorbed_path.exists()
        archived = (
            tmp_path / "memory" / "archive" / "entities" / "person"
            / "marcelo_m.md"
        )
        assert archived.exists()
        # Absorbed page carries traceability frontmatter under the spec
        # field names: `archived_into` (the canonical URI it merged into),
        # `archived_at` (UTC ISO timestamp), `archived_reason` (caller's
        # justification, when supplied).
        archived_page = EntityPage.from_file(archived)
        assert archived_page is not None
        assert archived_page.extra.get("archived_into") == "person:marcelo"
        assert archived_page.extra.get("archived_reason") == "alias overlap confirmed"
        assert "archived_at" in archived_page.extra

    def test_alias_index_drops_absorbed_ref(self, tmp_path: Path) -> None:
        _write_page(
            tmp_path, "person", "marcelo",
            aliases=["Marcelo"],
        )
        _write_page(
            tmp_path, "person", "marcelo_m",
            aliases=["Marcelo"],
        )
        idx = AliasIndex(tmp_path / "memory")
        idx.build()
        absorber = EntityAbsorption(tmp_path, alias_index=idx)

        # Before: ambiguous
        before = idx.lookup("Marcelo")
        assert set(before) == {"person:marcelo", "person:marcelo_m"}

        absorber.absorb("person:marcelo", "person:marcelo_m", reason="duplicate")

        # After: only canonical
        after = idx.lookup("Marcelo")
        assert after == ["person:marcelo"]
        # Absorbed ref totally removed from the index
        assert "person:marcelo_m" not in idx.all_entities()

    def test_idempotent_on_already_archived(self, tmp_path: Path) -> None:
        _write_page(
            tmp_path, "person", "marcelo",
            aliases=["Marcelo"],
        )
        _write_page(
            tmp_path, "person", "marcelo_m",
            aliases=["Marcelo"],
        )
        absorber = EntityAbsorption(tmp_path)
        sha1 = absorber.absorb("person:marcelo", "person:marcelo_m", reason="dup")
        assert sha1 is not None
        # Second call should be a no-op (absorbed file is now in archive)
        sha2 = absorber.absorb("person:marcelo", "person:marcelo_m", reason="dup")
        assert sha2 is None

    def test_missing_canonical_raises(self, tmp_path: Path) -> None:
        _write_page(tmp_path, "person", "absorbed", aliases=["X"])
        absorber = EntityAbsorption(tmp_path)
        with pytest.raises(AbsorptionError, match="canonical page missing"):
            absorber.absorb("person:nonexistent", "person:absorbed", reason="x")

    def test_missing_absorbed_raises(self, tmp_path: Path) -> None:
        _write_page(tmp_path, "person", "canonical", aliases=["X"])
        absorber = EntityAbsorption(tmp_path)
        with pytest.raises(AbsorptionError, match="absorbed page missing"):
            absorber.absorb("person:canonical", "person:vapor", reason="x")


# ---------------------------------------------------------------------------
# vector index drop on absorb (requires lancedb)
# ---------------------------------------------------------------------------


class _FakeProvider(EmbeddingProvider):
    DIM = 8

    @property
    def model_name(self) -> str:
        return "fake/x"

    @property
    def dimensions(self) -> int:
        return self.DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            seed = float(ord(text[0])) if text else 0.0
            out.append([seed] + [0.0] * (self.DIM - 1))
        return out


@pytest.mark.skipif(
    not vector_index_available(),
    reason="lancedb is not installed",
)
def test_absorb_removes_from_vector_index(tmp_path: Path) -> None:
    _write_page(
        tmp_path, "person", "marcelo",
        name="Marcelo Marmol",
        aliases=["Marcelo"],
        body="canonical body",
    )
    _write_page(
        tmp_path, "person", "marcelo_m",
        name="Marcelo M.",
        aliases=["Marcelo"],
        body="absorbed body",
    )

    index = VectorIndex(tmp_path, _FakeProvider())
    canonical_path = tmp_path / "memory" / "entities" / "person" / "marcelo.md"
    absorbed_path = tmp_path / "memory" / "entities" / "person" / "marcelo_m.md"
    index.upsert_entity_page(
        entity_ref="person:marcelo",
        name="Marcelo Marmol",
        aliases=["Marcelo"],
        body="canonical body",
        path=canonical_path,
    )
    index.upsert_entity_page(
        entity_ref="person:marcelo_m",
        name="Marcelo M.",
        aliases=["Marcelo"],
        body="absorbed body",
        path=absorbed_path,
    )
    # Both findable initially
    hits_before = index.search("Marcelo", top_k=10)
    ids_before = {h["id"] for h in hits_before}
    assert {"person:marcelo", "person:marcelo_m"} <= ids_before

    absorber = EntityAbsorption(tmp_path, vector_index=index)
    absorber.absorb("person:marcelo", "person:marcelo_m", reason="dup")

    hits_after = index.search("Marcelo", top_k=10)
    ids_after = {h["id"] for h in hits_after}
    assert "person:marcelo" in ids_after
    assert "person:marcelo_m" not in ids_after


# ---------------------------------------------------------------------------
# P2 / Q1: _merge_pages folds derived_from + relation provenance (item B bug)
# ---------------------------------------------------------------------------


def test_merge_unions_derived_from_and_folds_provenance() -> None:
    from durin.memory.absorption import _merge_pages
    from durin.memory.field_provenance import relation_prov_key

    canonical = EntityPage(
        type="topic", name="Rabies",
        relations=[{"to": "topic:virology", "type": "related_to"}],
        derived_from=["reference:doc-a"],
        provenance={
            "relations": {
                relation_prov_key("topic:virology", "related_to"): {
                    "to": "topic:virology", "type": "related_to",
                    "source_ref": "c", "extracted_at": "2026-06-01T00:00:00+00:00",
                    "author": "agent",
                },
            },
            "derived_from": {
                "reference:doc-a": {
                    "source_ref": "c", "extracted_at": "2026-06-01T00:00:00+00:00",
                    "author": "agent",
                },
            },
        },
    )
    absorbed = EntityPage(
        type="topic", name="Rabies (dup)",
        relations=[{"to": "topic:zoonosis", "type": "related_to"}],
        derived_from=["reference:doc-a", "reference:doc-b"],
        provenance={
            "relations": {
                relation_prov_key("topic:zoonosis", "related_to"): {
                    "to": "topic:zoonosis", "type": "related_to",
                    "source_ref": "a", "extracted_at": "2026-06-02T00:00:00+00:00",
                    "author": "agent",
                },
            },
            "derived_from": {
                "reference:doc-b": {
                    "source_ref": "a", "extracted_at": "2026-06-02T00:00:00+00:00",
                    "author": "agent",
                },
            },
        },
    )
    merged = _merge_pages(canonical, absorbed, absorbed_ref="topic:rabies_dup")

    # derived_from: union, dedup, canonical order first.
    assert merged.derived_from == ["reference:doc-a", "reference:doc-b"]
    # derived_from provenance: both refs folded.
    assert set(merged.provenance["derived_from"]) == {"reference:doc-a", "reference:doc-b"}
    # relation provenance: BOTH sides retained (item B — was dropped before).
    rel_prov = merged.provenance["relations"]
    tos = {e["to"] for e in rel_prov.values()}
    assert tos == {"topic:virology", "topic:zoonosis"}
