"""Tests for AliasIndex bootstrap from episodic entries (G3.e).

Background: AliasIndex.build() historically walks only `memory/entities/`
(the canonical pages produced by Dream). In cold workspaces, Dream
hasn't run yet, so AliasIndex is empty → entity_ranker can't activate
→ memory_search degrades to pure vector cosine. Verified empirically
by g3_verification.py: full pipeline == baseline at 5% pass-rate with
distractors.

G3.e extends build() to also derive minimal aliases from the
`entities:` frontmatter of episodic/stable/corpus/pending entries.
Entity pages still take precedence when both exist (richer aliases).

Per glm review:
- Slugs are indexed AS-IS — no underscore splitting (anti-pattern that
  produces noisy partial matches like "data_migration" → "data").
- Ambiguous slugs across types are preserved as a list (consistent
  with existing collision semantics).
- Malformed entity refs (no ':') skip silently.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from durin.memory.aliases_index import AliasIndex
from durin.memory.entity_page import EntityPage
from durin.memory.store import store_memory


def _write_entity_page(
    memory_root: Path,
    type_: str,
    slug: str,
    *,
    name: str | None = None,
    aliases: list[str] | None = None,
) -> Path:
    page = EntityPage(
        type=type_,
        name=name or slug.title(),
        aliases=aliases or [],
        extra={},
    )
    path = memory_root / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


# ---------------------------------------------------------------------------
# Core bootstrap behaviour
# ---------------------------------------------------------------------------


class TestEpisodicBootstrap:
    def test_picks_up_episodic_entities_when_no_entity_pages(
        self, tmp_path: Path,
    ) -> None:
        """Without entity_pages, AliasIndex must populate with slugs derived
        from the `entities:` frontmatter of the episodic entries."""
        store_memory(
            tmp_path, content="Caroline lives in Boston",
            entities=["person:caroline"],
            valid_from=datetime.date(2024, 1, 1),
        )
        store_memory(
            tmp_path, content="Marcelo works on durin",
            entities=["person:marcelo", "project:durin"],
            valid_from=datetime.date(2024, 1, 2),
        )

        idx = AliasIndex(tmp_path / "memory")
        idx.build()

        assert "person:caroline" in idx.lookup("caroline")
        assert "person:marcelo" in idx.lookup("marcelo")
        assert "project:durin" in idx.lookup("durin")

    def test_handles_mixed_classes_corpus_stable_pending(
        self, tmp_path: Path,
    ) -> None:
        """corpus / stable / pending entries with entities also count."""
        store_memory(
            tmp_path, content="Python tips collection",
            entities=["topic:python"],
            class_name="corpus",
        )
        store_memory(
            tmp_path, content="Alice's preferred meeting time",
            entities=["person:alice"],
            class_name="stable",
        )
        store_memory(
            tmp_path, content="Renew SSL cert next month",
            entities=["task:ssl_renewal"],
            class_name="pending",
        )

        idx = AliasIndex(tmp_path / "memory")
        idx.build()

        assert "topic:python" in idx.lookup("python")
        assert "person:alice" in idx.lookup("alice")
        assert "task:ssl_renewal" in idx.lookup("ssl_renewal")

    def test_build_defensive_against_corrupted_file(
        self, tmp_path: Path,
    ) -> None:
        """Corrupted .md (un-loadable) doesn't crash build — the rest
        of the workspace still indexes correctly.

        (Note: MemoryEntry's frontmatter validator rejects malformed
        entity refs at write time, so a loaded entry won't contain
        them. The defence-in-depth ``":" not in entity_ref`` check in
        build() is for the hypothetical case where corrupted state
        bypasses the validator — not directly testable end-to-end
        without monkey-patching internals.)
        """
        ep_dir = tmp_path / "memory" / "episodic"
        ep_dir.mkdir(parents=True)
        # File with no frontmatter at all — load_entry should fail
        (ep_dir / "corrupt.md").write_text("not valid frontmatter at all")
        # Plus one well-formed entry that should still be indexed
        store_memory(
            tmp_path, content="alice note",
            entities=["person:alice"],
        )

        idx = AliasIndex(tmp_path / "memory")
        idx.build()  # must not raise

        assert "person:alice" in idx.lookup("alice")



# ---------------------------------------------------------------------------
# Precedence: entity_pages have richer info, should not be shadowed
# ---------------------------------------------------------------------------


class TestEntityPagePrecedence:
    def test_entity_page_aliases_preserved_alongside_episodic_slug(
        self, tmp_path: Path,
    ) -> None:
        """Entity_page with `aliases: [Caro, Carolina]` must still be
        lookup-able by those aliases, in addition to the slug we
        derive from the episodic."""
        memory_root = tmp_path / "memory"
        _write_entity_page(
            memory_root, "person", "caroline",
            name="Caroline", aliases=["Caro", "Carolina"],
        )
        store_memory(
            tmp_path, content="caroline observed something",
            entities=["person:caroline"],
        )

        idx = AliasIndex(memory_root)
        idx.build()

        # Lookup by entity_page-specific alias works
        assert "person:caroline" in idx.lookup("caro")
        assert "person:caroline" in idx.lookup("carolina")
        # Lookup by slug (derived from episodic) also works
        assert "person:caroline" in idx.lookup("caroline")

    def test_no_duplicate_refs_when_entity_page_and_episodic_coexist(
        self, tmp_path: Path,
    ) -> None:
        """Si entity_page tiene `name: caroline` y episodic tiene
        `person:caroline`, lookup("caroline") debe devolver el ref
        UNA sola vez, no duplicado."""
        memory_root = tmp_path / "memory"
        _write_entity_page(
            memory_root, "person", "caroline",
            name="caroline", aliases=[],
        )
        store_memory(
            tmp_path, content="caroline note",
            entities=["person:caroline"],
        )

        idx = AliasIndex(memory_root)
        idx.build()

        refs = idx.lookup("caroline")
        assert refs.count("person:caroline") == 1


# ---------------------------------------------------------------------------
# Edge cases identified by glm review
# ---------------------------------------------------------------------------


class TestEdgeCasesPerGlmReview:
    def test_ambiguous_slug_across_types_preserved_as_list(
        self, tmp_path: Path,
    ) -> None:
        """Same slug across distinct entity types (project:python +
        snake:python) → lookup returns BOTH refs. Disambiguation belongs
        to the consumer, not the index."""
        store_memory(
            tmp_path, content="Python project release",
            entities=["project:python"],
        )
        store_memory(
            tmp_path, content="My snake is shedding",
            entities=["snake:python"],
        )

        idx = AliasIndex(tmp_path / "memory")
        idx.build()

        refs = idx.lookup("python")
        assert "project:python" in refs
        assert "snake:python" in refs
        assert len(refs) == 2

    def test_does_not_split_underscored_slugs(self, tmp_path: Path) -> None:
        """Underscored slugs (data_migration) are NOT split into tokens.
        Splitting would inject noise: lookup('data') would surface
        data_migration even when the user means an unrelated 'data'."""
        store_memory(
            tmp_path, content="DB migration finished",
            entities=["project:data_migration"],
        )

        idx = AliasIndex(tmp_path / "memory")
        idx.build()

        # Full slug works
        assert "project:data_migration" in idx.lookup("data_migration")
        # Splits do NOT
        assert "project:data_migration" not in idx.lookup("data")
        assert "project:data_migration" not in idx.lookup("migration")

    def test_lookup_is_case_insensitive(self, tmp_path: Path) -> None:
        """Slugs are normalized to lowercase, queries also lowercased."""
        store_memory(
            tmp_path, content="entry",
            entities=["person:Marcelo"],  # mixed-case slug
        )

        idx = AliasIndex(tmp_path / "memory")
        idx.build()

        assert "person:Marcelo" in idx.lookup("MARCELO")
        assert "person:Marcelo" in idx.lookup("marcelo")
        assert "person:Marcelo" in idx.lookup("Marcelo")

    def test_empty_workspace_returns_empty_lookup(self, tmp_path: Path) -> None:
        """Cold start with no entries at all — no crash, empty lookups."""
        idx = AliasIndex(tmp_path / "memory")
        idx.build()
        assert idx.lookup("anything") == []
