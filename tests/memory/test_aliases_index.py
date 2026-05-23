"""Tests for `durin.memory.aliases_index` — sidecar that maps alias → entities."""

from __future__ import annotations

from pathlib import Path

import pytest

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage


def _write_page(
    memory_root: Path,
    type_: str,
    slug: str,
    *,
    name: str | None = None,
    aliases: list[str] | None = None,
    extra: dict | None = None,
) -> Path:
    """Helper: write an entity page to disk."""
    page = EntityPage(
        type=type_,
        name=name or slug,
        aliases=aliases or [],
        extra=extra or {},
    )
    path = memory_root / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


# ---------------------------------------------------------------------------
# build / load / save
# ---------------------------------------------------------------------------


class TestBuild:
    def test_empty_workspace_builds_empty_index(self, tmp_path: Path) -> None:
        idx = AliasIndex(tmp_path)
        idx.build()
        assert idx.size() == 0

    def test_single_page_indexed(self, tmp_path: Path) -> None:
        _write_page(
            tmp_path, "person", "marcelo",
            name="Marcelo Marmol",
            aliases=["Marcelo", "marcelo"],
        )
        idx = AliasIndex(tmp_path)
        idx.build()
        # All identifying strings, lowercased, map to "person:marcelo"
        assert idx.lookup("marcelo") == ["person:marcelo"]
        assert idx.lookup("Marcelo") == ["person:marcelo"]  # case-insens
        assert idx.lookup("marcelo marmol") == ["person:marcelo"]

    def test_multiple_pages_distinct(self, tmp_path: Path) -> None:
        _write_page(tmp_path, "person", "marcelo", name="Marcelo")
        _write_page(tmp_path, "project", "durin", name="Durin")
        idx = AliasIndex(tmp_path)
        idx.build()
        assert idx.lookup("marcelo") == ["person:marcelo"]
        assert idx.lookup("durin") == ["project:durin"]
        assert len(idx.all_entities()) == 2

    def test_collision_returns_list(self, tmp_path: Path) -> None:
        """Per doc 18 §10 R6: ambiguous aliases return list with both."""
        _write_page(
            tmp_path, "person", "marcelo_marmol",
            name="Marcelo Marmol",
            aliases=["Marcelo", "marcelo"],
        )
        _write_page(
            tmp_path, "person", "marcelo_diaz",
            name="Marcelo Diaz",
            aliases=["Marcelo", "marcelo"],
        )
        idx = AliasIndex(tmp_path)
        idx.build()
        candidates = idx.lookup("marcelo")
        assert set(candidates) == {"person:marcelo_marmol", "person:marcelo_diaz"}
        assert len(candidates) == 2

    def test_emergent_identifiers_indexed(self, tmp_path: Path) -> None:
        """Emergent fields like `identifiers` populate the index alongside aliases."""
        _write_page(
            tmp_path, "person", "marcelo",
            name="Marcelo",
            aliases=["marcelo"],
            extra={
                "identifiers": [
                    "mmarmol@mxhero.com",
                    "UM7TCSZRN",
                    "+5491234567",
                ],
            },
        )
        idx = AliasIndex(tmp_path)
        idx.build()
        # Each identifier maps back to the entity
        assert idx.lookup("mmarmol@mxhero.com") == ["person:marcelo"]
        assert idx.lookup("um7tcszrn") == ["person:marcelo"]  # lowercased
        assert idx.lookup("+5491234567") == ["person:marcelo"]

    def test_archive_subfolders_excluded(self, tmp_path: Path) -> None:
        """Pages under <slug>/archive/ are de-indexed per doc 18 §3 + R6."""
        _write_page(tmp_path, "person", "marcelo", name="Marcelo")
        # Archived absorbed alias — should NOT be in index
        archived = tmp_path / "entities" / "person" / "marcelo" / "archive"
        archived.mkdir(parents=True)
        EntityPage(
            type="person",
            name="Marcelo Marmolovich",
            aliases=["marcelo-m"],
            extra={"absorbed_into": "../../marcelo.md"},
        ).save(archived / "marcelo-m.md")
        idx = AliasIndex(tmp_path)
        idx.build()
        # marcelo-m alias should not surface
        assert idx.lookup("marcelo-m") == []
        # The absorbed entity ref also absent
        assert "person:marcelo-m" not in idx.all_entities()

    def test_malformed_page_skipped(self, tmp_path: Path) -> None:
        """Pages that fail to parse are skipped — index doesn't crash."""
        _write_page(tmp_path, "person", "marcelo", name="Marcelo")
        bad = tmp_path / "entities" / "person" / "garbage.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("not a valid frontmatter\n", encoding="utf-8")
        idx = AliasIndex(tmp_path)
        idx.build()
        assert idx.lookup("marcelo") == ["person:marcelo"]
        assert idx.size() >= 1


# ---------------------------------------------------------------------------
# (no persistence — per doc 23 T1.4 + G14, AliasIndex is rebuild-only.
# Tests for `save()` and `load()` removed; `build()` is the only way to
# populate the index from disk.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Incremental add / remove / refresh
# ---------------------------------------------------------------------------


class TestIncremental:
    def test_add_new_entity(self, tmp_path: Path) -> None:
        idx = AliasIndex(tmp_path)
        page = EntityPage(type="topic", name="Embeddings", aliases=["embed"])
        idx.add(page, slug="embeddings")
        assert idx.lookup("embed") == ["topic:embeddings"]
        assert idx.lookup("embeddings") == ["topic:embeddings"]

    def test_remove_entity(self, tmp_path: Path) -> None:
        idx = AliasIndex(tmp_path)
        page = EntityPage(type="person", name="Marcelo", aliases=["marcelo"])
        idx.add(page, slug="marcelo")
        assert idx.lookup("marcelo") == ["person:marcelo"]
        idx.remove("person:marcelo")
        assert idx.lookup("marcelo") == []
        assert idx.size() == 0

    def test_remove_only_one_of_collision(self, tmp_path: Path) -> None:
        """When two entities share an alias, removing one keeps the other."""
        idx = AliasIndex(tmp_path)
        a = EntityPage(type="person", name="Marcelo Marmol", aliases=["marcelo"])
        b = EntityPage(type="person", name="Marcelo Diaz", aliases=["marcelo"])
        idx.add(a, slug="marcelo_marmol")
        idx.add(b, slug="marcelo_diaz")
        assert len(idx.lookup("marcelo")) == 2
        idx.remove("person:marcelo_marmol")
        assert idx.lookup("marcelo") == ["person:marcelo_diaz"]

    def test_refresh_atomic_remove_then_add(self, tmp_path: Path) -> None:
        """refresh_for replaces an entity's aliases atomically."""
        idx = AliasIndex(tmp_path)
        old = EntityPage(type="person", name="Marcelo", aliases=["m"])
        idx.add(old, slug="marcelo")
        assert idx.lookup("m") == ["person:marcelo"]
        new = EntityPage(
            type="person",
            name="Marcelo Marmol",
            aliases=["marcelo", "Marcelo M."],
        )
        idx.refresh_for(new, slug="marcelo")
        assert idx.lookup("m") == []  # old alias gone
        assert idx.lookup("marcelo") == ["person:marcelo"]
        assert idx.lookup("marcelo m.") == ["person:marcelo"]


# ---------------------------------------------------------------------------
# Lookup semantics
# ---------------------------------------------------------------------------


class TestLookup:
    def test_case_insensitive(self, tmp_path: Path) -> None:
        idx = AliasIndex(tmp_path)
        idx.add(
            EntityPage(type="person", name="Marcelo", aliases=["Marcelo"]),
            slug="marcelo",
        )
        assert idx.lookup("marcelo") == ["person:marcelo"]
        assert idx.lookup("MARCELO") == ["person:marcelo"]
        assert idx.lookup("Marcelo") == ["person:marcelo"]

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        idx = AliasIndex(tmp_path)
        idx.add(
            EntityPage(type="person", name="Marcelo"),
            slug="marcelo",
        )
        assert idx.lookup("  Marcelo  ") == ["person:marcelo"]

    def test_unknown_returns_empty(self, tmp_path: Path) -> None:
        idx = AliasIndex(tmp_path)
        assert idx.lookup("nobody") == []

    def test_lookup_returns_copy(self, tmp_path: Path) -> None:
        """Mutating the returned list must not affect the index."""
        idx = AliasIndex(tmp_path)
        idx.add(EntityPage(type="person", name="Marcelo"), slug="marcelo")
        result = idx.lookup("marcelo")
        result.append("bogus")
        assert idx.lookup("marcelo") == ["person:marcelo"]
